#!/usr/bin/env python3
"""ledger_check.py: parses a DELEGATION.md ledger and reports its state.

Structural enforcement for the delegation half: the gate is only satisfied when every
unit row is `verified` and the evidence section is non-empty.

Usage:
  python3 ledger_check.py [path/to/DELEGATION.md]

Exit codes:
  0  ledger complete (every row verified, evidence present)
  1  ledger incomplete (pending/done rows, or missing evidence)
  2  not a ledger (no header, no rows, or unparseable)

Port of ledger-check.mjs. The JavaScript test suite is the oracle: it runs unchanged
against this implementation (`AD_RUNTIME=python node tests/ledger-tests.mjs`), so any
divergence is a porting defect rather than a re-specified test.
"""

import hashlib
import os
import re
import subprocess
import sys
import time
import json
import stat as statmod
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from gates import read_stable_regular_file  # noqa: E402  # type: ignore[import-not-found]

# why: the same runner vocabulary as a word set — a regex alternation of shell names trips the
# publish gate's injection scanner, and a dynamically built pattern trips its ReDoS rule.
RUNNER_WORDS = {
    "node", "python", "python3", "pytest", "git", "npx", "npm", "pnpm",
    "make", "tsc", "deno", "bash", "sh", "ruby", "go", "cargo",
}

# JavaScript's \w, \b and \d are ASCII-only; Python's are Unicode by default. re.ASCII fixes
# those three -- but it ALSO narrows \s, which JavaScript leaves Unicode-wide, so a pattern
# containing both cannot use the flag. Measured: with re.ASCII, `12\xa0tests passed` (a
# non-breaking space, what a paste out of a browser or a word processor routinely carries) is
# NOT a measured result to the port while it IS one to the oracle -- honest evidence silently
# demoted. So the flag is used only where no \s appears, and elsewhere \d and \b are spelled
# out as their ASCII selves.
NOT_WORD_BEFORE = r"(?<![0-9A-Za-z_])"
NOT_WORD_AFTER = r"(?![0-9A-Za-z_])"
CODE_SPAN = re.compile(r"`[^`]+`")
FILENAME_SHAPED = re.compile(r"\b[A-Za-z0-9_./-]+\.[a-z0-9]{2,5}\b", re.I | re.A)
MEASURED_RESULT = re.compile(
    NOT_WORD_BEFORE + r"[0-9]+\s+(?:[a-z]+\s+){0,3}(passed|passing|pass|ok)" + NOT_WORD_AFTER,
    re.I,
)
EXIT_CODE = re.compile(r"exit\s+[0-9]+", re.I)
# NO WHITESPACE in the span: `node test/run-tests.mjs` is a COMMAND that happens to name a
# path, and demanding that string exist as a file is nonsense. Only a bare path is a citation.
CITATION = re.compile(r"`([^`\s]*/[^`\s]*\.[A-Za-z0-9]{1,6})`")
UNIT_HEADER = re.compile(r"^\*\*unit\s+([0-9]+)" + NOT_WORD_AFTER, re.I)
TRAILING_PIPE = re.compile(r"(^|[^\\])\|$")
SEPARATOR_CELL = re.compile(r"^:?-+:?$")
RECEIPT_RE = re.compile(r"\n?<!-- agents-discipline-check: [^>]*-->\n?")
PRIOR_RECEIPT = re.compile(r"<!-- agents-discipline-check: ([^ ]+) sha256:([0-9a-f]+) -->")
CREATED = re.compile(
    r"^Created:?\s+([0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:]+(?:[+-][0-9]{2}:?[0-9]{2})?)", re.M
)

path = sys.argv[1] if len(sys.argv) > 1 else "DELEGATION.md"


