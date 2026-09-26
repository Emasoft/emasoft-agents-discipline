"""A shell-free command interpreter: hand-written tokenizer/parser/analyzer/executor.

Why this exists (TRDD-2U56GG7S, docs_dev/no-bash-spec.md): the user forbade any bash/shell
dependency in this plugin, including on Windows. `subprocess.run(cmd, shell=True)` spawns
`/bin/sh` or `cmd.exe` -- exactly what is forbidden. This module reimplements the small
supported grammar directly against `subprocess.Popen` with `shell=False`, so no shell process
is ever created on any platform.

Execution model (v2, docs_dev/cmdrun-v2-executor-spec.md): two processes, one timekeeper.
`python cmdrun.py` is the **supervisor** -- it reads/validates the request and answers a
refusal without spawning anything, threadless, no deadline logic of its own beyond the
single wait loop below. For an accepted command it spawns `python cmdrun.py --exec` (the
**worker**) with `start_new_session=True`, `stdin=PIPE` (the death pipe), `stdout=PIPE` (the
worker's one-line JSON answer), `stderr=DEVNULL`, and writes the validated request as one JSON
line. The worker's pid IS the one process group of the whole command (every stage it spawns is
a plain, group-inheriting `Popen`); a daemon thread in the worker reads stdin to EOF and
SIGKILLs its own group the instant that happens -- so a caller that SIGKILLs the supervisor (or
the supervisor's own group) reaches the worker automatically, because the OS closes the
worker's stdin pipe the moment the supervisor dies. The supervisor is the *only* timekeeper: on
its own deadline, on SIGTERM/SIGHUP/SIGINT, or on the worker exiting without a valid answer, it
repeats `killpg(worker_pid, SIGKILL)` until the kernel reports the group gone, under one global
cleanup deadline, and never answers silently on the rare case that bound is exhausted
(`leaked: true`). This closes the H1-H10 findings in reports/no-bash/*cmdrun-py-attack.md: no
fork, no result file, no per-stage process groups, no chdir-based glob race.

`analyze(command, env=None, cwd=None) -> (ok, reason)` is exposed standalone so a linter (a
future ledger-check/gate-check step) can ask "would this be refused" without executing anything.
"""

from __future__ import annotations

import ctypes
import glob as glob_mod
import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes  # pure-Python type defs; importable on every platform, not just win32
from dataclasses import dataclass, field
from typing import AbstractSet, Optional, Union

IS_WINDOWS = sys.platform == "win32"
# F4: the CMDRUN_TEST_* env hooks below (worker-crash injection, pre-spawn delay, cleanup-budget
# override) must be inert for every real caller -- gated on an argv flag ONLY tests pass, never on
# the env alone (a nested cmdrun invoked from inside a test run would otherwise inherit the outer
# test's env and silently arm the same hooks). Computed once, from argv, so both the supervisor
# invocation (`cmdrun.py --test-hooks`) and the worker invocation it spawns
# (`cmdrun.py --exec --test-hooks`) see the same answer.
TEST_HOOKS = "--test-hooks" in sys.argv[1:]

# ponytail: bounded, not unlimited -- an attacker-controlled command string must never make the
# parser do unbounded work. These caps are generous for real use and small enough that a fuzzer
# throwing 10k-stage pipelines or megabyte argv strings at us degrades to a fast refusal instead
# of a slow crawl.
MAX_COMMAND_LENGTH = 262_144
MAX_GROUP_DEPTH = 32
MAX_PIPELINE_STAGES = 512
MAX_TOKENS = 20_000
MAX_EXPANDED_BYTES = 1_048_576  # M4/M5: cap on the argv text a $VAR expansion can amplify to.
MAX_REQUEST_BYTES = 16 * 1024 * 1024  # spec: read stdin to EOF, cap 16 MiB.
# F9: `output_limit` cap. JSON string escaping can grow a captured byte to as much as 6 bytes in
# the answer line, and the old 64 MiB cap let a 64 MiB stdout + 64 MiB stderr capture produce an
# 805 MB answer line with 3.5 GB peak supervisor RSS (27x the captured bytes) -- and outlast the
# request's own timeout on nothing but encoding/writing that answer. The worst case under the new
# cap is 2 streams x 4 MiB x 6 bytes/byte = 48 MB (review round 2 corrected the earlier "under
# 32 MB" claim -- still ~17x better than 805 MB, and no gate consumer needs more today; step 4
# ports the consumers, so if one ever does, raise the cap then, not speculatively).
MAX_OUTPUT_LIMIT = 4 * 1024 * 1024
# Global cleanup deadline (spec: "deadline + 1.5 s") shared by the killpg-until-ESRCH loop and
# the reap that follows it -- exhausting it answers `leaked: true`, never silently.
CLEANUP_BUDGET_S = 1.5


def _cleanup_budget_s() -> float:
    """`CLEANUP_BUDGET_S`, overridable via the `CMDRUN_CLEANUP_BUDGET_S` env var -- a TEST-ONLY
    hook (gated on `--test-hooks`; not a request field: a caller must never be able to shrink the
    group-kill deadline from outside) so a test can force the "cannot confirm dead in time" /
    `leaked: true` path without waiting out the real 1.5s budget on every run.

    F4: `nan`/`inf` make `_kill_group_until_dead`'s `time.monotonic() >= deadline` comparison
    never succeed (any comparison against `nan` is False, and `now >= now+inf` is never True
    either), so an override that isn't finite and within a sane 0-10s range is REJECTED --
    fall back to the real budget -- instead of trusted as-is and looped on forever."""
    if not TEST_HOOKS:
        return CLEANUP_BUDGET_S
    override = os.environ.get("CMDRUN_CLEANUP_BUDGET_S")
    if override:
        try:
            value = float(override)
        except ValueError:
            return CLEANUP_BUDGET_S
        if math.isfinite(value) and 0.0 <= value <= 10.0:
            return value
    return CLEANUP_BUDGET_S

_IDENT_START = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_IDENT_CONT = _IDENT_START | set("0123456789")
_REFUSED_UNQUOTED_CHARS = {
    "`": "backticks",
    "~": "~",
    "{": "{ }",
    "}": "{ }",
    "!": "!",
}
_OP_CHARS = set("|&<>")
BUILTIN_NAMES = {"cd", "true", "false", "echo", "test", "["}
# M10/advisor P8: policing a tool's shebang breaks ordinary commands (yarn, pyenv shims). The
# grammar polices the command WORD only -- the ledger's syntax, not a tool's internals.
SHELL_NAMES = {
    "sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "busybox", "yash",
    "fish", "csh", "tcsh", "nu", "xonsh", "elvish",
    "cmd", "powershell", "pwsh", "wsl",
}
_REFUSED_SPECIAL_DOLLAR = set("0123456789@*#?$!-")


