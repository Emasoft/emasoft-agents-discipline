#!/usr/bin/env bash
# Differential for CELL TRIMMING in the delegation ledger parser.
#
# WHY THIS FILE EXISTS: `ledger-check.mjs:74` normalizes every table cell with `c.trim()` and
# `ledger_check.py:142` with `c.strip()`. Those are NOT the same function, and the difference is
# not a corner nobody reaches -- it decides what the Status column SAYS. Measured across the
# whole Unicode range (0..0x10FFFF, surrogates skipped) on 2026-09-07:
#
#   JS trim() strips, Python strip() does NOT:   U+FEFF
#   Python strip() strips, JS trim() does NOT:   U+001C U+001D U+001E U+001F U+0085
#
# Six code points, and the divergence runs BOTH WAYS -- so "make Python strip more" is not the
# fix, and a test that only covered U+FEFF would have said the port was correct on the other
# five. The first measurement of this scanned 0..0x3001, reported "JS strips nothing Python
# does not", and was exactly wrong: U+FEFF is 0xFEFF, outside the range. A scan's range is part
# of its result.
#
# THE USER-VISIBLE CONSEQUENCE is an exit-code split on a file nobody edited by hand. A ledger
# saved as UTF-8-with-BOM by a Windows editor puts U+FEFF at the head of the FIRST cell; a row
# whose Status is a BOM-prefixed `verified` reads `verified` under node (status recognized,
# ledger complete, exit 0) and `﻿verified` under python3 (unrecognized, counted as `other`,
# ledger INCOMPLETE, exit 1). Same bytes, same command, opposite verdicts.
#
# WHY NO EXISTING SUITE SEES IT: encoding-diff.sh covers what the port WRITES and PRINTS, not
# what it PARSES, and its fixtures are authored ASCII. argv-diff.sh and errno-message-diff.sh
# reach ledger-check only through a MISSING or UNREADABLE file -- they fail at the read and
# never reach the table parser. A differential only tests the vectors it presents.
#
# DIVERGE LINES START AT COLUMN 0 (see gate-args-diff.sh's note); every other verdict word keeps
# a 2-space indent so mutate-probe.sh's anchored `^DIVERGE` grep stays the only trigger.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORACLE="$HERE/../scripts/ledger-check.mjs"
PORT="$HERE/../scripts/ledger_check.py"
export PYTHONDONTWRITEBYTECODE=1
PY_ABS="$(command -v python3)"
NODE_ABS="$(command -v node)"

# The re-run loop executes acceptance commands. Every fixture here carries a NON-runnable
# acceptance on purpose, but the skip is set anyway: this suite is about the PARSER, and a
# fixture that grew a code span later must not silently start executing it.
export AGENTS_DISCIPLINE_SKIP_RERUN=1

WORK="$(mktemp -d)"
[ -n "$WORK" ] && [ -d "$WORK" ] || { echo "mktemp -d failed; refusing to build fixtures" >&2; exit 2; }
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
declare -a FAILED=()
# The unpadded oracle verdict per writer, recorded by that writer's control and read by its cases.
# Keyed by writer so the ordering constraint -- every `_control` runs before its own loop -- is
# checked rather than assumed: `_case` refuses an empty baseline instead of comparing against one.
declare -A BASE=()

# Every code point is written as EXPLICIT UTF-8 BYTES, never a literal character. A literal would
# have to survive this file, git, and every editor between here and the runner; encoding-diff.sh
# records a case where exactly that path turned an escape into a raw character. Bytes cannot
# round-trip into something else.
#   U+FEFF -> ef bb bf     U+001C..U+001F -> 1c..1f     U+0085 -> c2 85
# Code point only -- each loop below prepends the SURFACE it puts the bytes on. The names used to
# end in "around status", which stayed in the label when the header loop reused the array and
# produced "header U+FEFF (BOM) around status": a label naming two different surfaces at once.
declare -a CASE_NAME=(
  "U+FEFF (BOM)"
  "U+001C (file separator)"
  "U+001D (group separator)"
  "U+001E (record separator)"
  "U+001F (unit separator)"
  "U+0085 (NEL)"
)
declare -a CASE_BYTES=(
  '\xef\xbb\xbf'
  '\x1c'
  '\x1d'
  '\x1e'
  '\x1f'
  '\xc2\x85'
)

# What each pad must do to THE ORACLE'S OWN verdict, measured against the same fixture with an
# empty pad. This is not a second opinion about the port -- it is JS `\s`/`trim()` semantics, the
# thing every row below exists to protect, asserted directly instead of inferred from agreement.
#
# node treats U+FEFF as whitespace, so padding with it is INVISIBLE to the oracle and the verdict
# must not move. node treats the other five as ORDINARY CHARACTERS, so each must break the parse
# it lands in and MOVE the verdict. MEASURED, all 18 cells (6 code points x 3 writers), and the
# split is identical on every surface -- cell, header and evidence-header.
#
# WHY THIS EXISTS, and it is a hole the anchor below could not close. The anchor pins the pad to a
# LINE. Move it WITHIN that line to a position the whitespace class never governs and every check
# passed: `**Unit 2%s —**` instead of `**Unit %s2 —**` puts the byte after the digit, at a `\b`
# both runtimes handle identically, and the suite exited 0 with all six evidence-header rows
# comparing broken-to-broken. Measured, not hypothesized.
#
# The discriminating power is the OPPOSITION between row 0 and rows 1-5, and it is why a single
# expected value would not do. A position where the pad is inert makes all six `same`, so the five
# fire. A position where every pad breaks the parse makes FEFF `differ`, so row 0 fires. Only a
# position where FEFF is invisible AND the other five are not can satisfy both -- and that IS the
# `\s`/`trim()` boundary. A maintainer who reds this suite and "fixes" it by flipping cells to
# match whatever the oracle now does destroys that opposition and restores the vacuum; the
# measurement, not the current output, is what these values must be re-derived from.
declare -a CASE_ORACLE_EFFECT=(
  same    # U+FEFF -- js trim()/\s strip it; the oracle cannot see the pad at all
  differ  # U+001C
  differ  # U+001D
  differ  # U+001E
  differ  # U+001F
  differ  # U+0085
)

