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
from gates import (  # noqa: E402  # type: ignore[import-not-found]
    node_fs_message, read_stable_regular_file)
from jsapi import (  # noqa: E402  # type: ignore[import-not-found]
    force_utf8_streams, normalize_argv)

# BEFORE anything can print -- see the function's docstring. MEASURED under
# `PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C`, a DELEGATION.md whose unit name carries one
# non-ASCII character killed this script with UnicodeEncodeError at the row-listing print, where
# the oracle printed the row. A ledger checker that dies on the ledger it was handed is the
# loudest possible divergence.
force_utf8_streams()

# AND BEFORE anything can READ an argument -- see the function's docstring. This script takes the
# ledger PATH from argv, so a bad byte there changes which file the two runtimes even look for.
normalize_argv()

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

# DEFINED HERE, ABOVE EVERY PATTERN, and that placement is forced rather than tidy: the first
# `\s` conversion below is `MEASURED_RESULT`, so a class defined after the patterns is a
# NameError at import. The block used to sit lower, which was fine only while `UNIT_HEADER` was
# its sole consumer.
#
# JS `String.prototype.trim()` strips WhiteSpace + LineTerminator, which is NOT Python's
# `str.strip()` set. Measured across 0..0x10FFFF (surrogates skipped) on 2026-09-07:
#   JS strips, Python does NOT:  U+FEFF
#   Python strips, JS does NOT:  U+001C U+001D U+001E U+001F U+0085
# The divergence runs BOTH WAYS, so "strip more" is not the fix -- the port must strip exactly
# this set and no other. A ledger saved as UTF-8-with-BOM by a Windows editor put U+FEFF on a
# Status cell and exited 0 under node, 1 under python3: same bytes, same command, opposite
# verdicts, and no differential could see it (tests/whitespace-diff.sh now does).
# SPELLED AS CODE POINTS, never as the characters themselves. Written literally, this constant
# is a run of invisible glyphs: unreadable in review, and silently destroyed by an editor that
# trims whitespace or a paste through anything that normalizes. Numbers survive all of that, and
# they are the only form in which a reader can check the set against the measurement above.
# Every converted regex reads this set through JS_WS_CLASS, so a change here rewrites all of them
# at once, far from here and invisible at every call site.
# Re-derive it by finding, in each runtime, the code points cp where (cp + "x" + cp) trims to "x".
_JS_TRIM_CODEPOINTS = (
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007,
    0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
    0xFEFF,
)
JS_TRIM = "".join(map(chr, _JS_TRIM_CODEPOINTS))