def fail(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


try:
    # Every other reader in this codebase goes through read_stable_regular_file; a plain read
    # has no size cap, no regular-file assertion and no O_NOFOLLOW. A FIFO passed as argv
    # blocked the process forever -- an unbounded wait in the one script a coordinator is told
    # to run on a ledger it may not have written.
    text = read_stable_regular_file(path, label="ledger")
except OSError as err:
    fail(2, f"agents-discipline: cannot read {path}: {err}")

lines = text.split("\n")

# Find the table: a line starting with `| # |` is the header we accept.
header_idx = next((i for i, l in enumerate(lines) if re.match(r"^\|\s*#\s*\|", l)), -1)
if header_idx == -1:
    fail(2, f"agents-discipline: {path} is not a DELEGATION.md ledger (no unit table header)")

# The header's width is the contract every row is held to. Without it the only shape check
# was `len(cells) < 6`, which cannot see the failure that actually happens: an UNESCAPED pipe
# inside an Acceptance command adds a column, so a 6-column row becomes 7 cells, passes the
# `< 6` test, and `Status` is then read from the wrong cell. The row parses silently as
# whatever the shifted text happens to say. Widening the table has the mirror problem: a row
# omitting the new trailing columns reads them as missing forever, with no error.
_header = lines[header_idx].strip()
column_count = (
    len(_header.replace("\\|", " ").split("|"))
    - 1
    - (1 if TRAILING_PIPE.search(_header) else 0)
)

rows = []
malformed = []
for i in range(header_idx + 1, len(lines)):
    line = lines[i].strip()
    if not line:
        continue
    if not line.startswith("|"):
        break  # table ended
    # A trailing pipe only delimits when it is UNESCAPED. Testing the RAW line while splitting
    # the ESCAPED one silently dropped a last cell ending in `\|` -- and since the header used
    # the same wrong test, the two agreed and no malformed error fired. The column vanished.
    parts = line.replace("\\|", "\0").split("|")
    cells = [c.strip().replace("\0", "|") for c in parts[1:(-1 if TRAILING_PIPE.search(line) else None)]]
    # Separator row: every cell is dashes and/or colons (`---`, `:--`, `:--:`, `--:`).
    if all(c == "" or SEPARATOR_CELL.match(c) for c in cells):
        continue
    if len(cells) < 6 or len(cells) != column_count:
        # Direction matters, because the two causes need opposite fixes: too MANY cells is
        # almost always an unescaped pipe inside a command, too FEW is a row that never got
        # the columns the header added.
        if len(cells) < 6:
            why = f"only {len(cells)} cells; a unit row needs at least 6"
        elif len(cells) > column_count:
            why = (
                f"{len(cells)} cells but the header declares {column_count}"
                " — escape a literal pipe inside a cell as \\|"
            )
        else:
            why = (
                f"{len(cells)} cells but the header declares {column_count}"
                f" — the row is missing {column_count - len(cells)} trailing cell(s)"
            )
        malformed.append({"line": i + 1, "cells": cells, "why": why})
        continue
    if cells[0] == "..." or cells[1] == "...":
        continue  # template placeholder row
    rows.append({
        "unit": cells[0],
        "name": cells[1],
        "files": cells[2],
        "worker": cells[3],
        "acceptance": cells[4],
        "status": cells[5].lower(),
    })

if not rows:
    # Every diagnostic below this point is printed by the summary, which this bail skips. So a
    # ledger whose rows are ALL malformed used to report "no unit rows" on a file that visibly
    # has rows, with the reason already computed and then thrown away. Say it here instead.
    if malformed:
        detail = "\n".join(f"    - line {m['line']}: {m['why']}" for m in malformed)
        fail(2, f"agents-discipline: {path} has a table header but every row is malformed:\n{detail}")
    fail(2, f"agents-discipline: {path} has a table header but no unit rows")

counts = {"pending": 0, "done": 0, "verified": 0, "other": 0}
unverified = []
for r in rows:
    if r["status"] in ("pending", "done", "verified"):
        counts[r["status"]] += 1
    else:
        counts["other"] += 1
    if r["status"] != "verified":
        unverified.append(r)


def is_strong_evidence(l):
    """True when a line names something concrete: a command-shaped code span, a file path, or
    a measured result. Single words and generic phrases fail on purpose, so "verified" or
    "`done`" do not pass."""
    for s in CODE_SPAN.findall(l):
        inner = s[1:-1].strip()
        if re.search(r"\s", inner) or re.search(r"[/.]", inner):
            return True
        # A runner word counts only when it IS the span -- `pytest`, `make`. The scan this
        # replaced read the whole LINE and split on non-alphanumerics, so "I will go to the
        # shop" was strong evidence: `go` is a runner. One English sentence satisfied the
        # evidence requirement for an entire ledger.
        if inner.lower() in RUNNER_WORDS:
            return True
    # {2,5}, not {1,5}: a one-character extension made "U.S.A." look like a filename. The
    # trade is real -- `main.c`, `foo.h` and `analysis.R` no longer read as filename-shaped in
    # UNBACKTICKED prose. Cite those in backticks, where the span loop above accepts any path
    # character. The citation pattern is untouched.
    if FILENAME_SHAPED.search(l):
        return True
    # `of` dropped ("3 of them" is not a measured result), and intervening words allowed,
    # because the natural way to report a real run is "93 tests passed".
    if MEASURED_RESULT.search(l):
        return True
    if EXIT_CODE.search(l):
        return True
    return False


# Evidence: content after the table. The template's "## Rules of this ledger" section is
# boilerplate and is skipped structurally, by heading, so template wording can change without
# breaking the check.
evidence_lines = []
in_rules_section = False
for i in range(header_idx + 1, len(lines)):
    l = lines[i].strip()
    if l == "":
        continue
    if re.match(r"^##\s+", l):
        in_rules_section = bool(re.match(r"^##\s+Rules of this ledger\s*$", l, re.I))
        continue
    if l.startswith("|"):
        continue  # still in the table
    if in_rules_section:
        continue  # template rules boilerplate
    if l.lower().startswith("units:"):
        continue
    # Drop a line that IS a placeholder, not any line that CONTAINS angle brackets. The old
    # test killed real evidence in every language with generics: "changed the signature to
    # Vec<String>, 12 tests passed" was discarded -- and since the drop takes the whole line,
    # a `**Unit N —**` header goes with it, so the unit's entire block disappears and the row
    # reports UNBACKED with nothing explaining why.
    # Require a letter or digit to survive the strip, rather than enumerating punctuation: the
    # enumerating form needed a patch per character (it listed the ASCII hyphen but not the
    # em-dash this skill's own format uses), and an ASCII-only class dropped evidence written
    # in any script without Latin letters -- "тесты пройдены" and "テスト成功" both vanished,
    # taking their unit block with them. `str.isalnum()` is Unicode-aware, which is what the
    # JavaScript oracle's `\p{L}\p{N}` means; Python's `re` has no `\p{...}`, so this is the
    # faithful form rather than a third-party regex dependency.
    if not any(ch.isalnum() for ch in re.sub(r"<[^>]+>", "", l)):
        continue
    evidence_lines.append(l)

evidence_ok = sum(1 for l in evidence_lines if is_strong_evidence(l)) >= 1
evidence_text = "\n".join(evidence_lines)

# ---------------------------------------------------------------------------
# Artifact corroboration.
#
# Everything above reads PROSE the coordinator wrote, so everything above can be invented in
# one keystroke. This section does not read the ledger's claims -- it reads the FILESYSTEM,
# and asks whether the artifacts the ledger names are actually there. To pass, a fabricator
# must now CREATE FILES at the paths it invented, non-empty, dated after the ledger began.
#
# STALE-ARTIFACT RULE: an artifact older than the ledger cannot be evidence FOR THIS RUN.
artifact_paths = [m.strip() for m in CITATION.findall(evidence_text)]

# Per-unit Evidence blocks, sliced by LINE-INITIAL `**Unit N` headers. The per-unit regex this
# replaced could not terminate a block -- both its lookaheads needed text the evidence scan
# above had already removed -- so every block ran to end-of-text and one real artifact cited
# under the LAST unit backed every invented row above it.
#
# Line-anchored is load-bearing: a substring test for "Unit 1" also matches prose that merely
# mentions a unit (the template's own Evidence how-to did), reproducing the same over-crediting
# on an unmodified template. Slicing also removes the untrusted-cell regex interpolation:
# a unit cell of `(a+)+$` used to be compiled and matched with no budget.
#
# DELIBERATE BEHAVIOUR CHANGE (inherited from the oracle): only `**Unit N` is recognised, the
# one shape templates/DELEGATION.md documents. Evidence under no header at all backs no row.
evidence_blocks = {}
_current = None
for line in evidence_text.split("\n"):
    # Case-insensitive: `**unit 1 —**` is the same header a human meant to write, and matching
    # it case-sensitively would silently yield an empty block and report the row UNBACKED for
    # a reason no message names.
    m = UNIT_HEADER.match(line)
    if m:
        _current = m.group(1)
    if _current is not None:
        evidence_blocks[_current] = evidence_blocks.get(_current, "") + line + "\n"


def evidence_block_for(unit):
    return evidence_blocks.get(str(unit).strip(), "")


def existing_artifacts_in(block, bases):
    found = []
    for rel in (m.strip() for m in CITATION.findall(block)):
        cands = [rel] if os.path.isabs(rel) else [os.path.join(b, rel) for b in bases]
        if any(os.path.exists(c) for c in cands):
            found.append(rel)
    return found


# Resolution walks UP from the ledger, because a cited path is written relative to the PROJECT,
# not to wherever the ledger happens to sit. Resolving only against the ledger's own directory
# would redden every honest ledger of that shape -- the one failure mode that guarantees a gate
# gets switched off instead of obeyed.
bases = []
_d = os.path.dirname(os.path.abspath(path))
while True:
    bases.append(_d)
    if _d == os.path.dirname(_d):
        break
    _d = os.path.dirname(_d)
bases.append(os.getcwd())

missing_artifacts = []
empty_artifacts = []
stale_artifacts = []
artifacts_ok = True

if any(r["status"] == "verified" for r in rows):
    # The ledger's own start time, so "newer than the ledger" is answerable. Absent it, the
    # staleness rule is skipped rather than guessed -- a check that invents its own baseline
    # would fail honest ledgers, and a gate that cries wolf gets deleted.
    created_ms = None
    cm = CREATED.search(text)
    if cm:
        raw = cm.group(1).replace(" ", "T")
        # `T24:00` is legal ISO 8601 and Date.parse accepts it; fromisoformat raises on it
        # ("hour must be in 0..23"). Left unhandled, the ValueError below swallows the failure
        # and the staleness rule is SILENTLY SKIPPED -- a gate that quietly stops running is
        # worse than one that complains. Rewrite it to the midnight it denotes.
        m24 = re.match(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})T24(:.*)?$", raw)
        if m24:
            raw = (datetime.fromisoformat(m24.group(1)) + timedelta(days=1)).strftime("%Y-%m-%d") \
                + "T00" + (m24.group(2) or "")
        try:
            dt = datetime.fromisoformat(raw)
            # A bare datetime with no offset is local time in both runtimes.
            if dt.tzinfo is None:
                dt = dt.astimezone()
            created_ms = dt.timestamp() * 1000
        except ValueError:
            created_ms = None

    for p in artifact_paths:
        cands = [p] if os.path.isabs(p) else [os.path.join(b, p) for b in bases]
        hit = next((c for c in cands if os.path.exists(c)), None)
        if hit is None:
            missing_artifacts.append(p)
            continue
        st = os.stat(hit)
        # S_ISREG on the stat already taken, not a second os.path.isfile() call: two syscalls
        # leave a window where the file vanishes between them, and isfile() swallows the error
        # and answers False, so a zero-byte artifact would be judged by the staleness branch
        # using the FIRST stat. The oracle asks its one stat object; so does this.
        if statmod.S_ISREG(st.st_mode) and st.st_size == 0:
            empty_artifacts.append(p)
        elif created_ms is not None and st.st_mtime * 1000 < created_ms:
            stale_artifacts.append(p)
    artifacts_ok = not (missing_artifacts or empty_artifacts or stale_artifacts)