# Writes a one-row ledger whose Status cell is `verified` wrapped in $2 (a printf byte escape,
# empty for the control). One row and one unit, so the ONLY thing that can move the verdict is
# whether that cell parsed as `verified`.
#
# The acceptance cell is the non-runnable words `tests pass`, matching fixtures/abandoned-row.md
# and for the reason stated there: `evidence: present` comes from the `## Evidence` section, never
# from the Acceptance column, so a runnable command buys nothing and arms a landmine for whoever
# later turns the re-run on.
# shellcheck disable=SC2329  # invoked indirectly, as "$writer" from _case and _control
_write_ledger() {
  local dest="$1" ws="$2" pad
  # `%b` expands the escapes in the ARGUMENT. The first spelling was `printf "$ws"`, which puts
  # the fixture's own bytes in the FORMAT position -- shellcheck SC2059, and not a style note
  # here: a `%` ever appearing in a case's byte string would be read as a conversion spec and
  # silently consume the next argument, producing a fixture nobody wrote.
  pad="$(printf '%b' "$ws")"
  {
    printf '# Delegation plan\n'
    printf 'Units: 1\n\n'
    printf '| # | Unit | Files (mine) | Worker | Acceptance | Status |\n'
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | %sverified%s |\n' "$pad" "$pad"
    printf '\n## Evidence\n\n'
    printf '**Unit 1 —** ran the suite by hand; 12 passed.\n'
  } > "$dest"
}

# Same ledger, but the whitespace goes on the HEADER line instead of a Status cell. This is the
# strictly worse vector and it is why one cell case was not enough coverage: the header decides
# `column_count` (`ledger-check.mjs:58`, `ledger_check.py:144`), which every row is then held to.
# So a code point one runtime trims and the other does not shifts the count by one there, and the
# consequence is not a mis-read cell -- it is EVERY row reported malformed in one runtime and
# parsed normally in the other, from a byte no editor displays.
#
# ALL SIX MEASURED on this surface against the pre-fix port (94027f7), not inferred from the cell
# result -- and the measurement is why: U+FEFF diverges the OPPOSITE WAY from the other five.
#
#   node exit 0 `verified: 1`  vs  pre-fix port exit 2 `every row is malformed`   <- U+FEFF only
#   node exit 2 `every row is malformed`  vs  pre-fix port exit 0 `verified: 1`   <- the other five
#
# So the port was PERMISSIVE on five vectors and OVER-STRICT on the sixth, and a header ledger that
# one runtime closes is one the other refuses outright. Arguing the five by symmetry from the one
# would have predicted the wrong direction here; the first version of this file asserted exactly
# that symmetry and only U+001C had actually been run.
# FOURTH WRITER: pads the header row between its leading `|` and the `#`, which is the only
# position the header FINDER's whitespace class governs. `_write_ledger_hdr` below pads the row's
# TRAILING position and so reaches `column_count` instead -- a different expression, a different
# failure, and it is why nothing here had ever exercised the finder.
# shellcheck disable=SC2329  # invoked indirectly, as "$writer" from _case and _control
_write_ledger_find() {
  local dest="$1" ws="$2" pad
  pad="$(printf '%b' "$ws")"
  {
    printf '# Delegation plan\n'
    printf 'Units: 1\n\n'
    printf '| %s# | Unit | Files (mine) | Worker | Acceptance | Status |\n' "$pad"
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | verified |\n'
    printf '\n## Evidence\n\n'
    printf '**Unit 1 —** ran the suite by hand; 12 passed.\n'
  } > "$dest"
}

# shellcheck disable=SC2329  # invoked indirectly, as "$writer" from _case and _control
_write_ledger_hdr() {
  local dest="$1" ws="$2" pad
  pad="$(printf '%b' "$ws")"
  {
    printf '# Delegation plan\n'
    printf 'Units: 1\n\n'
    printf '| # | Unit | Files (mine) | Worker | Acceptance | Status |%s\n' "$pad"
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | verified |\n'
    printf '\n## Evidence\n\n'
    printf '**Unit 1 —** ran the suite by hand; 12 passed.\n'
  } > "$dest"
}

# THIRD SURFACE: the `**Unit N —**` evidence header. Neither writer above can reach it -- both
# emit that line as fixed text -- and it is parsed by a DIFFERENT expression from the table, so a
# divergence here is invisible to all twelve cases above.
#
# The pad goes AFTER a literal space, not instead of one: with $ws empty this must emit exactly
# `**Unit 1 —**` so the control is a real header. Replacing the space would make the control
# `**Unit1 —**`, which matches no detector in either runtime, and every case below would then
# agree on a ledger where the block never opened -- twelve vacuous OKs.
#
# shellcheck disable=SC2329  # invoked indirectly, as "$writer" from _case
_write_ledger_ev() {
  local dest="$1" ws="$2" pad
  pad="$(printf '%b' "$ws")"
  {
    printf '# Delegation plan\n'
    printf 'Units: 2\n\n'
    printf '| # | Unit | Files (mine) | Worker | Acceptance | Status |\n'
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | verified |\n'
    printf '| 2 | finance | app/finance.py | worker-2 | tests pass | abandoned |\n'
    printf '\n## Evidence\n\n'
    printf '**Unit 1 —** ran the suite by hand; 12 passed.\n\n'
    printf '**Unit %s2 —** the upstream API was withdrawn; the work cannot finish.\n' "$pad"
  } > "$dest"
}