# The SAME set spelled as a character class, because ECMAScript defines `\s` as exactly the
# WhiteSpace + LineTerminator that `trim()` strips -- so one constant serves both, and the two
# cannot drift apart. Python's `\s` is NOT that set (see the divergence table above), so any
# ported pattern whose JS original wrote `\s` must write THIS instead.
# Measured 2026-09-07, and this is the defect that motivated it: with `\s+` here, the evidence
# line `**Unit<U+FEFF>1 --** the endpoint was retired.` opened a block under node and opened
# NOTHING under python3, so the abandoned-row marker stayed silent in the oracle and FIRED in
# the port. Same bytes, opposite output, no test covering it.
JS_WS_CLASS = "[" + "".join(map(re.escape, JS_TRIM)) + "]"
CODE_SPAN = re.compile(r"`[^`]+`")
FILENAME_SHAPED = re.compile(r"\b[A-Za-z0-9_./-]+\.[a-z0-9]{2,5}\b", re.I | re.A)
# TWO divergences on one line, and they must be fixed TOGETHER in this order -- MEASURED.
#
# 1. BODY. `\s` -> JS_WS_CLASS, as everywhere else in this file.
# 2. FLAGS. `re.I` -> `re.I | re.A`, because Python's `re.I` is NOT JavaScript's `/i`. JS `/i`
#    WITHOUT the `u` flag -- which is what the oracle uses -- refuses case foldings that map a
#    non-ASCII character onto an ASCII one. Python's performs them, so the evidence line
#    `12 paſſed` is STRONG evidence in the port and NOT in the oracle. The `[a-z]+` CLASS
#    diverges the same way, not just the literal alternation: Python folds U+017F, U+0131 and
#    U+0130 into `[a-z]`, JS does not.
#
# THE ORDER IS LOAD-BEARING, and the file's own `:55-59` comment is why: `re.A` narrows `\s` too.
# Applied to a pattern still spelling `\s` it would trade a fold divergence for a whitespace one
# -- measured at the sibling site, where `re.A`-first takes the divergent set from 6 code points
# to 19 by dropping every shared non-ASCII space (NBSP, U+2000-200A, U+3000...). Once the
# whitespace is an explicit literal class there is no `\s` left for `re.A` to narrow.
#
# `re.A` IS NOT UNCONDITIONALLY SAFE, and the reason it is safe HERE is narrower than "\s was the
# only problem" -- MEASURED: `re.A` also restricts IGNORECASE folding to ASCII, while JS non-`u`
# `/i` DOES fold non-ASCII to non-ASCII (it excludes only foldings onto an ASCII character). So
# `/é/i` matches `É` in the oracle, and `re.compile("é", re.I | re.A)` does NOT -- a divergence
# `re.A` would INTRODUCE. It cannot bite here because every case-bearing element in this pattern
# is ASCII (`[a-z]`, and the literal `passed|passing|pass|ok`), leaving the folding restriction
# nothing to act on. DO NOT copy `re.A` to a site containing a non-ASCII literal or class member
# on the strength of this comment; check that precondition there.
# `NOT_WORD_BEFORE`/`NOT_WORD_AFTER` are spelled as explicit ASCII classes rather than `\b`, so
# `re.A` has nothing to narrow there either.
# `FILENAME_SHAPED` above already ships `re.I | re.A`; this is the house form, not a new device.
MEASURED_RESULT = re.compile(
    NOT_WORD_BEFORE + r"[0-9]+" + JS_WS_CLASS + r"+(?:[a-z]+" + JS_WS_CLASS
    + r"+){0,3}(passed|passing|pass|ok)" + NOT_WORD_AFTER,
    re.I | re.A,
)
# Oracle `ledger-check.mjs:161` is `/exit\s+\d+/i`, and BOTH of its divergence axes bite here.
# MEASURED against the oracle over 8 probes: the pre-fix port disagreed on 5 of them.
#   exit<U+001C>0, exit<U+0085>0    port matched, node did NOT  (Python's `\s` is the wider set)
#   exit<U+FEFF>0                   node matched, port did NOT  (it runs BOTH ways -- "strip
#                                   more" is not the fix, the set must be exactly JS's)
#   ex<U+0131>t 0, EX<U+0130>T 0    port matched, node did NOT  (`re.I` folds non-ASCII onto
#                                   ASCII; JS `/i` without `u` refuses exactly those foldings)
# Controls that had to NOT move, and did not: `exit 0` (both match), `exit<NBSP>0` (NBSP is in
# both whitespace sets, both match), `exit <U+0660>` (both REFUSE -- the oracle's `\d` was
# already spelled `[0-9]` here, so that axis was closed before this change and the probe proves
# the fix did not reopen it).
# Order per :101-127: BODY first, FLAGS second. `re.A` is safe here by the precondition stated
# at :117-124 -- every case-bearing element is ASCII (`exit`), so the ASCII-folding restriction
# `re.A` adds has no non-ASCII literal to act on.
EXIT_CODE = re.compile(r"exit" + JS_WS_CLASS + r"+[0-9]+", re.I | re.A)
# NO WHITESPACE in the span: `node test/run-tests.mjs` is a COMMAND that happens to name a
# path, and demanding that string exist as a file is nonsense. Only a bare path is a citation.
CITATION = re.compile(r"`([^`\s]*/[^`\s]*\.[A-Za-z0-9]{1,6})`")
# `re.A` is not cosmetic here: bare `re.I` folds U+017F/U+0131/U+0130/U+212A onto ASCII, so
# `**UNIT 1` (dotted-I) matched the literal `unit` in Python and NOT in node -- this finder
# decides evidence ATTRIBUTION, so over-matching reports a row BACKED that the oracle reports
# UNBACKED. JS `/i` without `u` refuses every non-ASCII->ASCII fold; `re.A` is how Python says
# the same thing. Safe on THIS pattern only because no case-bearing element is non-ASCII, and
# NOTHING ENFORCES THAT -- it is an eyeball check, so do NOT copy the flag onto another pattern
# on this comment's authority. `re.A` also narrows `\s`, `\d`, `\w` and `\b`, so "the pattern is
# pure ASCII" is not the test either; a pattern carrying `\d` would pass that reading and still
# be changed underneath you.
#
# NOT_WORD_AFTER stands in for the oracle's `\b` (`ledger-check.mjs:270`), and that rewrite is
# only valid while the quantifier is `+`. `\b` is two-sided; the lookahead is one-sided, and
# they agree solely because `[0-9]+` guarantees a word char to the left. Change it to `*` and
# `**Unit ` with no number matches here and not in node.
UNIT_HEADER = re.compile(r"^\*\*unit" + JS_WS_CLASS + r"+([0-9]+)" + NOT_WORD_AFTER, re.I | re.A)