# ---------------------------------------------------------------------------
# Acceptance RE-RUN — the only check here that does not take the coordinator's word.
#
# When a row's Acceptance cell holds a runnable command, the checker RUNS IT and reads the exit
# code itself. To fake a command-backed row you must make the real command really pass, which
# is indistinguishable from doing the work.
#
# A row whose acceptance CANNOT be a command is reported as unreproducible, not failed. Real
# acceptance criteria legitimately include "the card carries a first-hand argument with
# file:line" — no exit code expresses that, and failing those would redden honest ledgers.


def acceptance_command(cell):
    for s in CODE_SPAN.findall(cell):
        inner = s[1:-1].strip()
        # A command, not a bare path or a single word: it must have an argument or a runner.
        if re.search(r"\s", inner) and re.match(r"^[A-Za-z0-9_./-]+", inner):
            return inner
    return None


# Run from the project, not from wherever the checker was invoked: acceptance commands are
# written relative to the repo the work happened in.
run_cwd = os.path.dirname(os.path.abspath(path))
_d = run_cwd
while True:
    if os.path.exists(os.path.join(_d, ".git")):
        run_cwd = _d
        break
    if _d == os.path.dirname(_d):
        break
    _d = os.path.dirname(_d)

# A no-op acceptance is one that exits 0 NO MATTER WHAT, so writing one is an attempt to
# satisfy the re-run without testing anything. The bar is UNCONDITIONAL success, and getting
# that bar wrong broke both previous attempts, in opposite directions:
#
#   FALSE POSITIVES. `test -f x`, `[ -f x ]`, `ls dist/*.js` and `cat f` all EXIT 1 when the
#   thing is missing. They are WEAK checks, not tautologies. Rejecting them reddens honest
#   ledgers, and a check that reddens honest ledgers is a check someone eventually deletes.
#
#   ESCAPES. `pytest -q || true` exits 0 unconditionally. No whole-command regex can see it,
#   because the cheat lives in the OPERATOR, not in any one word.
ALWAYS_TRUE = re.compile(
    # `\Z`, not `$`: Python's `$` also matches just before a trailing newline, JavaScript's
    # does not. Unreachable today (cells come from one stripped table line) and spelled
    # exactly anyway, because the next edit is where an inexact anchor gets noticed.
    r"^(?:(?:/(?:usr/)?bin/)?(?:true|echo|printf|pwd|sleep)(?:\s+[^&|;]*)?|:|command\s+true|exit\s+0)\Z",
    re.I,
)