class Refused(Exception):
    """Raised anywhere in tokenize/parse/analyze to name a refused construct."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


@dataclass
class Word:
    # parts: list of (text, literal). literal=True means "never a glob metachar",
    # i.e. it came from a quote, an escape, or a $VAR expansion.
    parts: list = field(default_factory=list)
    # M8: True the instant ANY quote (single or double, even "") was consumed while building
    # this word -- the only way to tell a deliberately empty argument ("", "$UNSET") apart from
    # an unquoted word that expanded to nothing, since `Word.add` drops empty text either way.
    saw_quote: bool = False

    def add(self, text, literal):
        if text:
            self.parts.append((text, literal))

    def is_empty(self):
        return not self.parts


@dataclass
class Tok:
    kind: str  # WORD, PIPE, AND, LPAREN, RPAREN, REDIR_FILE, REDIR_DUP
    word: Optional[Word] = None
    fd: Optional[int] = None
    redir_kind: Optional[str] = None  # '>', '>>', '<'
    dup_target: Optional[int] = None
    raw_prefix: str = ""  # raw text before any quote began (for VAR= detection)


def _charge(budget, text):
    """M4/M5: bound the total bytes a $VAR/${VAR} expansion can add across the whole command --
    otherwise a 4 KB env value referenced 65,000 times amplifies to hundreds of MB before the
    parser ever gets a chance to refuse anything (measured: 822 MB peak RSS from a 262 KB
    command)."""
    budget[0] += len(text)
    if budget[0] > MAX_EXPANDED_BYTES:
        raise Refused("expanded command too large")


def _expand_dollar(s, i, n, env, out_literal_parts, budget):
    """s[i] == '$'. Returns new index. Appends (text, literal=True) parts."""
    i += 1
    if i < n and s[i] == "{":
        # M4: `s.find` from i+1, never `"}" in s[i+1:]` -- the old slice copies the REST of the
        # command on every single `${`, making a command with many of them quadratic.
        j = s.find("}", i + 1)
        if j == -1:
            raise Refused("unterminated ${...}")
        content = s[i + 1 : j]
        if content and content[0] in _IDENT_START and all(c in _IDENT_CONT for c in content[1:]):
            value = _env_get(env, content)
            out_literal_parts.append((value, True))
            _charge(budget, value)
            return j + 1
        raise Refused(f'unsupported "${{{content}}}" operator')
    # M7: `$0`-`$9`, `$@ $* $# $? $$ $! $-` refused BY NAME -- bash expands these; this
    # interpreter has no positional/special parameters to give them, so printing them literally
    # (the old behaviour) silently disagrees with what a real shell would do.
    if i < n and s[i] in _REFUSED_SPECIAL_DOLLAR:
        raise Refused(f"'${s[i]}' is refused")
    if i < n and s[i] == "'":
        raise Refused("$'...' is refused")
    if i < n and s[i] == '"':
        raise Refused('$"..." is refused')
    if i < n and s[i] in _IDENT_START:
        j = i
        while j < n and s[j] in _IDENT_CONT:
            j += 1
        name = s[i:j]
        value = _env_get(env, name)
        out_literal_parts.append((value, True))
        _charge(budget, value)
        return j
    if i < n and s[i] == "(":
        raise Refused("$( command substitution")
    # Bare '$' not followed by a name: literal '$'.
    out_literal_parts.append(("$", True))
    return i


def _env_get(env, name):
    if env is None:
        return ""
    if IS_WINDOWS:
        upper = name.upper()
        for k, v in env.items():
            if k.upper() == upper:
                return v
        return ""
    return env.get(name, "")


def _env_set(env_dict, name, value):
    """M12: Windows env keys are case-insensitive, but a Python dict is not -- setting `PATH`
    next to an inherited `Path` would hand the child TWO keys the OS disagrees about. Replace
    whichever existing key matches case-insensitively before inserting the new one."""
    if IS_WINDOWS:
        upper = name.upper()
        for k in list(env_dict.keys()):
            if k != name and k.upper() == upper:
                del env_dict[k]
    env_dict[name] = value


def tokenize(command, env):
    if len(command) > MAX_COMMAND_LENGTH:
        raise Refused("command too long")
    if "\n" in command or "\r" in command:
        raise Refused("newline / multi-line command")
    n = len(command)
    tokens = []
    i = 0
    budget = [0]
    while i < n:
        c = command[i]
        if c in " \t":
            i += 1
            continue
        if c == "#" and (i == 0 or command[i - 1] in " \t"):
            break  # comment to end of line
        # fd-prefixed or bare redirection? M6: a digit RUN immediately before '<'/'>' is an fd
        # prefix candidate; refuse anything but a single digit 0-2 instead of silently letting
        # "3" become a stray argument and fd 3 misparse as fd 1 (the old bug).
        fd = None
        # ASCII digits ONLY, never str.isdigit(): isdigit() also matches every Unicode
        # decimal (Arabic-Indic ٠-٩, superscripts...), so py accepted a non-ASCII fd
        # prefix and REFUSED with "out of range 0-2" where the JS twin (c >= "0" &&
        # c <= "9") folded the run into the word -- same bytes, opposite verdicts
        # (measured: '٢1>out.txt echo hi' refused here, executed there).
        if c in "0123456789":
            j = i
            while j < n and command[j] in "0123456789":
                j += 1
            if j < n and command[j] in "<>":
                numstr = command[i:j]
                if len(numstr) != 1 or numstr not in "012":
                    raise Refused(f"redirection fd '{numstr}' out of range 0-2")
                fd = int(numstr)
                i = j
                c = command[i]
        if c in _OP_CHARS:
            j = i
            while j < n and command[j] in _OP_CHARS:
                j += 1
            opstr = command[i:j]
            i = j
            tok = _classify_op(opstr, fd)
            if tok.kind == "REDIR_DUP":
                if i < n and command[i] in "0123456789":
                    j = i
                    while j < n and command[j] in "0123456789":
                        j += 1
                    numstr = command[i:j]
                    if len(numstr) != 1 or numstr not in "012":
                        raise Refused(f"dup target '{numstr}' out of range 0-2")
                    tok.dup_target = int(numstr)
                    i = j
                else:
                    raise Refused(f"'{opstr}' without a target file descriptor")
            tokens.append(tok)
            if len(tokens) > MAX_TOKENS:
                raise Refused("too many tokens")
            continue
        if c == ";":
            raise Refused("lone ';'")
        if c == "(":
            tokens.append(Tok(kind="LPAREN"))
            i += 1
            continue
        if c == ")":
            tokens.append(Tok(kind="RPAREN"))
            i += 1
            continue
        # WORD
        word = Word()
        raw_prefix_chars = []
        raw_prefix_done = False
        started = i
        while i < n:
            ch = command[i]
            if ch in " \t" or ch in _OP_CHARS or ch in "();#\n\r":
                if ch == "#" and not (i == started or command[i - 1] in " \t"):
                    pass  # mid-word '#' is literal
                else:
                    break
            if ch == "'":
                word.saw_quote = True
                raw_prefix_done = True
                j = command.find("'", i + 1)
                if j == -1:
                    raise Refused("unterminated single quote")
                word.add(command[i + 1 : j], True)
                i = j + 1
                continue
            if ch == '"':
                word.saw_quote = True
                raw_prefix_done = True
                i += 1
                buf = []
                closed = False
                while i < n:
                    dc = command[i]
                    if dc == '"':
                        closed = True
                        i += 1
                        break
                    if dc == "`":
                        raise Refused("backticks")
                    if dc == "\\" and i + 1 < n and command[i + 1] in '"\\$':
                        buf.append(command[i + 1])
                        i += 2
                        continue
                    if dc == "$":
                        parts = []
                        i = _expand_dollar(command, i, n, env, parts, budget)
                        if buf:
                            word.add("".join(buf), True)
                            buf = []
                        for text, lit in parts:
                            word.add(text, lit)
                        continue
                    buf.append(dc)
                    i += 1
                if not closed:
                    raise Refused("unterminated double quote")
                if buf:
                    word.add("".join(buf), True)
                continue
            if ch == "\\":
                if i + 1 >= n:
                    raise Refused("trailing backslash")
                nxt = command[i + 1]
                # I2: on Windows, an unquoted backslash is almost always a PATH separator
                # (`C:\Users\foo`), not an escape -- silently treating `\U` as "literal U" (the
                # POSIX behaviour above) would mangle the path into "C:Usersfoo". Refuse instead
                # of guessing, and tell the caller the fix: quote the whole path.
                # ASCII-only letter/digit set, matching the JS twin's /[A-Za-z0-9]/:
                # isalpha()/isdigit() are Unicode-wide, so py would refuse `\Ä` on
                # Windows where the JS twin treats it as a plain escape.
                if IS_WINDOWS and (nxt in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                   or nxt in "0123456789"):
                    raise Refused("unquoted backslash before a letter/digit -- quote Windows paths")
                word.add(nxt, True)
                i += 2
                raw_prefix_done = True
                continue
            if ch == "$":
                parts = []
                i = _expand_dollar(command, i, n, env, parts, budget)
                for text, lit in parts:
                    word.add(text, lit)
                raw_prefix_done = True
                continue
            if ch == "`":
                raise Refused("backticks")
            if ch in _REFUSED_UNQUOTED_CHARS:
                raise Refused(_REFUSED_UNQUOTED_CHARS[ch])
            if not raw_prefix_done:
                raw_prefix_chars.append(ch)
            word.add(ch, False)
            i += 1
        tokens.append(Tok(kind="WORD", word=word, raw_prefix="".join(raw_prefix_chars)))
        if len(tokens) > MAX_TOKENS:
            raise Refused("too many tokens")
    return tokens


def _classify_op(opstr, fd):
    if opstr == "|":
        return Tok(kind="PIPE")
    if opstr == "&&":
        return Tok(kind="AND")
    if opstr == ">":
        return Tok(kind="REDIR_FILE", fd=1 if fd is None else fd, redir_kind=">")
    if opstr == ">>":
        return Tok(kind="REDIR_FILE", fd=1 if fd is None else fd, redir_kind=">>")
    if opstr == "<":
        return Tok(kind="REDIR_FILE", fd=0 if fd is None else fd, redir_kind="<")
    if opstr == ">&":
        return Tok(kind="REDIR_DUP", fd=1 if fd is None else fd)
    if opstr == "||":
        raise Refused("'||'")
    if opstr == "|&":
        raise Refused("'|&'")
    if opstr == "&>":
        raise Refused("'&>'")
    if opstr == "&>>":
        raise Refused("'&>>'")
    if opstr == "<<":
        raise Refused("'<<' heredoc")
    if opstr == "<<<":
        raise Refused("'<<<' herestring")
    if opstr == "&":
        raise Refused("lone '&' (background)")
    raise Refused(f"unsupported operator '{opstr}'")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@dataclass
class Redir:
    fd: int
    kind: str
    target: object  # Word for file redirs, int for dup redirs


@dataclass
class Command:
    assigns: list
    name: Word
    args: list
    redirs: list


@dataclass
class Group:
    chain: object


@dataclass
class Pipeline:
    stages: list


@dataclass
class Chain:
    pipelines: list


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0
        self.depth = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def parse_chain(self):
        pipelines = [self.parse_pipeline()]
        tok = self.peek()
        while tok is not None and tok.kind == "AND":
            self.i += 1
            pipelines.append(self.parse_pipeline())
            tok = self.peek()
        return Chain(pipelines)

    def parse_pipeline(self):
        stages = [self.parse_stage()]
        tok = self.peek()
        while tok is not None and tok.kind == "PIPE":
            self.i += 1
            stages.append(self.parse_stage())
            tok = self.peek()
        if len(stages) > MAX_PIPELINE_STAGES:
            raise Refused("pipeline has too many stages")
        return Pipeline(stages)

    def parse_stage(self):
        tok = self.peek()
        if tok is None:
            raise Refused("expected a command")
        if tok.kind == "LPAREN":
            self.depth += 1
            if self.depth > MAX_GROUP_DEPTH:
                raise Refused("too deeply nested")
            self.i += 1
            inner = self.parse_chain()
            close_tok = self.peek()
            if close_tok is None or close_tok.kind != "RPAREN":
                raise Refused("unbalanced '('")
            self.i += 1
            self.depth -= 1
            return Group(inner)
        return self.parse_simple_command()

    def parse_simple_command(self):
        assigns = []
        name = None
        args = []
        redirs = []
        saw_word = False
        while True:
            tok = self.peek()
            if tok is None or tok.kind in ("PIPE", "AND", "RPAREN"):
                break
            if tok.kind == "REDIR_FILE":
                self.i += 1
                target_tok = self.peek()
                if target_tok is None or target_tok.kind != "WORD":
                    raise Refused("redirection missing a target")
                self.i += 1
                redirs.append(Redir(tok.fd, tok.redir_kind, target_tok.word))
                continue
            if tok.kind == "REDIR_DUP":
                self.i += 1
                redirs.append(Redir(tok.fd, ">&", tok.dup_target))
                continue
            if tok.kind != "WORD":
                raise Refused(f"unexpected token {tok.kind}")
            if not saw_word:
                m = _assignment_name(tok.raw_prefix, tok.word)
                if m is not None:
                    assigns.append(m)
                    self.i += 1
                    continue
            if not saw_word:
                name = tok.word
                saw_word = True
            else:
                args.append(tok.word)
            self.i += 1
        if name is None:
            raise Refused("assignment with no command" if assigns else "empty or whitespace-only command")
        return Command(assigns=assigns, name=name, args=args, redirs=redirs)


def _assignment_name(raw_prefix, word):
    """`NAME=value` prefix, unquoted NAME, literal '='. Returns (name, value_word) or None."""
    if not raw_prefix or raw_prefix[0] not in _IDENT_START:
        return None
    j = 1
    while j < len(raw_prefix) and raw_prefix[j] in _IDENT_CONT:
        j += 1
    if j >= len(raw_prefix) or raw_prefix[j] != "=":
        return None
    name = raw_prefix[:j]
    # Everything the raw_prefix scan captured is unquoted plain text; the '=' plus
    # whatever follows (still part of the same Word) is the value. We locate the
    # split point by re-walking `word.parts`, cutting exactly at len(name)+1 chars
    # into the *unquoted, unescaped* leading text -- which is exactly raw_prefix,
    # so the split point in the flattened parts is len(name) + 1 characters in.
    cut = len(name) + 1
    value = Word()
    consumed = 0
    for text, literal in word.parts:
        if consumed >= cut:
            value.add(text, literal)
            continue
        remaining_cut = cut - consumed
        if not literal and len(text) >= remaining_cut:
            tail = text[remaining_cut:]
            if tail:
                value.add(tail, literal)
            consumed += len(text)
        else:
            consumed += len(text)
    return (name, value)


def parse(tokens):
    if not tokens:
        raise Refused("empty or whitespace-only command")
    p = _Parser(tokens)
    chain = p.parse_chain()
    trailing = p.peek()
    if trailing is not None:
        raise Refused(f"unexpected token {trailing.kind}")
    return chain


# ---------------------------------------------------------------------------
# Word expansion (variables done at tokenize time; glob left for here)
# ---------------------------------------------------------------------------

_GLOB_CHARS = set("*?[")


def expand_word_no_glob(word):
    return "".join(text for text, _ in word.parts)


def expand_word_glob(word, cwd):
    """Returns list[str] -- the arguments this word expands to (1, or N on a glob match)."""
    literal_str = expand_word_no_glob(word)
    raw_text = "".join(text for text, literal in word.parts if not literal)
    if not any(ch in _GLOB_CHARS for ch in raw_text):
        return [literal_str]
    pattern = "".join(glob_mod.escape(text) if literal else text for text, literal in word.parts)
    matches = sorted(_glob_in(cwd, pattern))
    if not matches:
        return [literal_str]
    return matches


def _glob_in(cwd, pattern):
    # H8: `glob.glob(pattern, root_dir=cwd)` (3.10+) instead of a chdir/restore pair -- the old
    # chdir ran on whatever thread happened to own the pipeline stage, and a SIBLING pipeline
    # stage running concurrently on another thread would restore `prev` out from under it,
    # expanding globs in the wrong directory (reproduced: 23 of 25 runs wrong). `root_dir`
    # requires no process-wide mutable state at all.
    return glob_mod.glob(pattern, root_dir=cwd)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

_REFUSED_TARGET_SUFFIXES = (".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1")


def compile_command(command, env=None):
    """Tokenize + parse + static checks that don't require running anything.

    Returns (chain_or_None, reason_or_None).
    """
    env = env if env is not None else os.environ
    try:
        tokens = tokenize(command, env)
        chain = parse(tokens)
        _check_static(chain)
        return chain, None
    except Refused as exc:
        return None, exc.reason


def _check_static(chain):
    for pipeline in chain.pipelines:
        stages = pipeline.stages
        if len(stages) > 1:
            for stage in stages:
                if isinstance(stage, Command) and _is_builtin_name(stage):
                    raise Refused(f"builtin '{_static_name(stage)}' is not allowed in a pipeline")
        for stage in stages:
            if isinstance(stage, Group):
                _check_static(stage.chain)
            elif isinstance(stage, Command):
                _finalize_empty_words(stage)
                _check_target_suffix(stage)


def _static_name(cmd):
    return expand_word_no_glob(cmd.name)


def _is_builtin_name(cmd):
    return _static_name(cmd) in BUILTIN_NAMES


def _finalize_empty_words(cmd):
    """M8: an UNQUOTED word that expands to empty is dropped, with three pins (spec): a quoted
    empty word ("", "$X") stays an empty argument (`saw_quote` survives regardless of content);
    `VAR=$EMPTY cmd` stays an assignment (assigns are untouched here, only `cmd.args` is
    filtered); a COMMAND word that expands to empty is refused outright rather than silently
    running the next word as the command."""
    if cmd.name.is_empty() and not cmd.name.saw_quote:
        raise Refused("command word expands to empty")
    cmd.args[:] = [w for w in cmd.args if not (w.is_empty() and not w.saw_quote)]


def _is_shell_name(name):
    base = name.lower()
    if base.endswith(".exe"):
        base = base[: -len(".exe")]
    return base in SHELL_NAMES


def _check_env_target(args):
    """M10/F10: `env bash -c ...` is the canonical way around a shell-name refusal on the command
    word alone -- apply the same rule to whatever `env` would exec, after skipping its own
    `VAR=x`, `-i`, `-u NAME`/attached `-uNAME`, `-S STRING` (split into words -- its first word
    IS the target), `--` (end of options), `-C DIR`, and combined short-flag clusters (`-iv`,
    `-vuNAME`). The old version treated `-S`'s argument as an opaque flag with no argument at
    all, so `env -S 'bash -c true'` slipped through unrefused: the next token examined was the
    whole quoted string, whose basename never equals a bare shell name."""
    texts = [expand_word_no_glob(w) for w in args]
    i = 0
    n = len(texts)
    while i < n:
        t = texts[i]
        if t == "--":
            i += 1
            break
        if len(t) >= 2 and t[0] == "-" and t[1] != "-":
            body = t[1:]
            j = 0
            while j < len(body):
                c = body[j]
                if c == "S":
                    rest = body[j + 1 :]
                    if rest:
                        s_value = rest
                    elif i + 1 < n:
                        i += 1
                        s_value = texts[i]
                    else:
                        s_value = ""
                    words = s_value.split()
                    if words:
                        target = words[0]
                        base = os.path.basename(target.replace("\\", "/"))
                        if _is_shell_name(base):
                            raise Refused(f"refusing to run shell '{target}' via env -S")
                    break
                if c in ("u", "C"):
                    rest = body[j + 1 :]
                    if not rest and i + 1 < n:
                        i += 1  # separate NAME/DIR argument, not attached
                    break
                j += 1
            i += 1
            continue
        if len(t) > 1 and t[0] in _IDENT_START and "=" in t:
            head = t.split("=", 1)[0]
            if all(c in _IDENT_CONT for c in head[1:]):
                i += 1
                continue
        break
    if i < n:
        target = texts[i]
        base = os.path.basename(target.replace("\\", "/"))
        if _is_shell_name(base):
            raise Refused(f"refusing to run shell '{target}' via env")


def _check_target_suffix(cmd):
    name = _static_name(cmd)
    lowered = name.lower()
    for suf in _REFUSED_TARGET_SUFFIXES:
        if lowered.endswith(suf):
            raise Refused(f"refusing to run a '{suf}' target: {name}")
    base = os.path.basename(name.replace("\\", "/")) if name else name
    if _is_shell_name(base):
        raise Refused(f"refusing to run shell '{name}' as a command")
    if base.lower() == "env":
        _check_env_target(cmd.args)
    if name in BUILTIN_NAMES:
        if name == "cd" and len(cmd.args) != 1:
            raise Refused("cd requires exactly one argument")


def analyze(command, env=None, cwd=None):
    """Public linter entry point: (ok, reason)."""
    _, reason = compile_command(command, env=env)
    return (reason is None, reason)


# ---------------------------------------------------------------------------
# Executor (runs inside the worker -- see module docstring)
# ---------------------------------------------------------------------------


class CwdBox:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class ExecContext:
    def __init__(self, env, cwd_box, output_limit):
        self.env = env
        self.cwd_box = cwd_box
        self.output_limit = output_limit
        self.children = []  # list of subprocess.Popen, for cleanup bookkeeping


def _is_windows_path_sep(name):
    return "/" in name or (IS_WINDOWS and "\\" in name)


class _NotExecutable:
    """Distinct sentinel type (not bare `object()`) so pyright can narrow `resolved` to
    `str | tuple[list[str], str]` after an `is _NOT_EXECUTABLE` check, instead of the whole
    union collapsing to `object` -- that collapse is what made the `Popen(argv, ...)` call
    unable to see `argv` as `list[str]` (the pyright finding this task was handed to fix).

    `reason`, when set, replaces the generic "permission denied" message -- used by the npm/npx
    trampoline below to name the exact `*-cli.js` path it looked for and didn't find, instead of
    falling through to a generic PATH search that would answer a misleading "command not found:
    npm" even though `node` itself was located just fine."""

    def __init__(self, reason: Optional[str] = None):
        self.reason = reason