# The table-header FINDER, mirroring `ledger-check.mjs:47`'s `/^\|\s*#\s*\|/`. JS_WS_CLASS, never
# Python `\s` -- this is the single most consequential of the class's sites, because it does not
# decide how a row parses, it decides whether the file IS A LEDGER AT ALL.
#
# MEASURED before the fix, one byte after the header row's leading `|`, both runtimes, plus three
# controls (U+0020, U+0009, no pad -- all agreeing at exit 0, which is what proves the probe
# reached this line rather than failing earlier):
#   U+001C U+001D U+001E U+001F U+0085 -> node exit 2 `not a DELEGATION.md ledger` but port
#     exit 0 `ledger complete: every unit verified`. The port FAILED OPEN: it certified the work
#     done on a file the oracle refuses to parse.
#   U+FEFF -> the reverse (node 0, port 2), because that code point is the one JS `\s` matches
#     and Python's does not.
# Both directions come from the same disagreement, so the class substitution fixes both at once.
#
# Compiled at module level rather than inline in the `next()` below, deliberately: UNIT_HEADER
# above is the house form, eight more `\s` sites are still to be converted and will copy whatever
# this one does, and an inline four-term concatenation buried in a generator expression is the
# one shape in this file with no precedent.
TABLE_HEADER = re.compile(r"^\|" + JS_WS_CLASS + r"*#" + JS_WS_CLASS + r"*\|")