# The three operators have THREE DIFFERENT status rules, and collapsing any two of them is how
# the previous versions leaked:
#   `;`   only the LAST status survives  -> `false; true` exits 0
#   `||`  ANY always-true link forces 0  -> `true || pytest` AND `pytest || true`
#   `&&`  every link must hold           -> `echo ok && pytest -q` is honest
# `||` and `;` are REFUSED rather than parsed. Both exist to decouple a chain's exit status
# from whether its work succeeded, and an acceptance has no honest need for either. Refusing
# them deletes the entire always-green class by construction, instead of by out-parsing
# whoever writes the next one. `&&` stays legal because it cannot hide a failure.
# USER decision, 2026-09-06.
CHAIN_OPERATORS = re.compile(r"\|\||;")


def mask_quoted(s):
    """Blank out quoted regions, length-preservingly, keeping the delimiters.

    Operators only count OUTSIDE quotes: `python3 -c "import sys; sys.exit(0)"` is an honest
    command whose `;` is an ARGUMENT. A single left-to-right scan, because two independent
    regex passes cannot track quote state -- the two-pass version matched an apostrophe INSIDE
    double quotes against a later single quote and spliced away the `&&` between them, so an
    honest command was flagged a no-op. Masking must never invent or destroy an operator.

    An unterminated quote masks to end-of-string, which errs toward refusal -- readable, and a
    human can rewrite it. KNOWN CEILING: a backslash-escaped quote (`echo "a\\"b; c"`) reads as
    closing the string, so the `;` after it looks like a chain and the row is refused.
    """
    out = []
    quote = None
    for c in s:
        if quote:
            if c == quote:
                quote = None
                out.append(c)
            else:
                out.append("x")
        elif c in ("'", '"'):
            quote = c
            out.append(c)
        else:
            out.append(c)
    return "".join(out)