_NOT_EXECUTABLE = _NotExecutable()  # L4: a resolved file exists but lacks +x -- distinct from "not found"

NpmTrampoline = Union[tuple, str, None]
ResolvedExecutable = Union[str, tuple, None, _NotExecutable]


def _is_npm_trampoline_name(name):
    base = os.path.basename(name.replace("\\", "/")).lower()
    return base in ("npm", "npx")


def _resolve_npm_trampoline(base, env, tried: Optional[list] = None) -> NpmTrampoline:
    """H10: `npm`/`npx` resolve to `node.exe` running the sibling npm package's CLI script, never
    to `npm.cmd`/`npx.cmd` -- Popen'ing a `.cmd` implicitly starts `cmd.exe` (D2), and
    list2cmdline does not quote cmd metacharacters (BatBadBut-class argument injection).

    `tried`, when given, collects every `*-cli.js` path this function looked for and rejected --
    the caller uses it to name the exact path in a diagnostic when `node` was found but npm's own
    CLI script wasn't (see `_NotExecutable.reason` above)."""
    path_var = _env_get(env, "PATH") or ""
    for d in path_var.split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        node_exe = _check_candidate(os.path.join(d, "node"), env)
        if node_exe is None or not isinstance(node_exe, str):
            continue
        node_dir = os.path.dirname(node_exe)
        cli = os.path.join(node_dir, "node_modules", "npm", "bin", f"{base}-cli.js")
        if os.path.isfile(cli):
            return ([node_exe], cli)
        if tried is not None:
            tried.append(cli)
        exe = _check_candidate(os.path.join(d, base), env)
        if isinstance(exe, str):
            return exe  # Volta-style npm.exe/npx.exe shipped alongside node.exe
    return None