# FIFTH SURFACE: `EXIT_CODE` (oracle `:161`, `/exit\s+\d+/i`), one of the three strong-evidence
# predicates. The four writers above cannot reach it -- MEASURED, not argued: 0 of the 31 inputs
# `ledger-tests.mjs` drives reach it either, because `FILENAME_SHAPED` and `MEASURED_RESULT` are
# tried first and the existing evidence lines all say "12 passed", which `MEASURED_RESULT` claims.
# So before this writer, NOTHING in the repo watched that line in either runtime.
#
# The evidence sentence is chosen so EXIT_CODE is the SOLE path to strong evidence, or the case
# would pass on a sibling predicate and assert nothing about this one:
#   - no `.`, so `FILENAME_SHAPED` (which needs a dot-extension) cannot fire;
#   - no `pass`/`passing`/`passed`/`ok` after the digit, so `MEASURED_RESULT` cannot.
#
# The pad goes AFTER the existing space (`exit %s3`), never instead of it, for the reason
# `_write_ledger_ev` documents: with an empty pad this must emit `exit 3`, which the oracle DOES
# match. Replacing the space would make the control `exit<pad>3` -- unmatched in both runtimes,
# so every case below would agree on a ledger where the predicate never fired.
# shellcheck disable=SC2329  # invoked indirectly, as "$writer" from _case and _control
_write_ledger_exit() {
  local dest="$1" ws="$2" pad
  pad="$(printf '%b' "$ws")"
  {
    printf '# Delegation plan\n'
    printf 'Units: 1\n\n'
    printf '| # | Unit | Files (mine) | Worker | Acceptance | Status |\n'
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | verified |\n'
    printf '\n## Evidence\n\n'
    printf '**Unit 1 —** the runner ended with exit %s3\n' "$pad"
  } > "$dest"
}
# THIRD INVARIANT OF THE LINE ABOVE, and it is the one a future editor is most likely to break
# because breaking it looks like an improvement: NO BACKTICKS. `is_strong_evidence` has a
# code-span branch that runs BEFORE the two blockers named above, so backticking a path or a
# command here would satisfy the evidence check by that route instead, `EXIT_CODE` would stop
# being the sole path, and all six rows would go green while testing nothing.

# SIXTH SURFACE: `CREATED` (oracle `:303`). Unlike the exit-code surface this one IS reached by
# ordinary inputs -- 16 of the 31 `ledger-tests.mjs` drives carry a `Created:` line -- so `npm
# test` is already a real gate for its ORDINARY-INPUT behaviour. What no test reached is its
# whitespace class: not one writer in this file emitted a `Created:` line before this one, so the
# divergence axis was unwatched in both runtimes.
#
# THIS WRITER IS THE ONLY ONE THAT HAS TO BUILD A SIDE FILE, because `CREATED` has exactly one
# consumer and it is a comparison, not a parse. `createdMs` is used at `:321` for ONE thing:
# `st.mtimeMs < createdMs` -> the cited artifact is STALE. So the pad can only move the verdict
# when a cited artifact EXISTS -- otherwise it lands in `missingArtifacts` and the staleness
# branch is never reached, the verdict is the same either way, and all six cases report a vacuous
# `same`. Hence `art/created.md`, created next to the ledger (`bases[0]` is the ledger's own
# directory, so a relative citation resolves there), and a Created date in 2099 so a file written
# *now* is unambiguously older than it. No `touch` needed: the future date does that work.
#
# The polarity is therefore INVERTED from what "the regex stopped matching" suggests, and it is
# worth stating because it is what makes the vectors discriminate. A pad the oracle's `\s+`
# swallows (U+FEFF) leaves the match intact -> `createdMs` finite -> STALE. A pad it does not
# swallow kills the match -> `createdMs` NaN -> the staleness rule is SKIPPED and the ledger
# reads clean. Same/differ split as every surface above, so `CASE_ORACLE_EFFECT` still applies --
# and it applies for the same PRECONDITION, not merely to avoid a copy: the split is a property
# of the codepoint under JS `\s`, and the pad here sits where JS `\s` governs.
#
# The pad goes after the colon and AFTER the existing space, same convention as every writer
# above: with an empty pad this must emit `Created: <date>`, which the oracle matches.
#
# TWO INVARIANTS, and the first is the INVERSE of the NO-BACKTICKS rule on the exit-code writer
# two functions up -- do not cargo-cult that note onto this one.
#   1. The citation must stay a BARE backticked PATH. Turn it into a command
#      (`` `cat art/created.md` ``) and the citation regex yields nothing, `artifactPaths` is
#      empty, the staleness branch is unreachable, and all six rows go vacuously `same`.
#   2. The side file must stay NON-EMPTY. `if (st.size === 0) emptyArtifacts.push(p); else if
#      (...createdMs...)` -- an empty artifact takes the FIRST branch and `createdMs` is never
#      consulted, so a `touch` or `: >` in place of the `printf` kills the surface.
# Both fail LOUD rather than silent: `_control`'s pad-had-no-effect check fires. They are
# comments because they are already asserted, not because they are unasserted.
# shellcheck disable=SC2329  # invoked indirectly, as "$writer" from _case and _control
_write_ledger_created() {
  local dest="$1" ws="$2" pad
  pad="$(printf '%b' "$ws")"
  mkdir -p "$(dirname "$dest")/art"
  printf 'ran it\n' > "$(dirname "$dest")/art/created.md"
  {
    printf '# Delegation plan\n'
    printf 'Units: 1\n'
    printf 'Created: %s2099-01-01T00:00:00+0000\n\n' "$pad"
    printf '| # | Unit | Files (mine) | Worker | Acceptance | Status |\n'
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | verified |\n'
    printf '\n## Evidence\n\n'
    # shellcheck disable=SC2016  # the backticks are a literal markdown code span, not a command
    printf '**Unit 1 —** ran the suite by hand; wrote `art/created.md`.\n'
  } > "$dest"
}