def wraps_whole(s, open_c, close_c):
    """True when the leading bracket closes only at the very end -- i.e. the pair really does
    enclose the whole string, rather than being the first of several groups."""
    depth = 0
    for i, ch in enumerate(s):
        if ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
            if depth == 0:
                return i == len(s) - 1
    return False


def is_noop_acceptance(raw):
    """With `||` and `;` refused upstream, the only remaining shape is an `&&` chain, and it is
    always-green exactly when EVERY link is."""
    s = str(raw).strip()
    # `( true )` and `{ true }` wrap the same command. The balance check is load-bearing:
    # an anchored `^\(...\)$` mangles `(echo a) && (echo b)` into `echo a) && (echo b`.
    for _ in range(4):
        before = s
        for open_c, close_c in (("(", ")"), ("{", "}")):
            if s.startswith(open_c) and s.endswith(close_c) and wraps_whole(s, open_c, close_c):
                s = s[1:-1].strip()
        if s == before:
            break
    if not s:
        return False
    # Judged on the MASKED text: a quoted `&&` is an argument, not a link.
    parts = [p.strip() for p in mask_quoted(s).split("&&")]
    return all(ALWAYS_TRUE.match(p) for p in parts if p)


reran = []
repro_failed = []
unreproducible = []
unbacked = []
rerun_skipped = bool(os.environ.get("AGENTS_DISCIPLINE_SKIP_RERUN"))
# One budget for the whole re-run, not one per row: a 40-row ledger could otherwise occupy this
# process for `rows x 600s` with nothing watching the total. An EMPTY string is treated as
# unset, matching the sibling SKIP_RERUN -- two env vars in one file with opposite
# empty-string semantics is a trap. The validation only runs when a re-run will actually
# happen: a structure-only check has no budget to misconfigure.
def _js_number(s):
    """JavaScript `Number(s)`, because the oracle's accept/reject set is the contract.

    Measured divergence against a bare `float()`: `0x10` is 16 to Number and a ValueError to
    float (the oracle RAN with a 16 ms budget, the port refused with exit 2), and `1_000` is
    NaN to Number and 1000.0 to float (the oracle refused, the port RAN). Neither value is
    sane in a milliseconds variable — the point is that the two runtimes must disagree with
    the operator identically, or the same configuration means two different things.
    """
    if "_" in s:  # JS numeric separators are a LITERAL feature; Number("1_000") is NaN
        return float("nan")
    try:
        for prefix, base in (("0x", 16), ("0o", 8), ("0b", 2)):
            if s[:2].lower() == prefix:
                return float(int(s[2:], base))
        return float(s)
    except ValueError:
        return float("nan")


