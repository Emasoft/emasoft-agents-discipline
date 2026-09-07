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

_case() {
  local name="$1" ws="$2" writer="$3" effect="$4" led o p base
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

echo
if [ "$fail" = 0 ]; then
  # "check(s) passed", not "trim vector(s) identical": `$pass` has always also counted the
  # controls, and a control does not assert identity -- it asserts a count, and now also that the
  # padded verdict DIFFERS from the unpadded one. A summary line that calls every check an
  # identity is the kind of over-claim this suite exists to catch.
  echo "--- $pass check(s) passed (cell + header + evidence-header surfaces) ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