# SEVENTH AND EIGHTH SURFACES: the two `##` heading expressions (oracle `:181-182`, port
# `ledger_check.py:376-377`). They are ONE two-line construct in the source and TWO writers here,
# and the split is the point -- a single writer would leave one of the two lines revertible with
# this suite still green, which is the mutation a single-writer version of this could not kill.
#
#   :376  `^##\s+`                              -- is this line a HEADING at all?
#   :377  `^##\s+Rules of this ledger\s*$`      -- if so, is it the boilerplate section to SKIP?
#
# EACH WRITER ISOLATES ONE LINE, measured, not arranged by hope:
#   _write_ledger_rules pads AFTER the space, so `^##\s+` matches on that space in BOTH runtimes
#   whatever the pad is -- :376's class cannot move it, and only :377's can.
#   _write_ledger_head pads BETWEEN `##` and the space, so it decides whether :376 fires at all;
#   its heading text is not the rules heading, so :377 fails in both runtimes either way.
# Reverting either production line alone therefore reds exactly one of the two loops.
#
# BOTH OBSERVABLES ARE `evidence: MISSING` vs `present`, arranged so the padded line carries the
# unit's SOLE strong evidence. Without that the pad still lands but the verdict does not move:
# the first draft of both fixtures put a second backed unit in the ledger and every row would
# have compared exit-0 to exit-0. Measured before writing either one.
#
# THE CONTROL EXITS 1, AND THAT IS CORRECT rather than tolerated. With an empty pad the line IS a
# heading (or IS the rules heading), so the evidence is skipped by construction and the ledger is
# INCOMPLETE. `_control` does not require exit 0 -- it requires both runtimes to AGREE, the status
# counter to have been reached (`verified: 1`, which this state does print; measured, because a
# grep written as `verified: [0-9]+` misses the real four-space output and would have condemned a
# healthy control), and the `X` pad to MOVE the verdict, which it does: MISSING -> present.
#
# BUT `verified: 1` DOES LESS WORK HERE than on the exit-0 controls, and the difference is worth
# naming because the next reader will otherwise re-litigate the exit code. There it means "the
# parse reached the counter AND the ledger closed"; here only the former, since the ledger is
# failing on evidence by design. So this control alone cannot separate "evidence deliberately
# skipped" from "evidence broken for an unrelated reason". THE `X`-PAD CHECK IS WHAT CLOSES THAT:
# it proves the fixture CAN reach `evidence: present`, so MISSING is a property of the pad rather
# than a permanent state of the fixture. That pairing is what makes a failing baseline acceptable.
# shellcheck disable=SC2329  # invoked indirectly, as "$writer" from _case and _control
_write_ledger_rules() {
  local dest="$1" ws="$2" pad
  pad="$(printf '%b' "$ws")"
  {
    printf '# Delegation plan\n'
    printf 'Units: 1\n\n'
    printf '| # | Unit | Files (mine) | Worker | Acceptance | Status |\n'
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | verified |\n'
    printf '\n## Evidence\n\n'
    printf '**Unit 1 —** see the note below\n'
    printf '## %sRules of this ledger\n' "$pad"
    printf 'the runner ended with exit 3\n'
  } > "$dest"
}

# NO BACKTICKS AND NO DOT on the evidence line below, same invariant the exit-code writer states:
# `EXIT_CODE` must be the SOLE route to strong evidence, or a case passes on a sibling predicate
# and asserts nothing about the surface it names.
#
# THE `## Evidence` LINE ABOVE THE PADDED ONE IS LOAD-BEARING, AND NO CONTROL CAN SEE IT.
# `in_rules_section` is STICKY STATE across lines: when `^##\s+` does NOT match, the `if` body
# never runs, so the flag KEEPS ITS PREVIOUS VALUE. This surface works because that previous
# value is False -- set by `## Evidence`, which is a heading in both runtimes and is not the
# rules heading. Make the preceding heading the RULES heading instead and the flag is True on
# entry, the non-matching runtime skips the reset, and the padded line is dropped as boilerplate
# in BOTH -- collapsing the divergence into six vacuous OKs.
# The control cannot catch that: it runs with an EMPTY pad, where `^##\s+` matches in both
# runtimes and the stateful path is never exercised at all. So this invariant is held by comment
# and by nothing else. Reorder or drop `## Evidence` and this surface silently stops testing.
# shellcheck disable=SC2329  # invoked indirectly, as "$writer" from _case and _control
_write_ledger_head() {
  local dest="$1" ws="$2" pad
  pad="$(printf '%b' "$ws")"
  {
    printf '# Delegation plan\n'
    printf 'Units: 1\n\n'
    printf '| # | Unit | Files (mine) | Worker | Acceptance | Status |\n'
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | verified |\n'
    printf '\n## Evidence\n\n'
    printf '**Unit 1 —** see the note below\n'
    printf '##%s Notes on exit 3\n' "$pad"
  } > "$dest"
}

# Runs one runtime against its OWN COPY. Separate copies are load-bearing, not hygiene: the
# checker APPENDS a receipt to the ledger it reads, so a shared file would hand the second
# runtime a document the first had already modified, and the comparison would be of two
# different inputs.
_verdict() {
  local runtime="$1" src="$2" mine out rc
  mine="$WORK/$runtime-$(basename "$src")"
  cp "$src" "$mine"
  if [ "$runtime" = node ]; then
    out="$("$NODE_ABS" "$ORACLE" "$mine" 2>&1)"; rc=$?
  else
    out="$("$PY_ABS" "$PORT" "$mine" 2>&1)"; rc=$?
  fi
  # The ledger's own path appears in both outputs and differs by runtime prefix, so it is
  # stripped before comparing -- otherwise every case would "diverge" on the filename alone.
  printf 'exit=%s\n%s\n' "$rc" "${out//$mine/<LEDGER>}"
}