_budget_raw = (os.environ.get("AGENTS_DISCIPLINE_RERUN_BUDGET_MS") or "").strip()
rerun_budget_ms = 600000.0 if _budget_raw == "" else _js_number(_budget_raw)
# `x != x` is the NaN test; `in (inf, -inf)` the infinity one -- together, Number.isFinite.
if not rerun_skipped and not (
    rerun_budget_ms == rerun_budget_ms
    and rerun_budget_ms not in (float("inf"), float("-inf"))
    and rerun_budget_ms >= 1
):
    # json.dumps, not an f-string in quotes: the oracle uses JSON.stringify, so a value
    # containing a quote or a backslash is escaped rather than printed raw into a message that
    # then reads as if the value ended early.
    fail(2, "agents-discipline: AGENTS_DISCIPLINE_RERUN_BUDGET_MS must be a positive number of "
            f"milliseconds, got {json.dumps(_budget_raw)}")

rerun_deadline = time.monotonic() * 1000 + rerun_budget_ms
if not rerun_skipped:
    for r in (x for x in rows if x["status"] == "verified"):
        # The no-op scan runs over EVERY code span, BEFORE extraction, because
        # acceptance_command requires an argument -- so a bare `true` was not recognised as a
        # command at all and fell through to `unreproducible`, which does not fail. That is
        # the cheapest cheat in the file: one word, and the row passes. Scan first, extract second.
        spans = [s[1:-1].strip() for s in CODE_SPAN.findall(r["acceptance"])]
        # Truthiness, not `is not None`: the oracle's `find` + `if (chained)` skips an
        # empty-string match. Both predicates reject "" today, so this only matters to whoever
        # edits them next -- which is exactly when a silent semantic difference costs an hour.
        chained = next((s for s in spans if CHAIN_OPERATORS.search(mask_quoted(s))), None)
        if chained:
            repro_failed.append({
                "unit": r["unit"], "cmd": chained,
                "code": "acceptance uses `||` or `;` — those pass whatever the code does; "
                        "chain with `&&` or put the steps in a script",
            })
            continue
        noop = next((s for s in spans if is_noop_acceptance(s)), None)
        if noop:
            repro_failed.append({
                "unit": r["unit"], "cmd": noop,
                "code": "no-op acceptance — exits 0 by construction, tests nothing",
            })
            continue
        cmd = acceptance_command(r["acceptance"])
        if not cmd:
            # No runnable acceptance is an honest limit — but only when the row's own evidence
            # still points at something that EXISTS. Otherwise "unreproducible" becomes the
            # hole every fabricated row escapes through. Disk-backed prose is the floor.
            if not existing_artifacts_in(evidence_block_for(r["unit"]), bases):
                unbacked.append(r)
            else:
                unreproducible.append(r)
            continue
        # A row arriving after the shared budget is gone FAILS -- it is not silently skipped,
        # which would turn an exhausted budget into a free pass for every row after it.
        remaining = rerun_deadline - time.monotonic() * 1000
        if remaining <= 0:
            repro_failed.append({
                "unit": r["unit"], "cmd": cmd,
                "code": "re-run budget exhausted before this row ran — nothing was verified here",
            })
            continue
        try:
            # `-o pipefail` is load-bearing, not hygiene. A shell pipeline reports the LAST
            # command's status, so `pytest | tail -1` exits 0 while pytest is failing.
            # Output is DISCARDED, not captured. Measured on the oracle: an acceptance of
            # `sh -c 'sleep 300 & exit 0'` held the call for its ENTIRE timeout because the
            # parent drained a stdout pipe the backgrounded grandchild had inherited (pipe
            # 10002ms, ignore 7ms). Nothing reads this output -- only the exit status is used.
            code = subprocess.run(
                ["/bin/bash", "-o", "pipefail", "-c", cmd],
                cwd=run_cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=remaining / 1000,
            ).returncode
            # A signal death is `returncode = -N` here and `err.status = null` in the oracle,
            # which its `typeof === "number"` test turns into 1. Left raw, an acceptance that
            # segfaults or is OOM-killed prints `-> exit -9` in one runtime and `-> exit 1` in
            # the other: same verdict, different artifact, and the ledger's own output is the
            # artifact. Verdict is unaffected either way -- both are non-zero.
            if code < 0:
                code = 1
        except (subprocess.TimeoutExpired, OSError):
            code = 1
        (reran if code == 0 else repro_failed).append({"unit": r["unit"], "cmd": cmd, "code": code})