def resolve_executable(name, cwd, env) -> ResolvedExecutable:
    if _is_windows_path_sep(name):
        # M11: only a name containing a path separator is a path -- a bare name (even one
        # starting with '.') is looked up on PATH only, never against cwd.
        candidate = name if os.path.isabs(name) else os.path.join(cwd, name)
        found = _check_candidate(candidate, env)
        if found is None and not IS_WINDOWS and os.path.isfile(candidate) and not os.access(candidate, os.X_OK):
            return _NOT_EXECUTABLE
        return found
    if IS_WINDOWS and _is_npm_trampoline_name(name):
        tried: list = []
        trampoline = _resolve_npm_trampoline(os.path.basename(name).lower(), env, tried)
        if trampoline is not None:
            return trampoline
        if tried:
            # `node` was found but its sibling npm CLI script wasn't -- name the exact path we
            # looked for instead of silently falling through to the generic PATH loop below,
            # which would answer a misleading "command not found: npm" even though node exists.
            return _NotExecutable(f"npm/npx trampoline: node found but missing {tried[0]}")
    path_var = _env_get(env, "PATH") or ""
    for d in path_var.split(os.pathsep):
        # M11: skip relative AND empty PATH entries -- "never search the current directory".
        if not d or not os.path.isabs(d):
            continue
        candidate = os.path.join(d, name)
        found = _check_candidate(candidate, env)
        if found:
            return found
    return None


def _check_candidate(path, env) -> Optional[str]:
    if IS_WINDOWS:
        # H10: Win32 strips trailing dots/spaces before deciding the extension -- "x.cmd."
        # passes a naive suffix check and then runs as x.cmd. Normalize first.
        stripped = path.rstrip(". ")
        if stripped != path:
            return _check_candidate(stripped, env)
        base, ext = os.path.splitext(path)
        if ext:
            # CreateProcess only ever runs a PE image directly -- restrict candidates to
            # .COM/.EXE regardless of PATHEXT, never .BAT/.CMD (those need cmd.exe).
            if ext.upper() not in (".COM", ".EXE"):
                return None
            return os.path.abspath(path) if os.path.isfile(path) else None
        exts = _env_get(env, "PATHEXT") or ".COM;.EXE"
        ext_list = [e for e in exts.split(";") if e.upper() in (".COM", ".EXE")] or [".COM", ".EXE"]
        for cand_ext in ext_list:
            cand = path + cand_ext
            if os.path.isfile(cand):
                return os.path.abspath(cand)
        return None
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return os.path.abspath(path)
    return None


_DEV_FD_ALIASES = {"/dev/stdin": 0, "/dev/stdout": 1, "/dev/stderr": 2}


def _dev_fd_alias(path):
    """F1: `/dev/stdin|stdout|stderr` and `/dev/fd/0-2` are bash's names for the STAGE'S OWN
    stdio at this point in the redirection chain, never a path to open by itself -- opening
    `/dev/stdout` as a PATH resolves (macOS/Linux) to the worker's OWN fd 1, the answer pipe to
    the supervisor, letting a ledger command forge or erase the JSON answer (or, for
    `/dev/stdin`/`/dev/fd/0`, read the death pipe and hang until the timeout). Returns the local
    fd number to dup, or None if `path` names neither -- `/dev/fd/N` for any other N is refused
    by raising OSError (caught by the caller exactly like any other failed `open()`, so it
    becomes an ordinary status-1 redirection failure, not a crash)."""
    if path in _DEV_FD_ALIASES:
        return _DEV_FD_ALIASES[path]
    if path.startswith("/dev/fd/"):
        rest = path[len("/dev/fd/") :]
        # ASCII digits only: isdigit() would accept a non-ASCII decimal here (e.g.
        # '/dev/fd/٣'), then int() it to 3 -- silently refusing what both runtimes
        # must refuse in the same words. JS twin: /^\d+$/ + a 0-2 membership check
        # on ASCII strings.
        if rest != "" and all(c in "0123456789" for c in rest) and int(rest) in (0, 1, 2):
            return int(rest)
        raise OSError(f"refusing to open '{path}': only /dev/fd/0-2 are supported")
    return None