TRAILING_PIPE = re.compile(r"(^|[^\\])\|$")
SEPARATOR_CELL = re.compile(r"^:?-+:?$")
RECEIPT_RE = re.compile(r"\n?<!-- agents-discipline-check: [^>]*-->\n?")
PRIOR_RECEIPT = re.compile(r"<!-- agents-discipline-check: ([^ ]+) sha256:([0-9a-f]+) -->")
# TWO AXES ON ONE REGEX, both converted: the whitespace class (`\s` -> JS_WS_CLASS) and the LINE
# ANCHOR. The oracle is `/^Created:?\s+.../m`, and ECMAScript's `^` under `/m` fires after ANY
# LineTerminator -- LF, CR, U+2028, U+2029 -- while Python's `re.M` fires after LF alone. So
# `re.M` is NOT the port of `/m`: on `x<CR>Created: 2099-...` the oracle reads a Created date and
# the port reads none, the staleness rule is silently skipped, and a stale artifact passes. The
# alternative below spells the four terminators and drops `re.M`; `^` without it is
# start-of-string, the fifth position `/m` fires at. The two non-ASCII terminators are `\N{...}`
# escapes, never literal characters, so they survive an editor, a paste and a normalizing diff.
# Gated by whitespace-diff.sh's `created` surface (the `\s` axis) and `created-lt` surface (the
# anchor axis, in BOTH directions: one `same` row per terminator against a narrowed anchor, and
# a NEL row against a widened one -- U+0085 breaks lines to Python and not to ECMAScript).
CREATED = re.compile(
    r"(?:^|(?<=[\n\r\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]))Created:?" + JS_WS_CLASS
    + r"+([0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:]+(?:[+-][0-9]{2}:?[0-9]{2})?)"
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
    # ledger-check.mjs:41 interpolates `err.message`, which is node's fs shape and NOT Python's
    # str(). MEASURED on a missing ledger, the same divergence 9071a84 fixed in gate-lint:
    #     oracle  ... : ENOENT: no such file or directory, open '<path>'
    #     port    ... : [Errno 2] No such file or directory: '<path>'
    # THE SECOND SITE OF ONE DEFECT, and the reason it survived the commit that fixed the first
    # is that no sweep followed it -- the same way the EXPECT-warning ensure_ascii defect
    # survived the lease-write commit that fixed its twin. Found by argv-diff.sh on its first
    # run, driving a second CLI purely because "every CLI" had been claimed at three of four
    # before. The `errno is not None` guard preserves the helper's OWN raised errors (kind check,
    # size cap), which carry no errno and whose text is already the message the oracle prints.
    fail(2, "agents-discipline: cannot read " + path + ": " +
            (node_fs_message(err, "open") if err.errno is not None else str(err)))

lines = text.split("\n")

# Find the table: a line starting with `| # |` is the header we accept.
header_idx = next((i for i, l in enumerate(lines) if TABLE_HEADER.match(l)), -1)
if header_idx == -1:
    fail(2, f"agents-discipline: {path} is not a DELEGATION.md ledger (no unit table header)")

# The header's width is the contract every row is held to. Without it the only shape check
# was `len(cells) < 6`, which cannot see the failure that actually happens: an UNESCAPED pipe
# inside an Acceptance command adds a column, so a 6-column row becomes 7 cells, passes the
# `< 6` test, and `Status` is then read from the wrong cell. The row parses silently as
# whatever the shifted text happens to say. Widening the table has the mirror problem: a row
# omitting the new trailing columns reads them as missing forever, with no error.
_header = lines[header_idx].strip(JS_TRIM)
column_count = (
    len(_header.replace("\\|", " ").split("|"))
    - 1
    - (1 if TRAILING_PIPE.search(_header) else 0)
)

rows = []
malformed = []
for i in range(header_idx + 1, len(lines)):
    line = lines[i].strip(JS_TRIM)
    if not line:
        continue
    if not line.startswith("|"):
        break  # table ended
    # A trailing pipe only delimits when it is UNESCAPED. Testing the RAW line while splitting
    # the ESCAPED one silently dropped a last cell ending in `\|` -- and since the header used
    # the same wrong test, the two agreed and no malformed error fired. The column vanished.
    parts = line.replace("\\|", "\0").split("|")
    cells = [c.strip(JS_TRIM).replace("\0", "|") for c in parts[1:(-1 if TRAILING_PIPE.search(line) else None)]]
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

# `abandoned` is TERMINAL-but-unsuccessful: the unit cannot be finished, its Evidence block
# carries the reason, and the ledger reports a required handoff rather than completion. Kept OUT
# of `unverified` because those two say opposite things -- one is "still coming", the other
# "never coming". Mirrors the gate half (`gate_check` HANDOFF REQUIRED, exit 1).
counts = {"pending": 0, "done": 0, "verified": 0, "abandoned": 0, "other": 0}
unverified = []
abandoned = []
for r in rows:
    # Membership in `counts` itself, matching the oracle's hasOwnProperty test exactly -- a dict
    # inherits no keys, so a status of `constructor`/`toString` lands in `other` here and used to
    # VANISH in node (prototype lookup made its `=== undefined` guard false and stored NaN). The
    # oracle was fixed to agree; this line is the shape both runtimes now share.
    if r["status"] in counts:
        counts[r["status"]] += 1
    else:
        counts["other"] += 1
    if r["status"] == "abandoned":
        abandoned.append(r)
    elif r["status"] != "verified":
        unverified.append(r)


def is_strong_evidence(l):
    """True when a line names something concrete: a command-shaped code span, a file path, or
    a measured result. Single words and generic phrases fail on purpose, so "verified" or
    "`done`" do not pass."""
    for s in CODE_SPAN.findall(l):
        inner = s[1:-1].strip(JS_TRIM)
        # `\s` -> JS_WS_CLASS. The two divergence directions do NOT have the same reach, and the
        # first draft of this comment claimed they did -- MEASURED after a review caught it:
        #   U+FEFF IS in the JS trim set, so the `strip(JS_TRIM)` above removes it at the EDGES.
        #     It reaches this test only from INSIDE the span: `pyt<U+FEFF>est` bears whitespace
        #     to node and is a runner-word miss to the port.
        #   U+001C..U+001F and U+0085 are NOT in that set, so they survive at EVERY position --
        #     leading, trailing, and as the whole span (`pytest<U+0085>`.strip(JS_TRIM) is still
        #     7 characters). Each is whitespace to Python and an ordinary character to node.
        # So "the strip leaves only interior pads" is true of ONE code point and false of five.
        # Both directions reach the VERDICT, not merely the predicate: `evidence: present` vs
        # `MISSING`, hence `-> ledger complete` against `-> ledger INCOMPLETE.` on the same bytes.
        #
        # All five were measured at all three positions, not three measured and two inferred from
        # the trim-set constant -- an earlier draft did the latter under a MEASURED heading.
        #
        # GATED AT THE INTERIOR POSITION ONLY -- that is the one shape where all six code points
        # diverge, so it is the only shape a single writer can cover. The edge positions are fixed
        # by this same expression and are NOT separately gated. PREDICTION, not a result: a writer
        # padding the span EDGE should red for the five and pass for U+FEFF (stripped at the edges,
        # so both runtimes see the bare runner word). Nobody has run it.
        #
        # `[/.]` NEEDS NO CONVERSION -- two literal ASCII characters, no shorthand class.
        #
        # The sibling `\s` in `acceptance_command` is deliberately NOT fixed with it. A command
        # returned from there is what makes the re-run EXECUTE it, and the differential ships only
        # non-runnable acceptances by design -- so no row in that suite can gate that site, and a
        # fix landing there would ship ungated. It is the more consequential of the two (it decides
        # whether a command RUNS), so it gets its own harness rather than a ride. TRDD-REJRD8V5.
        if re.search(JS_WS_CLASS, inner) or re.search(r"[/.]", inner):
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
    l = lines[i].strip(JS_TRIM)
    if l == "":
        continue
    # `\s` -> JS_WS_CLASS on BOTH expressions. These are one construct but two decisions, with
    # different blast radii: the first says whether the line is a HEADING at all (a heading is
    # dropped from the evidence scan entirely, so it decides what can corroborate a row), the
    # second whether it is the boilerplate section to skip. Measured in both directions --
    # `##<U+001C>Notes` was a heading to the port alone, `##<U+FEFF>Notes` to node alone.
    # Gated by whitespace-diff.sh's `heading-finder` and `rules-heading` surfaces, one writer
    # per line, so reverting either of the two alone reds exactly one of them.
    #
    # `$` NEEDS NO CONVERSION, and that is measured rather than skipped: `lines` is
    # `text.split("\n")`, so `l` contains no newline and Python's match-before-a-final-newline
    # case has nothing to fire on. Here `$` is end-of-string in both runtimes.
    #
    # `re.I | re.A`, not bare `re.I`: Python's `re.I` folds U+017F (and U+0131/U+0130/U+212A)
    # onto ASCII where JS `/i` without `/u` refuses, so a heading with U+017F in `Rules` was the
    # boilerplate heading to the port alone -- the port skipped a section the oracle scored as
    # evidence (MEASURED: `evidence: MISSING` here, `present` in node, on the fixture below).
    # `re.A` is safe HERE by the `:111-117` precondition: no `\s`/`\w`/`\b`/`\d` remains for it
    # to narrow (the whitespace is `JS_WS_CLASS`, an explicit class), and its ASCII-only folding
    # has nothing to refuse -- every case-bearing element is ASCII, and the class members are
    # whitespace, which has no case. Gated by `tests/fixtures/rules-heading-fold.md` and its
    # ledger-tests case under `AD_RUNTIME=python node tests/ledger-tests.mjs`; only U+017F is
    # exercised there, the other three fold characters are named as examples, not as gated.
    if re.match(r"^##" + JS_WS_CLASS + r"+", l):
        in_rules_section = bool(
            re.match(
                r"^##" + JS_WS_CLASS + r"+Rules of this ledger" + JS_WS_CLASS + r"*$",
                l,
                re.I | re.A,
            )
        )
        continue
    if l.startswith("|"):
        continue  # still in the table
    # STICKY until the next heading, on purpose -- the rules section is several lines. Gated by
    # ledger-tests' "copy the template and flip every status" case under
    # `AD_RUNTIME=python node tests/ledger-tests.mjs`: the template's own rules bullets score
    # strong if leaked, so an early reset reds `evidence: MISSING` there; its arming case checks
    # that those bullets still score strong when the section is not skipped.
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
artifact_paths = [m.strip(JS_TRIM) for m in CITATION.findall(evidence_text)]

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
    return evidence_blocks.get(str(unit).strip(JS_TRIM), "")


def existing_artifacts_in(block, bases):
    found = []
    for rel in (m.strip(JS_TRIM) for m in CITATION.findall(block)):
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
        # Only the forms ISO 8601 actually permits, because hour 24 is legal ONLY as exactly
        # 24:00:00. Measured: Date.parse("2020-01-01T24:00:00") is next-day local midnight,
        # and Date.parse("2020-01-01T24:00:01") is NaN. A loose `(:.*)?` rewrote the second
        # into a parseable datetime, so the port ENFORCED staleness where the oracle skips it
        # — the opposite direction from the bug this fix is for, and just as silent.
        m24 = re.match(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})T24(:00(?::00)?)?((?:[+-].*)?)$", raw)
        if m24:
            raw = (datetime.fromisoformat(m24.group(1)) + timedelta(days=1)).strftime("%Y-%m-%d") \
                + "T00" + (m24.group(2) or "") + m24.group(3)
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
        inner = s[1:-1].strip(JS_TRIM)
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
    #
    # `\s` -> JS_WS_CLASS at ALL THREE occurrences, in ONE edit: they are alternatives of one
    # pattern deciding one verdict, so converting two would leave the third diverging on the
    # same bytes. The divergence runs BOTH WAYS -- Python's `\s` also matches U+001C..U+001F
    # and U+0085, node's also matches U+FEFF -- so this is not "strip more", it is "strip
    # exactly node's set", which is what JS_WS_CLASS is.
    #
    # This comment quotes `\s` literally, so a grep scoped to this whole block counts those
    # occurrences too. Scope any such count to the two pattern lines below, never to the span:
    # a count over the span reads 2, which is the comment, not the pattern.
    #
    # Stated as MECHANISM, not as a verdict pair: `exit<U+001C>0` is a no-op to the unconverted
    # port and an ordinary command to node. Which line each runtime then PRINTS depends on the
    # row's artifacts, so a comment asserting two verdicts would be true of one fixture only.
    # tests/fixtures/noop-pad-divergence.md carries one vector per occurrence.
    #
    # `[^&|;]` needs no conversion -- three literal characters, no shorthand class.
    #
    # Adjacent f-string literals, never `+` across a line break: mixing implicit concatenation
    # with `+` makes the grouping depend on where the line happens to wrap.
    #
    # The `re.I` below is a SEPARATE and still-open divergence at this same site -- Python folds
    # U+017F onto `s`, so `sleep` spelled with a long s matches here and not in the oracle.
    # Measured, NOT fixed here: the `\s` conversion above is complete and mutation-proven, and
    # the fold is what still leaves this site short of oracle-equivalent.
    rf"^(?:(?:/(?:usr/)?bin/)?(?:true|echo|printf|pwd|sleep)(?:{JS_WS_CLASS}+[^&|;]*)?"
    rf"|:|command{JS_WS_CLASS}+true|exit{JS_WS_CLASS}+0)\Z",
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
    s = str(raw).strip(JS_TRIM)
    # `( true )` and `{ true }` wrap the same command. The balance check is load-bearing:
    # an anchored `^\(...\)$` mangles `(echo a) && (echo b)` into `echo a) && (echo b`.
    for _ in range(4):
        before = s
        for open_c, close_c in (("(", ")"), ("{", "}")):
            if s.startswith(open_c) and s.endswith(close_c) and wraps_whole(s, open_c, close_c):
                s = s[1:-1].strip(JS_TRIM)
        if s == before:
            break
    if not s:
        return False
    # Judged on the MASKED text: a quoted `&&` is an argument, not a link.
    parts = [p.strip(JS_TRIM) for p in mask_quoted(s).split("&&")]
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
    # `_`: JS numeric separators are a LITERAL feature; Number("1_000") is NaN.
    # `isascii()`: Python's float() accepts non-ASCII decimal digits and Number() does not.
    # Measured — `１２３` (fullwidth) and `١٢٣` (Arabic-Indic) are both 123.0 to float and NaN
    # to Number, so a budget written in them would run in the port and hard-fail in the
    # oracle. Same "one variable, two meanings" class as the `1_000` bug this replaced.
    if "_" in s or not s.isascii():
        return float("nan")
    try:
        for prefix, base in (("0x", 16), ("0o", 8), ("0b", 2)):
            if s[:2].lower() == prefix:
                return float(int(s[2:], base))
        return float(s)
    except ValueError:
        return float("nan")