print(f"agents-discipline: {path}")
print(f"  units:       {len(rows)}")
print(f"  verified:    {counts['verified']}")
print(f"  done:        {counts['done']}")
print(f"  pending:     {counts['pending']}")
if counts["other"]:
    print(f"  other:       {counts['other']}")
if malformed:
    print(f"  malformed:   {len(malformed)}")
print(f"  evidence:    {'present' if evidence_ok else 'MISSING'}")
if artifact_paths:
    print(f"  artifacts:   {len(artifact_paths)} cited, {'all present' if artifacts_ok else 'PROBLEMS'}")
elif any(r["status"] == "verified" for r in rows):
    # NOT a failure. Evidence that is a command plus its output, with no report file, is honest
    # evidence. But say out loud what the gate could NOT do here, and say WHICH shape was
    # missing: "none cited" reads as "you cited nothing" even when the ledger cited plenty --
    # as commands. The ask is the artifact you PRODUCED.
    print("  artifacts:   none cited — no backticked bare path found (a citation must be a path,"
          " not a command); evidence is uncorroborated prose (shape checked, truth not)")
for label, lst in (("missing", missing_artifacts), ("empty", empty_artifacts),
                   ("older than the ledger", stale_artifacts)):
    if not lst:
        continue
    print(f"  {label} artifacts:")
    for p in lst:
        print(f"    - {p}")