# `grep -qF -- "$marker"`, and the `--` is not defensive style. Without it a marker that STARTS
# WITH `-` is parsed by grep as OPTIONS rather than a pattern, grep exits non-zero having matched
# nothing, and `_case` reports `oracle moved, but not via ...` on output that contains the marker
# verbatim -- sending the maintainer to the checker's report text when the bug is in this line.
# Measured: `-> ledger complete` did exactly that on all five `differ` rows of both new surfaces.
# It failed LOUD, but only because those rows assert must-CONTAIN; a `same` row's must-NOT-contain
# would have been satisfied by the same broken grep in total silence.
#
# $5 is OPTIONAL: a string the ORACLE's padded output must contain when $4 is `differ`, and must
# NOT contain when $4 is `same`. Absent (the three original surfaces) it is skipped, so their four
# -argument calls are unchanged.
#
# WHY IT EXISTS, and it is specific to the header-finder surface. Everywhere else the oracle moves
# between exit 0 and exit 1, so "the verdict moved" has one meaning. The finder moves 0 -> exit 2,
# and exit 2 is ALSO what `fail(2, "cannot read ...")` emits. `_verdict` captures the message text
# as well as the code, so a fixture that became unreadable would still DIFFER from its baseline and
# satisfy the effect check -- six green rows asserting nothing about the header finder. The marker
# `no unit table header` is emitted from exactly one branch, so requiring it converts "something
# moved" into "the finder rejected this line".
_case() {
  local name="$1" ws="$2" writer="$3" effect="$4" marker="${5:-}" led o p base
  led="$WORK/$(printf '%s' "$name" | tr -c 'A-Za-z0-9' '-').md"
  "$writer" "$led" "$ws"
  o="$(_verdict node "$led")"
  # The baseline must EXIST before it is compared against. An unset key here is not a divergence,
  # it is a missing control -- and comparing against an empty string would pass every `differ`
  # expectation trivially (any real verdict differs from "") while failing every `same` one, which
  # is a fresh vacuity of exactly the kind the effect check was added to close.
  base="${BASE[$writer]-}"
  if [ -z "$base" ]; then
    printf 'DIVERGE  %-38s no baseline recorded for %s; its control never ran\n' "$name" "$writer"
    exit 1
  fi
  # `exit 1`, not a counted failure: this asserts the FIXTURE puts the pad somewhere the oracle's
  # whitespace class governs, which is a statement about this file, not about the port. Letting it
  # land in the `fail` counter would print it in the same shape as a genuine port divergence and
  # send the maintainer to the wrong source file.
  if [ "$effect" = differ ] && [ "$o" = "$base" ]; then
    printf 'DIVERGE  %-38s pad did not move the oracle; this vector is vacuous\n' "$name"
    printf '    unpadded: %s\n' "$(printf '%s' "$base" | tr '\n' '|')"
    printf '    padded  : %s\n' "$(printf '%s' "$o" | tr '\n' '|')"
    exit 1
  fi
  if [ "$effect" = same ] && [ "$o" != "$base" ]; then
    printf 'DIVERGE  %-38s oracle trims this pad; its verdict must not move\n' "$name"
    printf '    unpadded: %s\n' "$(printf '%s' "$base" | tr '\n' '|')"
    printf '    padded  : %s\n' "$(printf '%s' "$o" | tr '\n' '|')"
    exit 1
  fi
  # ATTRIBUTION. Without this the effect check above proves only that SOMETHING moved.
  # shellcheck disable=SC2016  # the backticks below are literal report text, not command substitution
  if [ -n "$marker" ]; then
    if [ "$effect" = differ ] && ! printf '%s' "$o" | grep -qF -- "$marker"; then
      printf 'DIVERGE  %-38s oracle moved, but not via `%s`; not attributable\n' "$name" "$marker"
      printf '    padded  : %s\n' "$(printf '%s' "$o" | tr '\n' '|')"
      exit 1
    fi
    if [ "$effect" = same ] && printf '%s' "$o" | grep -qF -- "$marker"; then
      printf 'DIVERGE  %-38s oracle must NOT reject this pad, but emitted `%s`\n' "$name" "$marker"
      printf '    padded  : %s\n' "$(printf '%s' "$o" | tr '\n' '|')"
      exit 1
    fi
  fi
  p="$(_verdict py "$led")"
  if [ "$o" = "$p" ]; then
    printf '  OK      %-38s identical verdict\n' "$name"
    pass=$((pass + 1))
  else
    printf 'DIVERGE  %-38s\n' "$name"
    printf '    oracle: %s\n' "$(printf '%s' "$o" | tr '\n' '|')"
    printf '    port  : %s\n' "$(printf '%s' "$p" | tr '\n' '|')"
    fail=$((fail + 1)); FAILED+=("$name")
  fi
}

# POSITIVE CONTROL, and it runs FIRST because everything below is meaningless without it. A plain
# unwrapped `verified` must produce the SAME verdict in both runtimes. If this row diverges, the
# harness itself is broken -- wrong path, receipt bleed, a stray env var -- and the six rows after
# it would "detect" a trim divergence that is really a defect in this file. A suite whose failures
# cannot be distinguished from its own breakage reports nothing.
#
# ONE CONTROL PER WRITER, and that is the whole reason this is a function rather than the single
# inline block it started as. A control vouches for the writer it runs and for no other: if
# `_write_ledger_hdr` emitted a ledger that does not parse, its six cases would agree
# identically-broken across both runtimes and every one would report OK -- a vacuous pass, and
# from the output indistinguishable from real coverage. The cell control cannot see that, because
# it never runs that writer.
#
# WHAT IT DOES **NOT** CATCH, measured rather than assumed, because the first version of this
# comment named an example that turns out to be wrong. Deleting the separator row from
# `_write_ledger_hdr` was offered as the motivating defect; run it and the oracle still exits 0
# with `verified: 1`. The parser SKIPS the separator row (`cells.every(c => c === "" ||
# /^:?-+:?$/.test(c))`), so its absence changes no verdict and no control can detect it. The two
# writers do emit BYTE-IDENTICAL files at `ws=''` -- measured with `cmp`, not inferred. But that
# buys nothing here, and the first version of this sentence claimed it did ("a defect in the
# writer's static text still diverges them"). It cannot. A static-text defect lives in the SHARED
# writer, so both runtimes read the same bytes and agree BY CONSTRUCTION -- the node-vs-python half
# can never fire on one. Only the `verified: 1` half can, and only when the defect moves the count
# off 1. A changed unit name, a reordered column whose header still parses, a different worker
# string: each leaves one verified row and passes here in silence.
#
# The honest scope: this catches a `_write_ledger_hdr` defect that changes the COUNT the parse
# yields. It CANNOT catch a defect in where the writer PLACES `$pad` -- the one line that makes it
# a different writer from `_write_ledger` -- because the control is by construction the only case
# that never supplies a pad. A pad written onto the separator row instead of the header would pass
# HERE. Whether the six header cases would then pass too is NOT established, and this session's own
# U+FEFF measurement argues they might not: the two runtimes diverge in OPPOSITE directions on the
# header surface, so a misplaced pad is at least as likely to be loud as silent.

# The pad every control uses to prove `%s` landed. `\x58` is the letter `X` -- whitespace in
# NEITHER language, in every version of both -- so it must survive into the ledger as an ordinary
# character and shift that writer's verdict. Deliberately not U+180E, the other obvious "matches
# neither" candidate: it left JS's `\s` in ES2016 and Python's on a Unicode table update, so it
# encodes a version bet where `X` encodes none. Written as a BYTE ESCAPE rather than a literal `X`
# on purpose -- that is what routes it through `%b`, so a control also fails when `%b` expansion
# is what broke.
NON_WS_PAD='\x58'