def _open_redir_target(redir, ctx, local_fds):
    path = expand_word_no_glob(redir.target)
    if path == "/dev/null" or (IS_WINDOWS and path.upper() == "NUL"):
        path = os.devnull
    dev_fd = _dev_fd_alias(path)
    if dev_fd is not None:
        return os.dup(local_fds[dev_fd])
    abspath = path if os.path.isabs(path) else os.path.join(ctx.cwd_box.value, path)
    if redir.kind == ">":
        return os.open(abspath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    if redir.kind == ">>":
        return os.open(abspath, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    if redir.kind == "<":
        # P7: a FIFO with no writer blocks `os.open` indefinitely -- open O_NONBLOCK (returns in
        # under a millisecond instead of hanging) then clear the flag so reads behave normally.
        nonblock = getattr(os, "O_NONBLOCK", 0)
        if nonblock and not IS_WINDOWS:
            fd = os.open(abspath, os.O_RDONLY | nonblock)
            os.set_blocking(fd, True)
            return fd
        return os.open(abspath, os.O_RDONLY)
    raise ValueError(redir.kind)


def _apply_redirs(cmd, base_fds, ctx):
    """Returns (local_fds, opened_fds_to_close_after_spawn). F11: on a failing redirection,
    every target already opened by an EARLIER redirection in this same command is closed before
    re-raising -- `_run_command_inner`'s `except OSError` only sees `opened` if we hand it back,
    so a leaked fd from redirection 1 must not survive redirection 2's failure."""
    local = dict(base_fds)
    opened = []
    try:
        for redir in cmd.redirs:
            if redir.kind == ">&":
                local[redir.fd] = local[redir.target]
            else:
                fd = _open_redir_target(redir, ctx, local)
                local[redir.fd] = fd
                opened.append(fd)
    except OSError:
        _close_quiet(opened)
        raise
    return local, opened


class _OutputSink:
    """Drains a pipe read-end into a size-bounded buffer without ever blocking a writer."""

    def __init__(self, read_fd, limit):
        self.read_fd = read_fd
        self.limit = limit
        self.buf = bytearray()
        self.truncated = False
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while True:
                try:
                    chunk = os.read(self.read_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                with self.lock:
                    room = self.limit - len(self.buf)
                    if room > 0:
                        self.buf.extend(chunk[:room])
                        if len(chunk) > room:
                            self.truncated = True
                    else:
                        self.truncated = True
        finally:
            try:
                os.close(self.read_fd)
            except OSError:
                pass

    def snapshot(self):
        with self.lock:
            return bytes(self.buf)


def _write_fd(fd, data):
    try:
        os.write(fd, data)
    except OSError:
        pass


def _close_quiet(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def run_command(cmd, fds, ctx, close_after_spawn: AbstractSet[int] = frozenset()):
    try:
        return _run_command_inner(cmd, fds, ctx, close_after_spawn)
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001 - H6: a failed stage must not hang its neighbours.
        # A stage that raises anywhere past this point (a bad redirection target we didn't
        # anticipate, NUL/lone-surrogate bytes in argv or env, EMFILE) used to leave its thread
        # dead with the pipe fds it was handed still open -- the neighbouring stage then waited
        # on a pipe that would never see EOF, until the caller's own timeout. Always close what
        # we were given and report a status instead of letting the exception vanish silently.
        _close_quiet(close_after_spawn)
        try:
            os.write(fds[2], f"cmdrun: {exc}\n".encode())
        except OSError:
            pass
        return 1


def _run_command_inner(cmd, fds, ctx, close_after_spawn):
    name = expand_word_no_glob(cmd.name)
    if name in BUILTIN_NAMES:
        return _run_builtin(cmd, name, fds, ctx)
    args = [name]
    for w in cmd.args:
        args.extend(expand_word_glob(w, ctx.cwd_box.value))
    try:
        local_fds, opened = _apply_redirs(cmd, fds, ctx)
    except OSError as exc:
        # L5: a redirection failure gets a shell-like message and status 1 -- "internal error"
        # is reserved for a genuine interpreter bug, not "the file doesn't exist".
        target = expand_word_no_glob(cmd.redirs[0].target) if cmd.redirs else "?"
        _close_quiet(close_after_spawn)
        _write_fd(fds[2], f"cmdrun: {target}: {exc.strerror or exc}\n".encode())
        return 1
    # F4: scrub every CMDRUN_* key before it can reach a CHILD process -- otherwise a nested
    # cmdrun invocation spawned as a stage (a ledger command running `python3 cmdrun.py < …`)
    # would inherit the outer worker's own test-fault-injection env vars (e.g. a forced cleanup
    # budget of 0) whenever the request has no explicit `env` of its own, silently arming the
    # nested run's hooks. `--test-hooks` already gates whether they're READ; this additionally
    # stops them being PASSED ALONG at all. CMDRUN_* is a RESERVED namespace (review round 2):
    # a caller's explicit `env: {"CMDRUN_X": ...}` is dropped here too, on purpose -- the exact
    # hook names are re-added by prefix assignment (`CMDRUN_X=1 cmd`) when a test genuinely needs
    # them, and both transports stay in agreement instead of diverging by path.
    overlay_env = {k: v for k, v in ctx.env.items() if not k.startswith("CMDRUN_")}
    for aname, avalue in cmd.assigns:
        _env_set(overlay_env, aname, expand_word_no_glob(avalue))
    resolved = resolve_executable(args[0], ctx.cwd_box.value, overlay_env)
    if isinstance(resolved, _NotExecutable):
        # `isinstance`, not `is _NOT_EXECUTABLE` -- pyright only narrows a union member away on
        # an identity check for None/enum literals, not an arbitrary singleton instance, so the
        # `is` form left `resolved` as the full 4-member union all the way to the Popen call
        # below and made `argv` look like `list[str | _NotExecutable]`.
        msg = resolved.reason if resolved.reason is not None else f"permission denied: {args[0]}"
        _write_fd(local_fds[2], f"{msg}\n".encode())
        _close_quiet(opened)
        _close_quiet(fd for fd in close_after_spawn if fd not in opened)
        return 126
    if resolved is None:
        msg = f"command not found: {args[0]}"
        if IS_WINDOWS and args[0] == "python3":
            msg += " (use `python` or `py -3`)"
        _write_fd(local_fds[2], (msg + "\n").encode())
        _close_quiet(opened)
        _close_quiet(fd for fd in close_after_spawn if fd not in opened)
        return 127
    if isinstance(resolved, tuple):
        prefix_args, resolved_path = resolved
        argv = prefix_args + [resolved_path] + args[1:]
    else:
        argv = [resolved] + args[1:]
    try:
        proc = subprocess.Popen(
            argv,
            cwd=ctx.cwd_box.value,
            env=overlay_env,
            stdin=local_fds[0],
            stdout=local_fds[1],
            stderr=local_fds[2],
            close_fds=True,
        )
    except (OSError, ValueError, UnicodeEncodeError) as exc:
        # H6: Popen can raise ValueError/UnicodeEncodeError (a NUL or lone surrogate in argv or
        # env) rather than OSError -- the old `except OSError` let those propagate uncaught.
        _write_fd(local_fds[2], f"exec failed: {exc}\n".encode())
        _close_quiet(opened)
        _close_quiet(fd for fd in close_after_spawn if fd not in opened)
        if isinstance(exc, PermissionError):
            return 126
        if isinstance(exc, FileNotFoundError):
            return 127
        return 1
    for fd in opened:
        os.close(fd)
    # fork() duplicated these into the child; OUR copy must close right away, before wait(),
    # or a fast writer (e.g. `printf x | grep x`) leaves the pipe's write end open in the
    # INTERPRETER itself after its own child has already exited, so the next stage's read
    # never sees EOF and hangs until the caller's timeout -- reproduced as `printf ... | grep
    # ... | wc -l` timing out at 5s before this fix. `opened` (this call's own redir fds) is
    # excluded since those were just closed above; only the pipeline's inter-stage pipe fds
    # land in close_after_spawn (see run_pipeline).
    for fd in close_after_spawn:
        if fd not in opened:
            try:
                os.close(fd)
            except OSError:
                pass
    ctx.children.append(proc)
    proc.wait()
    if proc.returncode is not None and proc.returncode < 0:
        return 128 - proc.returncode
    return proc.returncode


def _run_builtin(cmd, name, fds, ctx):
    args = []
    for w in cmd.args:
        args.extend(expand_word_glob(w, ctx.cwd_box.value))
    # L5: a builtin's redirection failure gets the same shell-like message and status 1
    # as an external command's. This catch used to live only in `_run_command_inner`
    # (which builtins bypass), so the OSError escaped to run_command's generic handler
    # and printed the raw `cmdrun: [Errno 1] ...` str(OSError) shape -- a message the
    # JS twin cannot reproduce (its error text is node's, not Python's) and an
    # inconsistency with the L5 shape one function over.
    try:
        local_fds, opened = _apply_redirs(cmd, fds, ctx)
    except OSError as exc:
        target = expand_word_no_glob(cmd.redirs[0].target) if cmd.redirs else "?"
        _write_fd(fds[2], f"cmdrun: {target}: {exc.strerror or exc}\n".encode())
        return 1
    try:
        if name == "cd":
            target = args[0]
            path = target if os.path.isabs(target) else os.path.join(ctx.cwd_box.value, target)
            path = os.path.normpath(path)
            if not os.path.isdir(path):
                _write_fd(local_fds[2], f"cd: no such directory: {target}\n".encode())
                return 1
            ctx.cwd_box.value = path
            return 0
        if name == "true":
            return 0
        if name == "false":
            return 1
        if name == "echo":
            no_newline = False
            rest = args
            if rest and rest[0] == "-n":
                no_newline = True
                rest = rest[1:]
            out = " ".join(rest) + ("" if no_newline else "\n")
            _write_fd(local_fds[1], out.encode())
            return 0
        if name in ("test", "["):
            a = list(args)
            if name == "[":
                if not a or a[-1] != "]":
                    _write_fd(local_fds[2], b"test: missing ']'\n")
                    return 2
                a = a[:-1]
            if len(a) != 2 or a[0] not in ("-e", "-f", "-d", "-s"):
                _write_fd(local_fds[2], b"test: usage: test -e|-f|-d|-s PATH\n")
                return 2
            flag, target = a
            path = target if os.path.isabs(target) else os.path.join(ctx.cwd_box.value, target)
            if flag == "-e":
                ok = os.path.exists(path)
            elif flag == "-f":
                ok = os.path.isfile(path)
            elif flag == "-d":
                ok = os.path.isdir(path)
            else:
                ok = os.path.isfile(path) and os.path.getsize(path) > 0
            return 0 if ok else 1
        raise AssertionError(f"unreachable builtin {name}")
    finally:
        for fd in opened:
            os.close(fd)


def run_stage(stage, fds, ctx, close_after_spawn: AbstractSet[int] = frozenset()):
    if isinstance(stage, Group):
        inner_box = CwdBox(ctx.cwd_box.value)
        inner_ctx = ExecContext(ctx.env, inner_box, ctx.output_limit)
        inner_ctx.children = ctx.children
        try:
            status = run_chain(stage.chain, fds, inner_ctx)
        except Exception as exc:  # noqa: BLE001 - F2: a `(...)` group that raises (EMFILE from a
            # nested os.pipe(), etc.) must close everything it owns and answer status 1 promptly
            # -- a `None` status must never reach the top-level answer, and a sibling pipeline
            # stage must never be left blocked reading a pipe that will now never see EOF.
            _close_quiet(close_after_spawn)
            _write_fd(fds[2], f"cmdrun: {exc}\n".encode())
            return 1
        # A group has no single "spawn" moment (it may run several commands in sequence), so
        # unlike run_command it can only release these once it is entirely done using them.
        _close_quiet(close_after_spawn)
        return status
    return run_command(stage, fds, ctx, close_after_spawn)


def run_pipeline(pipeline, fds, ctx):
    stages = pipeline.stages
    if len(stages) == 1:
        return run_stage(stages[0], fds, ctx)
    n = len(stages)
    pipes = []
    try:
        for _ in range(n - 1):
            pipes.append(os.pipe())
    except OSError as exc:
        # F2: `os.pipe()` can EMFILE partway through -- close whatever pipes we already made
        # instead of leaking them (the leak alone can make a SIBLING stage fail with EMFILE too,
        # masking this failure by luck).
        for r, w in pipes:
            _close_quiet((r, w))
        _write_fd(fds[2], f"cmdrun: pipe: {exc}\n".encode())
        return 1
    statuses: list = [None] * n
    threads = []
    outer_fds = set(fds.values())

    def worker(idx):
        stage_fds = dict(fds)
        if idx > 0:
            stage_fds[0] = pipes[idx - 1][0]
        if idx < n - 1:
            stage_fds[1] = pipes[idx][1]
        # Only the pipe endpoints THIS stage introduced are ours to close once the stage no
        # longer needs them -- never an fd borrowed from the outer scope (`fds`), which may
        # still be needed by a sibling pipeline later in the same `&&` chain.
        private = {v for v in (stage_fds[0], stage_fds[1]) if v not in outer_fds}
        try:
            statuses[idx] = run_stage(stages[idx], stage_fds, ctx, private)
        except Exception as exc:  # noqa: BLE001 - F2: belt-and-braces -- run_stage already
            # converts a Group's own exception into a status, but this thread must never die
            # silently for any OTHER reason and leave `statuses[idx]` at None forever with its
            # private pipe fds still open.
            _close_quiet(private)
            _write_fd(fds[2], f"cmdrun: {exc}\n".encode())
            statuses[idx] = 1

    # Run stages left-to-right but concurrently: external commands are already
    # concurrent via the OS once Popen'd; builtins/groups run inline on our
    # thread. Using one Python thread per stage keeps everything uniform and
    # avoids deadlock when two stages are both "inline" (e.g. two groups).
    for idx in range(n):
        t = threading.Thread(target=worker, args=(idx,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    # H7: every private pipe fd above is closed EXACTLY ONCE, by the stage that owned it (via
    # `close_after_spawn`/`private`). A second closing pass here -- what v1 did -- closes
    # whatever fd NUMBER a concurrently running SIBLING pipeline's `os.pipe()` has since reused
    # (fd numbers are recycled immediately), silently truncating or corrupting that pipeline's
    # own pipe (reproduced: a nested `( … ) | ( … )` losing its tail, status 0). Deleting this
    # loop, not adding tracking, is the fix -- each fd already has exactly one owner.
    status = 0
    for s in statuses:
        if s != 0:
            status = s
    return status


def run_chain(chain, fds, ctx):
    status = 0
    for pipeline in chain.pipelines:
        status = run_pipeline(pipeline, fds, ctx)
        if status != 0:
            break
    return status


# ---------------------------------------------------------------------------
# Worker (`cmdrun.py --exec`): one process group, no deadline, no fork.
# ---------------------------------------------------------------------------


def _emit_and_die(answer):
    """The worker's one and only exit path, on every branch: write the answer, then kill our
    OWN process group (which we are the leader of) before actually exiting. That single act
    closes H3 (a success-path descendant like a detached `subprocess.Popen` grandchild used to
    outlive the interpreter that spawned it) as a side effect of always being the worker's last
    act, success or failure -- there is no separate "cleanup on success" code path to forget.

    Writes the payload with raw `os.write` in a loop, bypassing `sys.stdout`'s buffering, so
    every byte is CONFIRMED delivered to the pipe before `killpg(0, SIGKILL)` runs -- SIGKILL is
    uncatchable and hits this very process too, so any byte still sitting in a Python-level
    buffer at that instant would never reach the reader (a partial/absent JSON line the
    supervisor could easily mistake for "worker exited without a valid answer")."""
    try:
        payload = memoryview((json.dumps(answer) + "\n").encode("utf-8"))
        fd = sys.stdout.fileno()
        while payload:
            try:
                n = os.write(fd, payload)
            except InterruptedError:
                continue
            payload = payload[n:]
    except (BrokenPipeError, OSError, ValueError):
        pass
    if not IS_WINDOWS:
        # W2: `os.killpg` is POSIX-only. On Windows there is no per-worker group to kill here --
        # the supervisor's Job Object has KILL_ON_JOB_CLOSE set, so closing ITS job handle (which
        # it does unconditionally, success or failure) is what reaps any escaped descendant; an
        # `AttributeError` here would otherwise make even the SUCCESS path exit via traceback.
        try:
            os.killpg(0, signal.SIGKILL)
        except OSError:
            pass
    os._exit(0)


def _death_watch():
    """Runs on a daemon thread for the worker's whole life. The worker's stdin is the death
    pipe: the supervisor holds the write end open for as long as IT lives, so the moment the
    supervisor dies for ANY reason -- a caller's SIGKILL, a crash, its own deadline -- the
    kernel closes that pipe and this read returns EOF. That is how a caller's SIGKILL of the
    supervisor reaches a running command that would otherwise be orphaned in its own session
    (H4): the supervisor is never in the loop for the kill itself, only for detecting its own
    death."""
    try:
        while True:
            chunk = sys.stdin.buffer.read(65536)
            if not chunk:
                break
    except OSError:
        pass
    if not IS_WINDOWS:
        # W2: see _emit_and_die -- on Windows the supervisor's job handle close (KILL_ON_JOB_CLOSE)
        # is what kills the job when the supervisor dies; this thread's only job on that platform
        # is to notice and exit, not to kill anything itself.
        try:
            os.killpg(0, signal.SIGKILL)
        except OSError:
            pass
    os._exit(1)


def _worker_main():
    tree_kill = "job" if IS_WINDOWS else "process_group"
    if TEST_HOOKS and os.environ.get("CMDRUN_TEST_WORKER_CRASH_BEFORE_READ"):
        # Test-only fault injection (finding 7), gated behind --test-hooks (F4): the worker dies
        # before it ever reads its request line -- proves the supervisor still answers with
        # exactly ONE `internal_error` JSON line ("worker exited without a valid answer"), never
        # a crash with two lines or a hang. Never armed outside a test.
        os._exit(1)
    raw = sys.stdin.buffer.readline()
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request is not an object")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        _emit_and_die({"status": None, "timed_out": False, "internal_error": True,
                       "reason": f"worker: bad request: {exc}", "tree_kill": tree_kill})
        return
    threading.Thread(target=_death_watch, daemon=True).start()
    command = request["command"]
    cwd = request["cwd"]
    env = request["env"] if "env" in request else dict(os.environ)
    mode = request.get("mode", "gate")
    output_limit = request.get("output_limit", 1_000_000)

    chain, reason = compile_command(command, env=env)
    if reason is not None:
        _emit_and_die({"status": None, "refused": True, "reason": reason, "timed_out": False,
                       "tree_kill": tree_kill})
        return

    devnull_r = os.open(os.devnull, os.O_RDONLY)
    sinks: Optional[tuple] = None
    if mode == "gate":
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        sinks = (_OutputSink(out_r, output_limit), _OutputSink(err_r, output_limit))
        fds = {0: devnull_r, 1: out_w, 2: err_w}
    else:
        devnull_w = os.open(os.devnull, os.O_WRONLY)
        fds = {0: devnull_r, 1: devnull_w, 2: devnull_w}
    ctx = ExecContext(env, CwdBox(cwd), output_limit)
    try:
        status = run_chain(chain, fds, ctx)
    except Exception as exc:  # noqa: BLE001 - must never propagate as an uncaught crash
        _close_quiet(fds.values())
        # F8: `internal_error: true` must be set on every genuinely internal fault -- this is the
        # flag a caller keys on to tell "the command legitimately exited 1" from "the interpreter
        # itself broke" (misc2.py: a 40-stage EMFILE pipeline used to answer plain status 1 here).
        _emit_and_die({"status": 1, "timed_out": False, "internal_error": True,
                       "reason": f"internal error: {exc}", "tree_kill": tree_kill})
        return
    if status is None:
        # F2 safety net: every real code path now converts a failing stage into an int status
        # before it gets here (run_stage's Group branch, run_pipeline's pipe/thread guards), but
        # `None` reaching the answer is exactly the defect this whole finding is about -- never
        # let a gap anywhere else silently forge a `status: null` with no `reason`.
        _close_quiet(fds.values())
        _emit_and_die({"status": None, "timed_out": False, "internal_error": True,
                       "reason": "internal error: no stage produced a status",
                       "tree_kill": tree_kill})
        return
    for fd in set(fds.values()):
        try:
            os.close(fd)
        except OSError:
            pass
    answer = {"status": status, "timed_out": False, "tree_kill": tree_kill}
    if sinks is not None:
        # F3: ONE combined 100ms bound for both readers together, not 0.5s EACH -- the writers
        # are already done (every stage has exited), so this only waits for the readers to drain
        # what's left of one pipe buffer's worth of bytes; never close their fds afterwards
        # (they're daemon threads and may still be mid-read on a stubborn escapee, and closing an
        # fd a thread might still be blocked on is exactly the H7 fd-reuse hazard in a new place).
        _join_deadline = time.monotonic() + 0.1
        for s in sinks:
            s.thread.join(timeout=max(0.0, _join_deadline - time.monotonic()))
        answer["stdout"] = sinks[0].snapshot().decode("utf-8", errors="replace")
        answer["stderr"] = sinks[1].snapshot().decode("utf-8", errors="replace")
        answer["truncated"] = sinks[0].truncated or sinks[1].truncated
    _emit_and_die(answer)


# ---------------------------------------------------------------------------
# Supervisor: request validation, spawning the worker, and the one timekeeper.
# ---------------------------------------------------------------------------


def validate_request(raw: bytes):
    """Returns (validated_request_dict, error_reason). Never raises -- M1: a malformed request
    (wrong JSON type, bad UTF-8, an out-of-range field) always becomes a `bad_request` answer,
    never an uncaught traceback with no JSON printed at all."""
    if len(raw) > MAX_REQUEST_BYTES:
        return None, "request exceeds 16 MiB"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"invalid UTF-8: {exc}"
    if not text.strip():
        return None, "empty request"
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as exc:
        # F5: `json.loads` raises more than `JSONDecodeError` -- a 5,001-digit integer literal
        # hits Python's int-string-conversion limit (`ValueError`), and a deeply nested `[[[…`
        # payload can exhaust the interpreter's recursion limit inside the parser itself
        # (`RecursionError`, not a subclass of `ValueError`). `JSONDecodeError` IS a `ValueError`
        # subclass, so this one clause also covers the ordinary malformed-JSON case.
        return None, f"invalid JSON: {exc}"
    if not isinstance(obj, dict):
        return None, "request must be a JSON object"
    command = obj.get("command")
    if not isinstance(command, str) or not command:
        return None, "missing or invalid 'command'"
    cwd = obj.get("cwd")
    try:
        if cwd is None:
            cwd = os.getcwd()
        else:
            if not isinstance(cwd, str) or not cwd:
                return None, "invalid 'cwd'"
            if not os.path.isabs(cwd):
                cwd = os.path.abspath(os.path.join(os.getcwd(), cwd))
    except OSError as exc:
        # F5: `os.getcwd()` raises if the supervisor's own working directory was deleted out from
        # under it -- one `bad_request` line, never a bare traceback with no JSON printed at all.
        return None, f"cannot determine cwd: {exc}"
    if not os.path.isdir(cwd):
        return None, f"cwd does not exist: {cwd}"
    env_present = "env" in obj
    env = obj.get("env")
    if env_present:
        if not isinstance(env, dict):
            return None, "invalid 'env': must be an object"
        for k, v in env.items():
            if not isinstance(k, str) or not isinstance(v, str) or "\x00" in k or "\x00" in v:
                return None, "invalid 'env': keys/values must be NUL-free strings"
        if IS_WINDOWS:
            # W3: Windows env keys are case-insensitive, but two JSON object keys differing only
            # in case (`"Path"` and `"PATH"`) are two distinct dict entries -- normalize here so
            # the child process never sees both spellings at once (later one wins).
            normalized: dict = {}
            for k, v in env.items():
                for existing in list(normalized.keys()):
                    if existing.upper() == k.upper():
                        del normalized[existing]
                        break
                normalized[k] = v
            env = normalized
    mode = obj.get("mode", "gate")
    if mode not in ("ledger", "gate"):
        return None, "invalid 'mode': must be 'ledger' or 'gate'"
    timeout_ms = obj.get("timeout_ms", 30000)
    # M3/P6: json.loads gives a `float` for `1e3`/`1000.0`/NaN/Infinity, and `bool` passes an
    # `int` isinstance check in Python -- reject both explicitly rather than let `NaN >=
    # deadline` silently evaluate to False forever.
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        return None, "invalid 'timeout_ms': must be an integer"
    if not (1 <= timeout_ms <= 86_400_000):
        return None, "invalid 'timeout_ms': out of range 1..86400000"
    output_limit = obj.get("output_limit", 1_000_000)
    if isinstance(output_limit, bool) or not isinstance(output_limit, int):
        return None, "invalid 'output_limit': must be an integer"
    if not (0 <= output_limit <= MAX_OUTPUT_LIMIT):
        return None, f"invalid 'output_limit': out of range 0..{MAX_OUTPUT_LIMIT}"
    result = {"command": command, "cwd": cwd, "mode": mode, "timeout_ms": timeout_ms,
              "output_limit": output_limit}
    if env_present:
        result["env"] = env
    return result, None


class _SignalWatch:
    """Records SIGTERM/SIGHUP/SIGINT without acting on them directly -- the supervisor's wait
    loop polls `.got` at least every 100ms, so the deadline/signal/answer race stays in one
    place (the loop) instead of running kill logic from inside a signal handler."""

    # W1: `signal.SIGHUP` does not exist on Windows ("Availability: Unix") -- referencing it
    # directly at class-definition time raised `AttributeError` before a single line of JSON
    # could be printed, so the module could not even be IMPORTED on Windows. `getattr` + a filter
    # keeps every platform's own subset.
    SIGNALS = tuple(
        s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None),
                    getattr(signal, "SIGINT", None))
        if s is not None
    )

    def __init__(self):
        self.got = None
        self._prev = {}
        for s in self.SIGNALS:
            self._prev[s] = signal.signal(s, self._handle)

    def _handle(self, signum, _frame):
        if self.got is None:
            self.got = signum

    def close(self):
        for s, h in self._prev.items():
            signal.signal(s, h)


def _wait_for_worker_answer(proc, deadline, signalled):
    """Returns (answer_dict_or_None, timed_out, signum_or_None)."""
    q: "queue.Queue[bytes]" = queue.Queue(maxsize=1)

    def _reader():
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline()
        except OSError:
            line = b""
        try:
            q.put_nowait(line)
        except queue.Full:
            pass

    threading.Thread(target=_reader, daemon=True).start()

    while True:
        if signalled.got is not None:
            return None, False, signalled.got
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, True, None
        try:
            line = q.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            continue
        if not line:
            return None, False, None  # worker exited without writing a full line
        try:
            answer = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, False, None
        if not _validate_worker_answer(answer):
            # F1: the answer line crosses a trust boundary a ledger command can reach (before the
            # /dev/stdout redirection hole was closed, a stage could forge or erase it outright).
            # Reject anything that isn't the EXACT shape the worker is contracted to produce --
            # this is treated identically to "no answer at all", which the caller already turns
            # into `internal_error` once the group is confirmed dead.
            return None, False, None
        return answer, False, None


_ANSWER_KNOWN_KEYS = {
    "status", "timed_out", "truncated", "tree_kill", "refused", "bad_request",
    "internal_error", "leaked", "reason", "stdout", "stderr", "pgid",
}


def _validate_worker_answer(answer) -> bool:
    """F1: known keys only, and every value's TYPE checked -- a forged `{"status": 0}` or a
    `{"name": "x", "version": "1.0.0"}` (no `status` key at all, from a ledger command that
    redirected an unrelated file onto its own answer channel) must never be mistaken for a real
    answer just because it happens to be valid JSON."""
    if not isinstance(answer, dict):
        return False
    if not set(answer.keys()) <= _ANSWER_KNOWN_KEYS:
        return False
    if "status" not in answer:
        return False
    status = answer["status"]
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
        return False
    for key in ("timed_out", "truncated", "refused", "bad_request", "internal_error", "leaked"):
        if key in answer and not isinstance(answer[key], bool):
            return False
    if "reason" in answer and not isinstance(answer["reason"], str):
        return False
    if "tree_kill" in answer and answer["tree_kill"] not in ("process_group", "job"):
        return False
    if "stdout" in answer and not isinstance(answer["stdout"], str):
        return False
    if "stderr" in answer and not isinstance(answer["stderr"], str):
        return False
    if "pgid" in answer and (isinstance(answer["pgid"], bool) or not isinstance(answer["pgid"], int)):
        return False
    return True


def _kill_group_until_dead(pid, deadline):
    """Repeats `killpg(pid, SIGKILL)` until the kernel reports the group gone (ESRCH), reaping
    the leader as we go. H1/H2/advisor-P1: on macOS a zombie-only group answers EPERM, not
    ESRCH or 0 -- EPERM must mean "reap what's mine and retry", never "done" and never "error",
    or the loop's only exit condition never fires and a `killpg`-versus-fork race (the SIGKILL
    landing while Popen's fork is still in flight) can leave the real descendant running with a
    reported timeout. Returns True ('leaked') if the deadline is exceeded without confirmation."""
    while True:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return False  # ESRCH: the whole group is confirmed gone.
        except PermissionError:
            pass  # EPERM (macOS, zombie-only group): reap below, then retry.
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.01)


def _reap_bounded(proc, timeout):
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _run_posix_supervisor(req, signalled):
    worker_argv = [sys.executable, os.path.abspath(__file__), "--exec"]
    if TEST_HOOKS:
        worker_argv.append("--test-hooks")
    _test_delay_ms = os.environ.get("CMDRUN_TEST_DELAY_BEFORE_SPAWN_MS") if TEST_HOOKS else None
    if _test_delay_ms:
        # Test-only fault injection (finding 7), gated behind --test-hooks (F4): widens the
        # window between "the supervisor process exists" and "the worker process exists" so a
        # test can prove a signal landing in exactly that gap still never produces more than one
        # answer line. Never armed outside a test.
        time.sleep(int(_test_delay_ms) / 1000.0)
    try:
        proc = subprocess.Popen(
            worker_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return {"status": None, "timed_out": False, "internal_error": True,
                "reason": f"failed to spawn worker: {exc}", "tree_kill": "process_group"}

    line = (json.dumps(req) + "\n").encode("utf-8")
    try:
        assert proc.stdin is not None
        proc.stdin.write(line)
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass  # the read loop below will notice the worker died and report it

    deadline = time.monotonic() + req["timeout_ms"] / 1000.0
    answer, timed_out, sig = _wait_for_worker_answer(proc, deadline, signalled)

    if answer is not None:
        _reap_bounded(proc, _cleanup_budget_s())
        answer.setdefault("tree_kill", "process_group")
        answer.setdefault("timed_out", False)
        return answer

    # F7: the reap that follows a confirmed-dead-or-leaked group counts INSIDE the same cleanup
    # budget as the kill loop itself, never a separate fixed 0.5s tacked on afterward -- the
    # old code could push the total past the spec's single "deadline + 1.5s" budget whenever the
    # worker was slow to reap (e.g. stuck reaping a zombie under EPERM the whole time).
    cleanup_deadline = time.monotonic() + _cleanup_budget_s()
    leaked = _kill_group_until_dead(proc.pid, cleanup_deadline)
    _reap_bounded(proc, max(0.0, cleanup_deadline - time.monotonic()))

    if leaked:
        # M6/forced-cleanup-exhaustion: never answer silently when the deadline runs out before
        # the group is confirmed dead -- `pgid` names exactly which group a caller must go clean
        # up by hand (it equals `proc.pid`: the worker is its own session/group leader). F7: name
        # the EPERM-zombie possibility explicitly -- the named group may hold only a harmless
        # zombie while the real survivor escaped into another session/group entirely.
        return {"status": None, "timed_out": timed_out, "leaked": True, "pgid": proc.pid,
                "reason": f"failed to reap process group {proc.pid} in time "
                          "(the group may hold only a zombie -- a real survivor may have "
                          "escaped into another session)",
                "tree_kill": "process_group"}
    if sig is not None:
        return {"status": 143, "timed_out": False, "tree_kill": "process_group"}
    if timed_out:
        return {"status": 124, "timed_out": True, "tree_kill": "process_group"}
    return {"status": None, "timed_out": False, "internal_error": True,
            "reason": "worker exited without a valid answer", "tree_kill": "process_group"}


# ---------------------------------------------------------------------------
# Windows Job Object supervisor (written per spec; not exercised on this host --
# reports/no-bash/20260924_195401+0200-cmdrun-py-attack.md and the advisor review both flag
# every Windows-only claim below as unproven; the spec asks for a Windows CI proof before step 3)
# ---------------------------------------------------------------------------

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_JobObjectBasicAccountingInformation = 1
_PROCESS_ALL_ACCESS = 0x1F0FFF


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):  # pragma: no cover - Windows only
    """W3: queried after `TerminateJobObject` to CONFIRM the job is actually empty -- the old
    code trusted `TerminateJobObject`'s return value alone (never even checked) and never
    verified anything died, so a job that failed to terminate (or terminated everything except
    one wedged process) would never answer `leaked`, contradicting the spec's "never silently"."""

    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _win_kernel32():  # pragma: no cover - requires Windows to exercise
    """One `use_last_error=True` handle for every call below -- `ctypes.windll.kernel32` (the
    old form) does NOT set `use_last_error`, and `ctypes.GetLastError()` reads whichever
    thread's error slot last happened to be touched process-wide, not necessarily this call's
    (a documented ctypes gotcha). `WinDLL(..., use_last_error=True)` + `ctypes.get_last_error()`
    is the pair the ctypes docs recommend instead -- M12.

    `getattr`, not `ctypes.WinDLL` directly: typeshed declares `WinDLL`/`get_last_error` only
    under a `sys.platform == "win32"` guard, so pyright (running on this non-Windows dev host,
    per `pyproject.toml`'s `[tool.pyright]`) reports a real attribute-access error on the direct
    form even though this whole function is itself `sys.platform`-gated at the only call site."""
    win_dll_cls = getattr(ctypes, "WinDLL")  # noqa: B009 - see docstring
    return win_dll_cls("kernel32", use_last_error=True)


def _win_last_error(call_name: str) -> str:  # pragma: no cover - requires Windows to exercise
    get_last_error = getattr(ctypes, "get_last_error")  # noqa: B009 - see _win_kernel32 docstring
    return f"{call_name} failed (WinError {get_last_error()})"


def _win_create_job_object():  # pragma: no cover - requires Windows to exercise
    """Returns (job_handle_or_None, reason_or_None) -- the reason NAMES the failing WinAPI call
    (M12) instead of the caller only ever seeing a bare None."""
    if sys.platform != "win32":
        return None, "not running on win32"
    try:
        kernel32 = _win_kernel32()
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None, _win_last_error("CreateJobObjectW")

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        ]
        # M12: every ctypes return value is checked -- a silent 0 return (no exception without
        # `use_last_error`) used to look identical to success.
        if not kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            reason = _win_last_error("SetInformationJobObject")
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle(job)
            return None, reason
        return job, None
    except OSError as exc:
        return None, f"CreateJobObjectW/SetInformationJobObject raised: {exc}"