_budget_raw = (os.environ.get("AGENTS_DISCIPLINE_RERUN_BUDGET_MS") or "").strip(JS_TRIM)
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
        spans = [s[1:-1].strip(JS_TRIM) for s in CODE_SPAN.findall(r["acceptance"])]
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
if counts["abandoned"]:
    print(f"  abandoned:   {counts['abandoned']}")
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
if abandoned:
    # The reason lives in the row's Evidence block, deliberately NOT copied here: it is free-form
    # text of unbounded length, and the gate half makes the same choice.
    #
    # The row IS now checked for having one, and the wording is the careful part. The predicate is
    # an EMPTY attributed block, which means "no line-initial `**Unit N —**` block was attributed
    # to this row" -- NOT "no reason was written". Those come apart in shapes a careful author
    # actually uses: one pooled paragraph covering three units blocked on the same cause, a
    # `#1`-style header, an indented header inside a list. In every one of those the reason exists
    # and this check cannot see it, so the line says what was MEASURED. Claiming "NO REASON GIVEN"
    # would put an unsupportable accusation about a correct author into the ledger's own output.
    print(f"  HANDOFF REQUIRED: {len(abandoned)} abandoned unit(s) — terminal and unsuccessful, not completion:")
    for r in abandoned:
        # See the oracle's comment: a header-only block is NON-EMPTY (the header line is part of
        # the block it opens), so emptiness alone was defeated by typing `**Unit 1 —**` and
        # nothing else. `[ \t]` rather than `\s` keeps this identical to the JS side, whose `\s`
        # includes U+FEFF while Python's does not.
        _reason = re.sub(
            r"^\*\*unit[ \t]+[0-9]+[ \t]*[-—–:]*[ \t]*\*\*", "", evidence_block_for(r["unit"]), 1, re.I
        ).strip(JS_TRIM)
        _why = f" — no reason found in a **Unit {r['unit']}** evidence block" if _reason == "" else ""
        print(f"    - #{r['unit']} {r['name']}{_why}")
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
# `.rstrip(JS_TRIM)`, NOT a bare `.rstrip()`. This mirrors `ledger-check.mjs:691`'s `.trimEnd()`,
# and the two disagree on exactly the six code points this port has been audited for all along:
# `trimEnd()` removes U+FEFF and keeps U+001C-U+001F/U+0085; bare `.rstrip()` does the reverse.
# (JS defines regex `\s` and `trimEnd()` over the SAME set -- WhiteSpace + LineTerminator -- so the
# `\s`-derived JS_TRIM is the exact argument, not an approximation.)
#
# This was the last bare strip in the file -- verified by parsing out every `.strip(`/`.rstrip(`/
# `.lstrip(` call in this module (comments excluded) and checking each one's argument: ZERO are
# bare. No count is given on purpose. The first version of this line said "all 17", counted by
# grep -- which counts LINES, not calls, and included three comment lines; the real figure is 16.
# The claim that carries weight is "no bare call remains", and it needs no numerator.
# It was also the one that mattered most, because
# `body_for_hash` is NOT only hashed -- `fh.write(body_for_hash + stamp)` below writes it BACK to
# the user's ledger (cited by NAME, not line number: the first version of this comment said
# "line 813", which this very comment block then pushed to 831). So the bare
# form did not merely compute a different digest: the two runtimes REWROTE the document
# differently, python deleting a trailing U+001C that node preserves and preserving a trailing
# U+FEFF that node deletes. A step the user runs to VERIFY a file was quietly editing it, and
# editing it differently depending on which runtime they had.
#
# The visible symptom was the receipt: sign under one runtime, re-check under the other, and the
# next run printed `receipt: STALE -- ledger content changed since it was last checked` on a file
# NO HUMAN had touched. The precise wording matters: the file HAD changed, by this very checker,
# so "a file nobody changed" is false and "tampering" would be an accusation the stamp explicitly
# declines to make. The message is factually accurate and misattributes by OMISSION -- it names no
# agent, and the only agent the reader knows about is themselves.
# Measured in both directions, with an unpadded control that stayed clean.
# That line is only a `print` and never reaches the exit code, so the stale stamp was the mild
# half; the divergent rewrite was the real defect.
body_for_hash = RECEIPT_RE.sub("\n", text).rstrip(JS_TRIM) + "\n"
digest = hashlib.sha256(body_for_hash.encode("utf-8")).hexdigest()[:16]