_control() {
  local writer="$1" label="$2" anchor="$3" led o p padded po added changed nchanged
  led="$WORK/control-$writer.md"
  "$writer" "$led" ''
  o="$(_verdict node "$led")"
  p="$(_verdict py "$led")"
  if [ "$o" != "$p" ]; then
    printf 'DIVERGE  %-38s harness is broken; the cases below cannot be trusted\n' "$label"
    printf '    oracle: %s\n' "$(printf '%s' "$o" | tr '\n' '|')"
    printf '    port  : %s\n' "$(printf '%s' "$p" | tr '\n' '|')"
    exit 1
  fi
  # AND the control must actually have reached the status parser. Both runtimes agreeing on exit 1
  # because the file was unreadable would satisfy the check above while proving nothing, so the
  # agreed verdict is asserted to be the one a recognized `verified` produces.
  # `verified: +1([^0-9]|$)`, not `verified: *1`: the loose form also matches `verified: 11` and
  # `verified: 1234`, because nothing terminates the number. A one-row fixture cannot produce
  # those today -- but a control's entire job is to still hold when the fixture changes under it,
  # and an assertion that widens silently as rows are added is the one that must not.
  if ! printf '%s' "$o" | grep -qE 'verified: +1([^0-9]|$)'; then
    printf 'DIVERGE  %-38s control never reached the status counter\n' "$label"
    printf '    oracle: %s\n' "$(printf '%s' "$o" | tr '\n' '|')"
    exit 1
  fi
  # The unpadded ORACLE verdict is this writer's baseline, and every one of its cases compares
  # against it (see CASE_ORACLE_EFFECT). Recorded here rather than recomputed per case because it
  # is the same six times, and because a case that had to build its own baseline could silently
  # build a different one.
  BASE["$writer"]="$o"
  # AND the pad must LAND on this writer's surface. Everything above runs with an EMPTY pad, so
  # without this the control is blind to the one line that makes each writer different from the
  # others -- where it puts `%s`. A dropped `%s`, a stray `%` swallowing it, or a `%b` that failed
  # to expand leaves every case reading the UNPADDED ledger; both runtimes then read identical
  # bytes and agree BY CONSTRUCTION, so the cases all print OK and report coverage that does not
  # exist. Six vacuous OKs is the exact defect this file has now shipped twice.
  #
  # The assertion is INEQUALITY AGAINST THE EMPTY-PAD VERDICT, deliberately not "some expected
  # string appeared". The first version of this check grepped for the abandoned-row reason marker,
  # and that was a PROXY: it asserted something about the checker's prose and inferred something
  # about the writer's format string, with the whole evidence pipeline in between. Anything else
  # that stopped unit 2's block attributing would have satisfied it with the pad deleted -- and
  # the stripper feeding that marker has a known-open fail-open defect, so the proxy was scheduled
  # to come apart. Worse, it would then have failed with a message blaming the writer, sending the
  # maintainer to the wrong file. Comparing verdicts asserts the property itself: the pad changed
  # what the checker saw. It needs no knowledge of WHICH string changed, so it survives a reword.
  #
  # Same runtime both sides (node vs node), never node vs python: this asks whether the pad had an
  # effect, which is a question about the FIXTURE. Whether the two runtimes agree about it is the
  # cases' job, and conflating the two would let a genuine port divergence read as a pad failure.
  padded="$WORK/padded-$writer.md"
  "$writer" "$padded" "$NON_WS_PAD"
  po="$(_verdict node "$padded")"
  if [ "$po" = "$o" ]; then
    printf 'DIVERGE  %-38s pad had no effect on the verdict; its cases are vacuous\n' "$label"
    printf '    unpadded: %s\n' "$(printf '%s' "$o" | tr '\n' '|')"
    printf '    padded  : %s\n' "$(printf '%s' "$po" | tr '\n' '|')"
    exit 1
  fi
  # AND the pad must land on the SURFACE this writer exists to exercise -- inequality alone
  # proves only that the pad had SOME effect, which is a weaker claim than the OK line used to
  # make. MEASURED, and this is why the anchor exists: move `_write_ledger_hdr`'s `%s` from the
  # end of the header-line format to the FRONT and the whole suite still exits 0. The control
  # passes (unpadded exits 0, `X|` at the front exits 2 -- an effect, just the wrong one), while
  # all six header vectors compare exit-2-to-exit-2, because `^\|` fails on the first byte before
  # any whitespace class is consulted and the trim semantics never run. Six vacuous OKs, green
  # control. "Difference is placement" is the same error as "agreement is coverage".
  #
  # `diff`'s `>` lines are the PADDED file's, so the anchor asks: did the byte land on a line
  # that still looks like the surface named? A pad in front of `|` leaves `X| # |`, which no
  # longer matches `^\| # \|`, and the check fires.
  # `diff` is captured, NOT piped: this file runs under `set -o pipefail`, and `diff` exits 1
  # whenever the files differ -- which is ALWAYS here, that being the point. Piped straight into
  # `grep -q`, the pipeline therefore reports 1 even on a match, and `if !` fires on every writer.
  # Measured: the anchor was correct and the check still failed for all three.
  # `-a`: `diff` goes BINARY-MODE on control characters and then prints "Binary files differ"
  # instead of any `>` line at all -- and control characters are this suite's entire subject. Today
  # only the `X` pad reaches here, so binary mode never fires; that is an invariant nobody wrote
  # down, and it stops holding the moment this check is pointed at a different pad. stderr is
  # captured with it so a `diff` that fails for some third reason cannot vanish into the void and
  # leave "no `>` lines" looking like a placement failure.
  #
  # EXACTLY ONE changed line, and that is stronger than the regex it accompanies. `\*\*Unit` is
  # AMBIGUOUS inside `_write_ledger_ev`, which emits two lines starting `**Unit` -- so the regex
  # alone cannot tell which of them `diff` reported, and a pad that moved from unit 2's header to
  # unit 1's would satisfy it. Requiring a single changed line removes the ambiguity without
  # needing a sharper regex per writer.
  added="$(diff -a "$led" "$padded" 2>&1 || true)"
  changed="$(printf '%s' "$added" | grep -a '^>' || true)"
  nchanged="$(printf '%s' "$changed" | grep -ac '^>' || true)"
  if [ "$nchanged" != 1 ] || ! printf '%s' "$changed" | grep -qE "^> ${anchor}"; then
    printf 'DIVERGE  %-38s pad landed outside this writer surface\n' "$label"
    printf '    anchor  : %s\n' "$anchor"
    printf '    changed : %s line(s): %s\n' "$nchanged" "$(printf '%s' "$changed" | tr '\n' '|')"
    exit 1
  fi
  printf '  OK      %-38s counts it verified; pad lands on its surface and moves the verdict\n' "$label"
  pass=$((pass + 1))
}

