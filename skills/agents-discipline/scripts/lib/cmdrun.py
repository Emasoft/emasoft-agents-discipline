"""A shell-free command interpreter: hand-written tokenizer/parser/analyzer/executor.

Why this exists (TRDD in design/tasks/, spec docs_dev/no-bash-spec.md): the user forbade any
bash/shell dependency in this plugin, including on Windows. `subprocess.run(cmd, shell=True)`
spawns `/bin/sh` or `cmd.exe` -- exactly what is forbidden. This module reimplements the small
supported grammar (see docs_dev/no-bash-spec.md) directly against `subprocess.Popen` with
`shell=False`, so no shell process is ever created on any platform.

Entry point: `python cmdrun.py` reads one JSON request line from stdin and prints exactly one
JSON answer line to stdout, never raising an uncaught exception and never hanging past
timeout_ms (+ a small grace period) -- see `main()`.

`analyze(command, env=None, cwd=None) -> (ok, reason)` is exposed standalone so a linter (a
future ledger-check/gate-check step) can ask "would this be refused" without executing anything.
"""

from __future__ import annotations

import ctypes
import glob as glob_mod
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import AbstractSet, Optional

if sys.platform != "win32":
    # `python cmdrun.py` puts this file's own directory (scripts/lib/) at
    # sys.path[0] regardless of the caller's cwd, so the bare module name
    # resolves whether we're invoked from scripts/lib/, scripts/, or from a
    # `-m` import inside a test that adds this same directory to sys.path.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from process_tree import terminate_process_tree
else:  # pragma: no cover - exercised only on Windows
    terminate_process_tree = None

IS_WINDOWS = sys.platform == "win32"

# ponytail: bounded, not unlimited -- an attacker-controlled command string must never make the
# parser do unbounded work. These caps are generous for real use and small enough that a fuzzer
# throwing 10k-stage pipelines or megabyte argv strings at us degrades to a fast refusal instead
# of a slow crawl.
MAX_COMMAND_LENGTH = 262_144
MAX_GROUP_DEPTH = 32
MAX_PIPELINE_STAGES = 512
MAX_TOKENS = 20_000

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


def _expand_dollar(s, i, n, env, out_literal_parts):
    """s[i] == '$'. Returns new index. Appends (text, literal=True) parts."""
    i += 1
    if i < n and s[i] == "{":
        j = s.index("}", i + 1) if "}" in s[i + 1 :] else -1
        if j == -1:
            raise Refused("unterminated ${...}")
        content = s[i + 1 : j]
        if content and content[0] in _IDENT_START and all(c in _IDENT_CONT for c in content[1:]):
            out_literal_parts.append((_env_get(env, content), True))
            return j + 1
        raise Refused(f'unsupported "${{{content}}}" operator')
    if i < n and s[i] in _IDENT_START:
        j = i
        while j < n and s[j] in _IDENT_CONT:
            j += 1
        name = s[i:j]
        out_literal_parts.append((_env_get(env, name), True))
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


def tokenize(command, env):
    if len(command) > MAX_COMMAND_LENGTH:
        raise Refused("command too long")
    if "\n" in command or "\r" in command:
        raise Refused("newline / multi-line command")
    n = len(command)
    tokens = []
    i = 0
    at_word_start = True  # true right after whitespace/operator/start
    while i < n:
        c = command[i]
        if c in " \t":
            i += 1
            at_word_start = True
            continue
        if c == "#" and at_word_start:
            break  # comment to end of line
        # fd-prefixed or bare redirection?
        fd = None
        if c.isdigit() and i + 1 < n and command[i + 1] in "<>" and c in "012":
            fd = int(c)
            i += 1
            c = command[i]
        if c in _OP_CHARS:
            j = i
            while j < n and command[j] in _OP_CHARS:
                j += 1
            opstr = command[i:j]
            i = j
            tok = _classify_op(opstr, fd)
            if tok.kind == "REDIR_DUP" and i < n and command[i] in "012":
                tok.dup_target = int(command[i])
                i += 1
            elif tok.kind == "REDIR_DUP":
                raise Refused(f"'{opstr}' without a target file descriptor")
            tokens.append(tok)
            at_word_start = True
            if len(tokens) > MAX_TOKENS:
                raise Refused("too many tokens")
            continue
        if c == ";":
            raise Refused("lone ';'")
        if c == "(":
            tokens.append(Tok(kind="LPAREN"))
            i += 1
            at_word_start = True
            continue
        if c == ")":
            tokens.append(Tok(kind="RPAREN"))
            i += 1
            at_word_start = False
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
                raw_prefix_done = True
                j = command.find("'", i + 1)
                if j == -1:
                    raise Refused("unterminated single quote")
                word.add(command[i + 1 : j], True)
                i = j + 1
                continue
            if ch == '"':
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
                        i = _expand_dollar(command, i, n, env, parts)
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
                word.add(command[i + 1], True)
                i += 2
                raw_prefix_done = True
                continue
            if ch == "$":
                parts = []
                i = _expand_dollar(command, i, n, env, parts)
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
        at_word_start = False
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
    # root_dir kwarg needs 3.10+; requires-python is 3.12 but keep this explicit/portable.
    prev = os.getcwd()
    try:
        os.chdir(cwd)
        return glob_mod.glob(pattern)
    finally:
        os.chdir(prev)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