def _win_assign_job(job, pid):  # pragma: no cover - requires Windows to exercise
    """Returns (ok, reason_or_None) -- see `_win_create_job_object`."""
    if sys.platform != "win32":
        return False, "not running on win32"
    try:
        kernel32 = _win_kernel32()
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if not handle:
            return False, _win_last_error("OpenProcess")
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        ok = bool(kernel32.AssignProcessToJobObject(job, handle))
        reason = None if ok else _win_last_error("AssignProcessToJobObject")
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(handle)
        return ok, reason
    except OSError as exc:
        return False, f"OpenProcess/AssignProcessToJobObject raised: {exc}"


def _win_terminate_process(pid):  # pragma: no cover - requires Windows to exercise
    if sys.platform != "win32":
        return
    try:
        kernel32 = _win_kernel32()
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if handle:
            kernel32.TerminateProcess.restype = wintypes.BOOL
            kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle(handle)
    except OSError:
        pass


def _win_kill_job(job, settle_budget_s):  # pragma: no cover - requires Windows to exercise
    """Returns (confirmed_empty, reason_or_None). W3: check `TerminateJobObject`'s own return
    value, then QUERY the job's active-process count afterward -- a caller must never believe
    cleanup succeeded just because the API call didn't raise. Contract item 5 (review round 2):
    TerminateJobObject INITIATES teardown and returns before it finishes, so an immediate
    accounting query can still see `ActiveProcesses > 0` on a job that is in fact dying -- a
    bounded settle/retry window (50 ms between queries) separates a real survivor from teardown
    in progress before answering `leaked: true`. The window is the caller's cleanup budget
    (re-derived at the call site, where no time has yet elapsed), not a hardcoded constant: a
    caller with a 0 budget (the test hook forces exactly that) must answer `leaked: true`
    immediately rather than silently waiting 1 s and possibly confirming empty against the
    contract it is being tested for."""
    if sys.platform != "win32":
        return True, None
    try:
        kernel32 = _win_kernel32()
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        if not kernel32.TerminateJobObject(job, 1):
            return False, _win_last_error("TerminateJobObject")
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
        ]
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        settle_deadline = time.monotonic() + settle_budget_s
        while True:
            queried = kernel32.QueryInformationJobObject(
                job, _JobObjectBasicAccountingInformation, ctypes.byref(info),
                ctypes.sizeof(info), None
            )
            if not queried:
                return False, _win_last_error("QueryInformationJobObject")
            if info.ActiveProcesses == 0:
                return True, None
            if time.monotonic() >= settle_deadline:
                return False, (f"job still has {info.ActiveProcesses} active process(es) "
                               f"after terminate and a {settle_budget_s:.1f}s settle window")
            time.sleep(0.05)
    except OSError as exc:
        return False, f"TerminateJobObject/QueryInformationJobObject raised: {exc}"