# The third argument is the ANCHOR: a regex the padded file's CHANGED line must still match, so
# the pad is pinned to the surface the writer names rather than merely having some effect
# somewhere. Each is the line-initial shape that survives a correctly-placed pad and is destroyed
# by a pad moved in front of it.
_control _write_ledger     "control: cell writer"   '\| 1 \|'
_control _write_ledger_hdr "control: header writer" '\| # \|'

for i in "${!CASE_NAME[@]}"; do
  _case "cell ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger "${CASE_ORACLE_EFFECT[$i]}"
done

# The same six code points on the header line. Kept as a separate loop rather than folded into the
# one above so a failure names which SURFACE diverged -- a header divergence and a cell divergence
# have different blast radii and would otherwise be indistinguishable in the output.
for i in "${!CASE_NAME[@]}"; do
  _case "header ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger_hdr "${CASE_ORACLE_EFFECT[$i]}"
done

_control _write_ledger_ev "control: evidence-header writer" '\*\*Unit'

# The same six code points inside the `**Unit N —**` evidence header. This surface is parsed by
# UNIT_HEADER, not by the table parser, so the twelve cases above cannot reach it -- and it is the
# one that was actually broken: the port transliterated the oracle's `\s+` literally, and `\s`
# denotes different sets in the two languages. Measured on the expression itself before the fix:
#   U+FEFF        js \s+ matches, python \s+ does NOT
#   U+001C/U+0085 python \s+ matches, js \s+ does NOT
# so the block opened in one runtime and not the other, and the abandoned-row reason marker went
# SILENT in the oracle while FIRING in the port on identical bytes. Fixed by building the class
# from _JS_TRIM_CODEPOINTS; these six rows are what stops it being transliterated back.
for i in "${!CASE_NAME[@]}"; do
  _case "evidence-header ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger_ev "${CASE_ORACLE_EFFECT[$i]}"
done

# The anchor is NOT `\| # \|` here, and the difference is forced. The other three writers pad AWAY
# from the token their anchor names, so that token survives the pad; this one pads THROUGH it -- a
# correctly-placed pad destroys `| # |` by construction, that being the whole surface. Anchoring on
# it would fail in BOTH the red and green states, reporting a working fix as a broken test.
#
# So the anchor asserts the pad's POSITION instead of a surviving token: `[^ |]` immediately before
# the `#`, inside the first cell. That is exactly the placement this writer exists to produce, and
# it rejects both ways of getting it wrong -- a pad moved in front of the `|` fails the line-initial
# `\|` (the shared rule above), and a pad moved to the row's TRAILING position leaves `| # |` with
# nothing before the `#` and fails here. A token-further-along anchor like `\| Unit \| Files` would
# have accepted the trailing-pad case, i.e. accepted `_write_ledger_hdr`'s surface as this one's.
# It is also matched line-initially (`^> ${anchor}`), so a mid-line token cannot match at all.
_control _write_ledger_find "control: header-finder writer" '\| [^|]*[^ |]# \|'

# FOURTH SURFACE: the table-header FINDER (`^\|\s*#\s*\|`, oracle `:47` / port TABLE_HEADER). It
# does not decide how a row parses -- it decides whether the file IS A LEDGER AT ALL, so it is the
# most consequential site of this whole class.
#
# THE THREE SURFACES ABOVE CANNOT REACH IT, and that is measured rather than argued. The header
# writer pads the row's TRAILING position, which feeds `column_count`; a `^`-anchored finder is
# untouchable from there. Reading that writer is only an argument, and an argument of exactly the
# shape this repo has had refuted twice by measuring -- so the real evidence is that applying the
# port fix changed NOTHING anywhere: all 14 diff suites, `ledger-tests.mjs` under both runtimes,
# and `npm test` stayed green. Nothing was watching this line.
#
# Measured before the fix, both runtimes, three controls:
#   U+001C/1D/1E/1F/0085  node exit 2 `not a DELEGATION.md ledger` / port exit 0 `ledger complete:
#                         every unit verified`  -- the port FAILED OPEN
#   U+FEFF                the reverse (node 0, port 2)
#   U+0020, U+0009, none  agree at 0 -- which is what proves the probe reached this line
#
# The pad goes AFTER the existing space (`| %s# |`), never instead of it, for the reason
# `_write_ledger_ev` already documents: replacing the space would make the CONTROL a different
# shape from the one the template ships, and a control that is not the unpadded case of the same
# fixture vouches for nothing.
#
# After the fix five of these six pass with BOTH runtimes at exit 2. That is correct -- the oracle
# is the spec -- but it means those five assert exactly one bit (does the finder reject?) and are
# blind to everything downstream. Do not read six green rows as coverage of the parse.
for i in "${!CASE_NAME[@]}"; do
  _case "header-finder ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger_find \
        "${CASE_ORACLE_EFFECT[$i]}" 'no unit table header'
done

# The anchor asserts the pad's POSITION, not a surviving token -- the same forced choice
# `_write_ledger_find` documents, and for the same reason. This writer pads THROUGH the span its
# surface is made of, so `exit 3` is destroyed by a CORRECTLY placed pad; anchoring on it fails in
# both the red and the green state and reports a working test as a broken one. (Written that way
# first, and the control caught it: `changed: > ... exit X3` against anchor `.*exit 3`.)
#
# `exit [^ 0-9]` says: a space after `exit`, then something that is neither a space nor a digit --
# i.e. the pad is exactly between the space and the `3`. It rejects both misplacements: a pad
# moved BEFORE `exit` leaves `exit 3` (a digit follows the space), and a pad moved to the line's
# TAIL leaves `exit 3X` (likewise). Only the intended placement matches.
#
# The trailing `[^ 0-9]*[0-9]` is not decoration: the bare `exit [^ 0-9]` above also accepts a
# writer that DROPS the digit entirely (`exit X`), because it pins the pad's position relative to
# `exit ` and says nothing about `3` surviving. The surface is `exit <ws><digit>`, so the anchor
# names both parts.
#
# THE FIRST `[^ 0-9]` IS LOAD-BEARING AND MUST NOT BE FOLDED INTO THE `*`. Written as
# `exit [^ 0-9]*[0-9]` -- which is how a review suggestion first landed here, applied without
# being traced -- the `*` matches ZERO characters and `exit 3` satisfies it: the anchor then
# accepts both misplacements it exists to reject, and stays green while doing so. Requiring one
# mandatory pad character AND a surviving digit is what rejects all three wrong shapes:
#   exit X3   -> [^ 0-9]=X, [^ 0-9]*=empty, [0-9]=3   MATCH   (the intended placement)
#   exit 3    -> [^ 0-9] vs `3`                       reject  (pad moved before `exit`)
#   exit 3X   -> [^ 0-9] vs `3`                       reject  (pad moved to the tail)
#   exit X    -> X, then no digit                     reject  (digit dropped)
_control _write_ledger_exit "control: exit-code writer" '\*\*Unit 1 .*exit [^ 0-9][^ 0-9]*[0-9]'