if rerun_skipped:
    # Announced, never silent. Any real ledger that sets it is declaring, in its own output,
    # that nothing was reproduced.
    print("  re-ran:      SKIPPED via AGENTS_DISCIPLINE_SKIP_RERUN — no acceptance was executed this run")
if reran:
    print(f"  re-ran:      {len(reran)} acceptance command(s), all passed")
    for x in reran:
        print(f"    - #{x['unit']} $ {x['cmd']} -> exit 0")
if repro_failed:
    print("  ACCEPTANCE DID NOT REPRODUCE:")
    for x in repro_failed:
        detail = f"exit {x['code']}" if isinstance(x["code"], int) else x["code"]
        print(f"    - #{x['unit']} $ {x['cmd']} -> {detail}")
if unbacked:
    print("  UNBACKED verified rows — no runnable acceptance AND no artifact of their own on disk:")
    for r in unbacked:
        print(f"    - #{r['unit']} {r['name']}")
if unreproducible:
    print(f"  unreproducible: {len(unreproducible)} verified row(s) have no runnable acceptance"
          " — those rest on the coordinator's word alone")
    for r in unreproducible:
        print(f"    - #{r['unit']} {r['name']}")
if unverified:
    print("  unverified rows:")
    for r in unverified:
        print(f"    - #{r['unit']} {r['name']} [{r['status']}]")
if malformed:
    print("  malformed rows:")
    for m in malformed:
        # Say WHY. A row printed back verbatim leaves the author counting pipes by eye, which
        # is exactly the mistake that produced the malformed row.
        print(f"    - line {m['line']}: {m['why']}")
        print(f"        {' | '.join(m['cells'])}")

complete = (
    all(r["status"] == "verified" for r in rows)
    and evidence_ok
    and artifacts_ok
    and not repro_failed
    and not unbacked
    and not malformed
)

# ---------------------------------------------------------------------------
# RECEIPT. The checker signs the ledger it just read, in the ledger itself.
#
# This is the answer to the last cheat that needed no skill at all: simply never running the
# checker. The ABSENCE of a receipt is now a visible fact about the artifact, readable by
# anyone, with no access to the agent's transcript.
#
# The hash is over the ledger with any prior receipt stripped, so it binds the receipt to the
# CONTENT that was checked. That closes the check-then-edit move: pass a clean ledger, then
# quietly flip a row to `verified`, and the stored hash no longer matches what is on disk.
#
# The receipt DELIBERATELY CARRIES NO VERDICT. It used to read PASS/FAIL, and that word was the
# last forgery left in the design: tamper the file afterwards and hand it to someone who reads
# the stamp instead of re-running, and they see a true statement about a document that no
# longer exists. No claim can be misread if no claim is made.
body_for_hash = RECEIPT_RE.sub("\n", text).rstrip() + "\n"
digest = hashlib.sha256(body_for_hash.encode("utf-8")).hexdigest()[:16]

prior = PRIOR_RECEIPT.search(text)
if prior and prior.group(2) != digest:
    print(f"  receipt:     STALE — ledger content changed since it was last checked ({prior.group(1)})")
elif prior:
    print(f"  receipt:     binds this exact content, last checked {prior.group(1)}")

print("  -> ledger complete: every unit verified." if complete else "  -> ledger INCOMPLETE.")

# A run that SKIPPED the re-run did not verify anything, so it must not sign anything. Signing
# it would mint exactly the artifact this receipt exists to make unforgeable: a PASS stamp on a
# ledger nobody executed.
if not rerun_skipped:
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        stamp = (
            f"<!-- agents-discipline-check: {now} sha256:{digest} -->\n"
            "<!-- agents-discipline-check: this is a CONTENT BINDING, not a verdict. It records "
            "which bytes were checked, never whether they passed. Re-run ledger-check.mjs on "
            "this file for a verdict. -->"
        )
        with open(os.path.abspath(path), "w", encoding="utf-8") as fh:
            fh.write(body_for_hash + stamp + "\n")
    except OSError:
        # A read-only ledger is not a verification failure — say nothing and keep the verdict.
        pass

sys.exit(0 if complete else 1)
