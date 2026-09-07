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
  local name="$1" ws="$2" writer="${3:-_write_ledger}" led o p
  led="$WORK/$(printf '%s' "$name" | tr -c 'A-Za-z0-9' '-').md"
  "$writer" "$led" "$ws"
  o="$(_verdict node "$led")"
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
_control() {
  local writer="$1" label="$2" led o p
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
  printf '  OK      %-38s both runtimes count it verified\n' "$label"
  pass=$((pass + 1))
}

_control _write_ledger     "control: cell writer"
_control _write_ledger_hdr "control: header writer"

for i in "${!CASE_NAME[@]}"; do
  _case "cell ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}"
done

# The same six code points on the header line. Kept as a separate loop rather than folded into the
# one above so a failure names which SURFACE diverged -- a header divergence and a cell divergence
# have different blast radii and would otherwise be indistinguishable in the output.
for i in "${!CASE_NAME[@]}"; do
  _case "header ${CASE_NAME[$i]}" "${CASE_BYTES[$i]}" _write_ledger_hdr
done

echo
if [ "$fail" = 0 ]; then
  echo "--- $pass trim vector(s) identical (cell + header surfaces) ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