prior = PRIOR_RECEIPT.search(text)
if prior and prior.group(2) != digest:
    print(f"  receipt:     STALE — ledger content changed since it was last checked ({prior.group(1)})")
elif prior:
    print(f"  receipt:     binds this exact content, last checked {prior.group(1)}")

# Three-way, not two. An abandoned ledger is not "INCOMPLETE" -- that word promises the work is
# still coming. It is terminal, and the exit code is unchanged (1 either way); only the claim
# changes, which was the whole defect.
if complete:
    print("  -> ledger complete: every unit verified.")
# `and not unverified` is load-bearing: TERMINAL is a claim about the WHOLE ledger, and one
# abandoned row among four pending ones does not make the ledger terminal. Without it this branch
# outranks every other incomplete reason and prints TERMINAL over work in flight -- the same
# over-claim as the defect this change fixes, with the sign flipped. Both facts still reach the
# reader on separate lines (the HANDOFF REQUIRED section above is unconditional), which is
# gate_check's shape: it prints HANDOFF REQUIRED and UNMET independently, never one instead of
# the other.
elif abandoned and not unverified:
    print("  -> ledger TERMINAL: HANDOFF REQUIRED — abandoned unit(s) will not be finished.")
else:
    print("  -> ledger INCOMPLETE.")

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