_REFUSED_TARGET_SUFFIXES = (".sh", ".bat", ".cmd", ".ps1")


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
                _check_target_suffix(stage)


def _static_name(cmd):
    return expand_word_no_glob(cmd.name)


def _is_builtin_name(cmd):
    return _static_name(cmd) in BUILTIN_NAMES


def _check_target_suffix(cmd):
    name = _static_name(cmd)
    lowered = name.lower()
    for suf in _REFUSED_TARGET_SUFFIXES:
        if lowered.endswith(suf):
            raise Refused(f"refusing to run a '{suf}' target: {name}")
    if name in BUILTIN_NAMES:
        if name == "cd" and len(cmd.args) != 1:
            raise Refused("cd requires exactly one argument")


def analyze(command, env=None, cwd=None):
    """Public linter entry point: (ok, reason)."""
    _, reason = compile_command(command, env=env)
    return (reason is None, reason)


# ---------------------------------------------------------------------------
# Executor
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
        # Windows only (_run_windows): the Job Object every spawned process is
        # assigned to, so a timeout can kill the whole tree in one call.
        self.win_job = None


def _is_windows_path_sep(name):
    return "/" in name or (IS_WINDOWS and "\\" in name)


def resolve_executable(name, cwd, env):
    if _is_windows_path_sep(name) or name.startswith("."):
        candidate = name if os.path.isabs(name) else os.path.join(cwd, name)
        return _check_candidate(candidate)
    path_var = _env_get(env, "PATH") or ""
    for d in path_var.split(os.pathsep):
        if not d:
            continue
        candidate = os.path.join(d, name)
        found = _check_candidate(candidate)
        if found:
            return found
    return None