def _win_close_job(job):  # pragma: no cover - requires Windows to exercise
    if sys.platform != "win32":
        return
    try:
        kernel32 = _win_kernel32()
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(job)
    except OSError:
        pass


def _run_windows_supervisor(req, signalled):  # pragma: no cover - requires Windows to exercise
    job, job_reason = _win_create_job_object()
    if job is None:
        return {"status": None, "timed_out": False, "internal_error": True,
                "reason": f"failed to create a Windows Job Object: {job_reason}", "tree_kill": "job"}
    worker_argv = [sys.executable, os.path.abspath(__file__), "--exec"]
    if TEST_HOOKS:
        worker_argv.append("--test-hooks")
    try:
        proc = subprocess.Popen(worker_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        _win_close_job(job)
        return {"status": None, "timed_out": False, "internal_error": True,
                "reason": f"failed to spawn worker: {exc}", "tree_kill": "job"}
    # H9: assign BEFORE writing the request line, so the worker cannot have spawned a stage
    # yet -- every descendant then inherits job membership from birth, with no CREATE_SUSPENDED
    # / NtResumeProcess window to get wrong. M12: a failed assignment terminates the already-
    # spawned worker (it would otherwise run un-jobbed, escaping the cleanup this whole
    # supervisor exists to guarantee) instead of merely reporting the failure and leaking it.
    assigned, assign_reason = _win_assign_job(job, proc.pid)
    if not assigned:
        _win_terminate_process(proc.pid)
        _reap_bounded(proc, 1.5)
        _win_close_job(job)
        return {"status": None, "timed_out": False, "internal_error": True,
                "reason": f"AssignProcessToJobObject failed: {assign_reason}", "tree_kill": "job"}

    line = (json.dumps(req) + "\n").encode("utf-8")
    try:
        assert proc.stdin is not None
        proc.stdin.write(line)
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass

    deadline = time.monotonic() + req["timeout_ms"] / 1000.0
    answer, timed_out, sig = _wait_for_worker_answer(proc, deadline, signalled)

    try:
        if answer is not None:
            _reap_bounded(proc, _cleanup_budget_s())
            answer.setdefault("tree_kill", "job")
            answer.setdefault("timed_out", False)
            return answer
        # F7 (parity with the POSIX path): the reap counts INSIDE the same cleanup budget as the
        # kill itself, never a separate bound tacked on afterward. The W3 settle window shares
        # that same budget -- the settle loop runs against the caller's remaining time.
        cleanup_deadline = time.monotonic() + _cleanup_budget_s()
        confirmed_empty, kill_reason = _win_kill_job(job, _cleanup_budget_s())
        _reap_bounded(proc, max(0.0, cleanup_deadline - time.monotonic()))
        if not confirmed_empty:
            # W3: TerminateJobObject's return value and the post-terminate active-process count
            # are both checked -- never silently believe the job is clean.
            return {"status": None, "timed_out": timed_out, "leaked": True,
                    "reason": f"failed to confirm the Windows job is empty: {kill_reason}",
                    "tree_kill": "job"}
        if sig is not None:
            return {"status": 143, "timed_out": False, "tree_kill": "job"}
        if timed_out:
            return {"status": 124, "timed_out": True, "tree_kill": "job"}
        return {"status": None, "timed_out": False, "internal_error": True,
                "reason": "worker exited without a valid answer", "tree_kill": "job"}
    finally:
        _win_close_job(job)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _print_answer(answer):
    try:
        print(json.dumps(answer))
    except BrokenPipeError:
        pass  # L2: the caller closed our stdout before we answered -- exit quietly, not a traceback.


def main():
    if not IS_WINDOWS and hasattr(signal, "SIGCHLD"):
        # SIGCHLD reset: an inherited SIG_IGN (some shells/launchers set it, and it survives
        # exec) makes CPython's `Popen._try_wait` take its `ChildProcessError` branch and report
        # returncode 0 for EVERY child, silently turning a failing command into a false success.
        # Reset explicitly rather than trust the caller's disposition -- this covers BOTH the
        # supervisor and the worker, since `_worker_main()` is also reached through this same
        # `main()` entry point, before either one calls `subprocess.Popen`/`.wait()`.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    if len(sys.argv) > 1 and sys.argv[1] == "--exec":
        _worker_main()
        return
    tree_kill = "job" if IS_WINDOWS else "process_group"
    # F6: install the signal watch FIRST THING, before any validation/spawn work, and keep it
    # installed through the whole cleanup phase below (closed only in the `finally`) -- the old
    # code installed it only just before the wait loop, leaving ~70ms (startup/validation/spawn)
    # and ~20ms (cleanup) windows where SIGTERM/SIGHUP/SIGINT killed the supervisor via Python's
    # default disposition (a bare traceback or silent death) instead of answering 143. Deliberate
    # trade-off (review round 2): the blocking `sys.stdin.buffer.read()` below is NOT interruptible
    # by this watch -- a supervisor opened with no request and SIGTERMed hangs until stdin closes
    # rather than answering 143 immediately. Accepted: the spec already makes the CALLER enforce
    # the timeout and kill the interpreter's group, so a supervisor with no request is a
    # misbehaving caller, and installing the watch late (the old shape) left the larger
    # spawn/cleanup windows unprotected instead.
    signalled = _SignalWatch()
    try:
        try:
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            req, err = validate_request(raw)
        except (ValueError, RecursionError) as exc:
            # F5: validate_request is now internally hardened to never raise these, but this is
            # the last line of defense the docstring's "never raises" contract depends on --
            # exactly one bad_request line, never a bare traceback with zero JSON printed.
            _print_answer({"status": None, "bad_request": True, "reason": f"invalid request: {exc}",
                            "timed_out": False, "tree_kill": tree_kill})
            return
        if err is not None or req is None:
            # `req is None` is unreachable in practice (validate_request never returns (None, None)
            # -- see its docstring), but pyright can't see that contract across the tuple return, so
            # this keeps `req` narrowed to `dict` below instead of `dict | None`.
            _print_answer({"status": None, "bad_request": True, "reason": err, "timed_out": False,
                            "tree_kill": tree_kill})
            return
        env_for_analysis: Union[dict, os._Environ] = req["env"] if "env" in req else os.environ
        _, reason = compile_command(req["command"], env=env_for_analysis)
        if reason is not None:
            _print_answer({"status": None, "refused": True, "reason": reason, "timed_out": False,
                            "tree_kill": tree_kill})
            return
        try:
            if IS_WINDOWS:
                answer = _run_windows_supervisor(req, signalled)
            else:
                answer = _run_posix_supervisor(req, signalled)
        except Exception as exc:  # noqa: BLE001 - the one place we truly must never crash
            answer = {"status": None, "timed_out": False, "internal_error": True,
                      "reason": f"internal error: {exc}", "tree_kill": tree_kill}
        answer.setdefault("timed_out", False)
        _print_answer(answer)
    finally:
        signalled.close()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    finally:
        # L2, continued: `_print_answer` already swallows the BrokenPipeError from `print()`
        # itself, but CPython's own interpreter-shutdown flush of stdout raises it AGAIN and
        # prints an "Exception ignored in: <_io.TextIOWrapper ...>" line to stderr with exit
        # status 120 -- not caught by any `except` in this process because it happens after
        # `main()` has already returned. This is the standard idiom from the Python docs' SIGPIPE
        # note: redirect stdout to devnull before falling off the end, so the shutdown flush has
        # nothing left to fail on.
        stdout_fd = 1  # captured before close(): a closed TextIOWrapper's .fileno() raises too.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        finally:
            try:
                devnull_fd = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull_fd, stdout_fd)
            except OSError:
                pass