# THE SIXTH CODE POINT DIVERGES THE OTHER WAY HERE TOO, and the table at the top already predicts
# it: U+FEFF is JS whitespace, so `\s+` swallows the pad and the oracle still reads `exit<ws>3` --
# effect `same`. The other five are not JS whitespace, so `\s+` stops at the space and then needs
# a digit, finds the pad, and the predicate goes silent -- effect `differ`. That is the SAME
# same/differ split as the four surfaces above, which is why CASE_ORACLE_EFFECT is reused
# unchanged rather than given a per-surface copy that could drift out of step with the bytes.
for i in "${!CASE_NAME[@]}"; do
  _case "exit-code ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger_exit \
        "${CASE_ORACLE_EFFECT[$i]}"
done

# Same position-anchored form as the exit-code control, INCLUDING the mandatory first `[^ 0-9]`
# whose absence would silently re-admit both misplacements -- see that control's comment; the
# defect was copied to this surface before it was caught, so the two must stay the same shape.
# The pad DESTROYS the token it sits in front of, so anchoring on `Created: 2099` would fail in
# the red state and the green one alike.
#   Created: X2099  -> MATCH   (intended)      XCreated: 2099 -> reject (digit after the space)
#   Created: 2099X  -> reject                  Created: X     -> reject (date dropped)
_control _write_ledger_created "control: created writer" 'Created: [^ 0-9][^ 0-9]*[0-9]'

# The `differ` rows here move the oracle from STALE to clean rather than the other way round --
# see the writer's comment for why the polarity is inverted. `CASE_ORACLE_EFFECT` is unchanged
# because the same/differ split is a property of the codepoint under JS `\s`, not of the surface.
for i in "${!CASE_NAME[@]}"; do
  _case "created ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger_created \
        "${CASE_ORACLE_EFFECT[$i]}"
done

# Position-anchored, same forced choice the exit-code and created controls document: a correctly
# placed pad DESTROYS the token it sits in front of, so anchoring on the surviving token would
# fail in the red and green states alike. `Rules` is the token the pad precedes.
#   ## XRules...  -> MATCH (intended)        ## Rules...   -> reject (pad dropped or moved before)
#   ## Rules...X  -> reject (pad at tail)    X## Rules     -> reject (line-initial `\|`... rule)
_control _write_ledger_rules "control: rules-heading writer" '## [^ R][^ R]*Rules'

# The :377 half. The pad sits inside the `\s+` that must span from `##` to `Rules`, so it decides
# whether this line is recognised as the boilerplate section -- and therefore whether the evidence
# line BELOW it is skipped as template prose or attributed to unit 1.
#
# CASE_ORACLE_EFFECT applies UNCHANGED, and that was measured rather than assumed: against node's
# own empty-pad baseline (exit 1, evidence MISSING), U+FEFF leaves the rules regex matching -- js
# `\s+` swallows it -- so the verdict does not move (`same`); the other five stop the `\s+` short
# of `Rules`, the section is no longer recognised, the evidence line is attributed, and the oracle
# moves to exit 0 (`differ`). An earlier design that padded a DIFFERENT position needed an
# inverted array; this one does not, and the difference is the pad's placement, not the codepoints.
for i in "${!CASE_NAME[@]}"; do
  _case "rules-heading ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger_rules \
        "${CASE_ORACLE_EFFECT[$i]}" '-> ledger complete'
done

# The pad sits BETWEEN `##` and the space, so `[^ ]` immediately after `##` is exactly the intended
# placement and both misplacements leave a space there.
#   ##X Notes  -> MATCH (intended)           ## Notes   -> reject (pad dropped/moved before)
#   ## NotesX  -> reject (pad at tail)
_control _write_ledger_head "control: heading-finder writer" '##[^ ][^ ]* Notes'

# The :376 half -- whether the line is a HEADING AT ALL, which is the more consequential of the
# two: a heading is dropped from the evidence scan entirely, so this expression decides what can
# corroborate a row. The heading text here is deliberately NOT the rules heading, so :377 fails in
# both runtimes on every pad and cannot contribute; only :376's whitespace class can move this.
#
# The marker is what makes the rows attributable. Without it a fixture that became unreadable
# would exit 2, differ from the exit-1 baseline, and satisfy the effect check while proving
# nothing -- the same vacuity the header-finder surface added its marker to close.
#
# THE MARKER IS THE VERDICT LINE, NOT `evidence:    present`, AND THAT IS DELIBERATE. Both flip
# together (the evidence flip is WHY the verdict flips), so they are equally attributable -- but
# the `evidence:` value is COLUMN-ALIGNED, so its marker would encode the report's padding, and
# the two failure directions are asymmetric. On a `differ` row a stale marker fires loudly
# ("moved, but not via ..."). On the `same` row the assertion is must-NOT-contain, which a
# never-matching string satisfies VACUOUSLY AND FOREVER -- and that row is U+FEFF, the one
# carrying the opposition the whole design rests on. A field rename or a tab would have gutted it
# in silence. `-> ledger complete` has no alignment dependency, and `-> ledger INCOMPLETE.` does
# not contain it (grep -F is case-sensitive), so the same/differ split still discriminates.
for i in "${!CASE_NAME[@]}"; do
  _case "heading-finder ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger_head \
        "${CASE_ORACLE_EFFECT[$i]}" '-> ledger complete'
done

echo
if [ "$fail" = 0 ]; then
  # "check(s) passed", not "trim vector(s) identical": `$pass` has always also counted the
  # controls, and a control does not assert identity -- it asserts a count, and now also that the
  # padded verdict DIFFERS from the unpadded one. A summary line that calls every check an
  # identity is the kind of over-claim this suite exists to catch.
  echo "--- $pass check(s) passed (cell + header + evidence-header + header-finder + exit-code + created + rules-heading + heading-finder surfaces) ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