def _check_candidate(path):
    if IS_WINDOWS:
        exts = _env_get(os.environ, "PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        ext_list = [e for e in exts.split(";") if e]
        if os.path.splitext(path)[1]:
            return os.path.abspath(path) if os.path.isfile(path) else None
        for ext in ext_list:
            cand = path + ext
            if os.path.isfile(cand):
                return os.path.abspath(cand)
        return None
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return os.path.abspath(path)
    return None


def _open_redir_target(redir, ctx):
    path = expand_word_no_glob(redir.target)
    if path == "/dev/null" or (IS_WINDOWS and path.upper() == "NUL"):
        path = os.devnull
    abspath = path if os.path.isabs(path) else os.path.join(ctx.cwd_box.value, path)
    if redir.kind == ">":
        return os.open(abspath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    if redir.kind == ">>":
        return os.open(abspath, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    if redir.kind == "<":
        return os.open(abspath, os.O_RDONLY)
    raise ValueError(redir.kind)


def _apply_redirs(cmd, base_fds, ctx):
    """Returns (local_fds, opened_fds_to_close_after_spawn)."""
    local = dict(base_fds)
    opened = []
    for redir in cmd.redirs:
        if redir.kind == ">&":
            local[redir.fd] = local[redir.target]
        else:
            fd = _open_redir_target(redir, ctx)
            local[redir.fd] = fd
            opened.append(fd)
    return local, opened


class _OutputSink:
    """Drains a pipe read-end into a size-bounded buffer without ever blocking a writer."""

    def __init__(self, read_fd, limit):
        self.read_fd = read_fd
        self.limit = limit
        self.buf = bytearray()
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
                    if len(self.buf) < self.limit:
                        self.buf.extend(chunk[: self.limit - len(self.buf)])
                    # else: discard, but keep draining so writers never block.
        finally:
            try:
                os.close(self.read_fd)
            except OSError:
                pass

    def snapshot(self):
        with self.lock:
            return bytes(self.buf)


def run_command(cmd, fds, ctx, close_after_spawn: AbstractSet[int] = frozenset()):
    name = expand_word_no_glob(cmd.name)
    if name in BUILTIN_NAMES:
        return _run_builtin(cmd, name, fds, ctx)
    args = [name]
    for w in cmd.args:
        args.extend(expand_word_glob(w, ctx.cwd_box.value))
    local_fds, opened = _apply_redirs(cmd, fds, ctx)
    overlay_env = dict(ctx.env)
    for aname, avalue in cmd.assigns:
        overlay_env[aname] = expand_word_no_glob(avalue)
    resolved = resolve_executable(args[0], ctx.cwd_box.value, overlay_env)
    if resolved is None:
        msg = f"command not found: {args[0]}"
        if IS_WINDOWS and args[0] == "python3":
            msg += " (use `python` or `py -3`)"
        _write_fd(local_fds[2], (msg + "\n").encode())
        _close_quiet(opened)
        _close_quiet(fd for fd in close_after_spawn if fd not in opened)
        return 127
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
    except OSError as exc:
        _write_fd(local_fds[2], f"exec failed: {exc}\n".encode())
        _close_quiet(opened)
        _close_quiet(fd for fd in close_after_spawn if fd not in opened)
        return 127
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


def _write_fd(fd, data):
    try:
        os.write(fd, data)
    except OSError:
        pass


def _run_builtin(cmd, name, fds, ctx):
    args = []
    for w in cmd.args:
        args.extend(expand_word_glob(w, ctx.cwd_box.value))
    local_fds, opened = _apply_redirs(cmd, fds, ctx)
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
        status = run_chain(stage.chain, fds, inner_ctx)
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
    pipes = [os.pipe() for _ in range(n - 1)]
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
        statuses[idx] = run_stage(stages[idx], stage_fds, ctx, private)

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
    for r, w in pipes:
        for fd in (r, w):
            try:
                os.close(fd)
            except OSError:
                pass
    # pipefail: last non-zero stage status, else 0.
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
# Top-level request handling
# ---------------------------------------------------------------------------


class _ForkedChild:
    """Adapts a raw pid to the (pid, poll()) surface terminate_process_tree expects."""

    def __init__(self, pid):
        self.pid = pid
        self._status = None

    def poll(self):
        if self._status is not None:
            return self._status
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._status = 0
            return 0
        if wpid == 0:
            return None
        self._status = status
        return status


def _run_in_child(command, cwd, env, mode, output_limit, result_path):
    """Runs inside the forked, setsid'd child. Writes the JSON result to result_path."""
    chain, reason = compile_command(command, env=env)
    answer = {"status": None, "timed_out": False}
    if reason is not None:
        answer.update(status=None, refused=True, reason=reason)
        _finish(answer, result_path)
        return
    devnull_r = os.open(os.devnull, os.O_RDONLY)
    # `sinks` stays None on the ledger path (stdout/stderr are DEVNULL, nothing to capture) and
    # holds the (out, err) pair on the gate path -- one Optional instead of two, so every read
    # site below narrows with a single `is not None` check instead of needing pyright to
    # correlate two separately-written `if mode == "gate":` blocks (reportOptionalMemberAccess).
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
        answer["reason"] = f"internal error: {exc}"
        answer["status"] = 1
        _close_quiet(fds.values())
        _finish(answer, result_path)
        return
    for fd in set(fds.values()):
        try:
            os.close(fd)
        except OSError:
            pass
    answer["status"] = status
    if sinks is not None:
        time.sleep(0.02)  # brief grace window for a well-behaved writer's final bytes
        answer["stdout"] = sinks[0].snapshot().decode("utf-8", errors="replace")
        answer["stderr"] = sinks[1].snapshot().decode("utf-8", errors="replace")
    _finish(answer, result_path)


def _close_quiet(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _finish(answer, result_path):
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(answer, f)


def _run_posix(request):
    import tempfile

    command = request["command"]
    cwd = request.get("cwd") or os.getcwd()
    env = request.get("env") or dict(os.environ)
    mode = request.get("mode", "gate")
    timeout_ms = request.get("timeout_ms", 30000)
    output_limit = request.get("output_limit", 1_000_000)

    # Fast path: a refused command never needs a process fork at all.
    _, reason = compile_command(command, env=env)
    if reason is not None:
        return {"status": None, "refused": True, "reason": reason, "timed_out": False}

    fd, result_path = tempfile.mkstemp(prefix="cmdrun-")
    os.close(fd)
    try:
        pid = os.fork()
        if pid == 0:
            # Child: become a new session (and thus process-group) leader so the
            # parent can kill "everything this command spawned" with one
            # os.killpg(-pid) without touching itself -- see module docstring.
            try:
                os.setsid()
                # os.fork() duplicates the WHOLE fd table, including OUR OWN caller's real
                # stdin/stdout/stderr (the harness that invoked `python cmdrun.py` is reading
                # our answer off of fd 1). If this child ever gets stuck -- normally
                # impossible since terminate_process_tree() below sends SIGKILL, which cannot
                # be ignored, but a stuck kill (a ESRCH race, a PID reused as a new session
                # leader, ...) is exactly the kind of thing this file exists to make
                # impossible to observe -- it would keep those fds open forever, and the
                # PARENT already answered and exited, so the caller's read would never see
                # EOF and would hang past its OWN timeout with no cmdrun bug even visible to
                # it. Decoupling this now, unconditionally, means a stuck child can never
                # again manifest as "the caller hung", only as an OS-level leaked process
                # (still a bug, but a survivable and diagnosable one, not a silent hang).
                devnull = os.open(os.devnull, os.O_RDWR)
                os.dup2(devnull, 0)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                if devnull > 2:
                    os.close(devnull)
                _run_in_child(command, cwd, env, mode, output_limit, result_path)
            finally:
                os._exit(0)
        child = _ForkedChild(pid)
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while True:
            status = child.poll()
            if status is not None:
                break
            if time.monotonic() >= deadline:
                terminate_process_tree(child)
                # A grandchild ignoring SIGTERM/SIGKILL race is bounded: give the
                # group a brief moment to actually die before we report timeout.
                time.sleep(0.05)
                return {"status": 124, "timed_out": True, "reason": "timeout"}
            time.sleep(0.002)
        if os.path.exists(result_path):
            with open(result_path, encoding="utf-8") as f:
                data = f.read()
            return json.loads(data) if data else {"status": 1, "timed_out": False, "reason": "no result produced"}
        return {"status": 1, "timed_out": False, "reason": "child produced no result"}
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Windows Job Object execution (written per spec; not exercised on this host)
# ---------------------------------------------------------------------------


def _run_windows(request):  # pragma: no cover - requires Windows to exercise
    command = request["command"]
    cwd = request.get("cwd") or os.getcwd()
    env = request.get("env") or dict(os.environ)
    mode = request.get("mode", "gate")
    timeout_ms = request.get("timeout_ms", 30000)
    output_limit = request.get("output_limit", 1_000_000)

    chain, reason = compile_command(command, env=env)
    if reason is not None:
        return {"status": None, "refused": True, "reason": reason, "timed_out": False}

    job = _win_create_job_object()
    if job is None:
        return {"status": None, "timed_out": False, "reason": "failed to create a Windows Job Object"}
    try:
        devnull_r = os.open(os.devnull, os.O_RDONLY)
        # See the matching comment in _run_in_child: one Optional pair, not two Optionals,
        # so every reader narrows with a single `is not None` check.
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
        ctx.win_job = job
        result_holder = {}
        done = threading.Event()

        def worker():
            try:
                result_holder["status"] = run_chain(chain, fds, ctx)
            except Exception as exc:  # noqa: BLE001
                result_holder["error"] = str(exc)
            finally:
                done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        if not done.wait(timeout_ms / 1000.0):
            _win_kill_job(job)
            return {"status": 124, "timed_out": True, "reason": "timeout"}
        if "error" in result_holder:
            return {"status": 1, "timed_out": False, "reason": f"internal error: {result_holder['error']}"}
        answer = {"status": result_holder["status"], "timed_out": False}
        if sinks is not None:
            time.sleep(0.02)
            answer["stdout"] = sinks[0].snapshot().decode("utf-8", errors="replace")
            answer["stderr"] = sinks[1].snapshot().decode("utf-8", errors="replace")
        return answer
    finally:
        _win_close_job(job)


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9


def _win_create_job_object():  # pragma: no cover
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
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
        kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        return job
    except OSError:
        return None


def _win_kill_job(job):  # pragma: no cover
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.TerminateJobObject(job, 1)
    except OSError:
        pass


def _win_close_job(job):  # pragma: no cover
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.CloseHandle(job)
    except OSError:
        pass


def main():
    raw = sys.stdin.readline()
    try:
        request = json.loads(raw) if raw.strip() else {}
        if "command" not in request:
            raise ValueError("missing 'command'")
    except (json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"status": None, "timed_out": False, "reason": f"bad request: {exc}"}))
        return
    try:
        if IS_WINDOWS:
            answer = _run_windows(request)
        else:
            answer = _run_posix(request)
    except Exception as exc:  # noqa: BLE001 - the one place we truly must never crash
        answer = {"status": None, "timed_out": False, "reason": f"internal error: {exc}"}
    answer.setdefault("timed_out", False)
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
