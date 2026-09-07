#!/usr/bin/env bash
# Differential for ARGV BYTES THAT ARE NOT VALID UTF-8 -- the decode the runtime performs before
# either program starts.
#
# WHY THIS IS NOT A ROW IN encoding-diff.sh: that file's subject is non-ASCII text the port
# WRITES or PRINTS, and its fixtures are VALID UTF-8 that both runtimes hold as the same string.
# This one is upstream of all of that. The bytes never form a character, the two runtimes build
# DIFFERENT STRINGS from them, and every downstream surface inherits the difference -- so the
# defect class, the fixtures and the fix all live at the boundary rather than at any write.
#
# THE DEFECT THAT MOTIVATED IT was a crash, not a spelling. `dispatch-check abandon --reason`
# with one bad byte: the oracle exited 0 and wrote U+FFFD, the port died with UnicodeEncodeError
# inside write_atomic -- MID-TRANSACTION, on state the other runtime reads. It survived because
# CPython surrogateescape-decodes argv with no explicit decode call anywhere, so nothing in the
# port's own source names the step that introduced it.
#
# THE MULTI-BYTE CASE IS THE ONE THAT MATTERS, and it is why case 2 exists next to case 1: node
# emits U+FFFD per MAXIMAL SUBPART while surrogateescape emits one surrogate PER BYTE, so a
# truncated three-byte sequence is ONE replacement to the oracle and TWO to a port that maps
# each surrogate to U+FFFD. Case 1 alone passes under that wrong fix. Case 2 does not.
#
# WHAT THIS FILE DOES NOT COVER, said here so four normalize_argv() call sites do not read as
# four covered surfaces. Only cases 1-2 discriminate the fix, and both drive ONE CLI. That is a
# property of the surfaces, not an omission:
#   * On a PRINTED value the fix is unobservable. The port's stream handler substitutes U+FFFD on
#     the way out, so a differently-decoded argument prints node's bytes either way. MEASURED:
#     with normalize_argv() neutered, cases 3-4 stay green.
#   * On macOS a PATH carrying invalid UTF-8 cannot be created at all -- the filesystem answers
#     "Illegal byte sequence" -- so the path-argument vector has nothing to open. MEASURED, and
#     it is also why `gate-check --cwd <bad byte>` reports identically in both runtimes with or
#     without the fix.
# So the one argv value that both carries free text AND reaches a FILE is dispatch-check's
# `--reason`, and that is what cases 1-2 are. On the other three CLIs the call is PROPHYLAXIS.
# Stated rather than implied, because "the call is in all four" is exactly the shape of claim
# this port has already had to retract twice.
#
# DIVERGE LINES START AT COLUMN 0 (see gate-args-diff.sh's note); every other verdict word keeps
# a 2-space indent so mutate-probe.sh's anchored `^DIVERGE` grep stays the only trigger.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
PY_ABS="$(command -v python3)"
NODE_ABS="$(command -v node)"

WORK="$(mktemp -d)"
[ -n "$WORK" ] && [ -d "$WORK" ] || { echo "mktemp -d failed; refusing to build fixtures" >&2; exit 2; }
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
declare -a FAILED=()

# Built from explicit byte escapes, never a literal: every non-ASCII value in this suite is, and
# the reason is recorded at length in encoding-diff.sh -- an escape typed into a file has been
# silently rendered back into a character four times in this project, twice producing a
# "divergence" whose two sides were identical.
#   BAD_ONE  a single invalid byte            -> 1 replacement in BOTH runtimes
#   BAD_SEQ  a TRUNCATED three-byte sequence  -> 1 replacement in node, 2 surrogates in CPython
BAD_ONE="$(printf 'bad\377reason')"
BAD_SEQ="$(printf 'bad\342\202mid\377reason')"

# ASSERTED TO CARRY THE BYTES, because every case below compares two runtimes against each
# other and would agree perfectly on a fixture that had degraded to ASCII. A whole file testing
# nothing, all rows green, is the failure this check exists to make impossible.
for probe in "$BAD_ONE" "$BAD_SEQ"; do
  if ! printf '%s' "$probe" | LC_ALL=C grep -q '[^ -~]'; then
    echo "DIVERGE  fixture argv is pure ASCII; every case below would be vacuous" >&2
    exit 1
  fi
done
# AND ASSERTED TO BE INVALID UTF-8. A valid non-ASCII fixture would still pass the check above
# while testing encoding-diff.sh's subject instead of this file's: both runtimes decode valid
# UTF-8 to the same string, so no boundary difference could arise for the cases to catch.
for probe in "$BAD_ONE" "$BAD_SEQ"; do
  if printf '%s' "$probe" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; then
    echo "DIVERGE  fixture argv is valid UTF-8; this file's whole subject is absent" >&2
    exit 1
  fi
done

# THIS FILE'S OWN SOURCE MUST BE PURE ASCII -- same rule, same reason as encoding-diff.sh.
# `tr -d`, NOT `grep -P`: macOS grep exits 2 on -P and `if grep -qP ...` reads that as "no
# match", so the guard would pass silently on any machine whose grep lacks it.
SELF="$HERE/argv-diff.sh"
[ -n "$SELF" ] && [ -r "$SELF" ] || { echo "cannot read own source for the ASCII guard" >&2; exit 2; }
if [ "$(tr -d '\000-\177' < "$SELF" | wc -c | tr -d ' ')" != "0" ]; then
  echo "DIVERGE  this file's source carries a non-ASCII byte; build fixtures from escapes" >&2
  exit 1
fi

# Timestamps differ by construction between two sequential runs, and the root differs by design
# (see the per-case note). Both are scrubbed so the comparison is about the DECODED TEXT.
# /private canonicalization first and symmetrically, for the reason encoding-diff.sh records:
# mapping both spellings to one token would hide a real lexical-vs-realpath divergence.
_scrub() {
  sed -e 's|/private/var/|/var/|g' -e "s|$1|<ROOT>|g" \
      -e 's/"\(openedAt\|abandonedAt\|startedAt\|sealedAt\|completedAt\|at\)": "[^"]*"/"\1": "<TS>"/g'
}

# --- CASES 1 and 2: an abandon reason, which reaches BOTH stdout and dispatch.json ----------
# Separate roots per runtime, and this one is not optional: dispatch.json written by the first
# run is READ by the second, so a shared root would compare the port against the oracle's own
# leftover state instead of against the oracle.
_abandon() {  # exe script root reason -> "<stdout>\n--stderr--\n<stderr>\n--json--\n<file>"
  local exe="$1" script="$2" root="$3" reason="$4"
  mkdir -p "$root"
  "$exe" "$script" open --root "$root" --scope s --wave w1 --leaf a >/dev/null 2>&1
  "$exe" "$script" abandon --root "$root" --scope s --wave w1 --reason "$reason" \
    > "$WORK/.o" 2> "$WORK/.e"
  local code=$?
  printf 'exit=%s\n' "$code"
  _scrub "$root" < "$WORK/.o"
  printf -- '--stderr--\n'
  _scrub "$root" < "$WORK/.e"
  printf -- '--json--\n'
  _scrub "$root" < "$root/.agents-discipline/s/dispatch.json" 2>/dev/null || echo "(no state file)"
}

_argv_case() {  # label reason
  local label="$1" reason="$2" tag; tag="$(printf '%s' "$label" | tr -c 'a-z0-9' _)"
  local o p
  o="$(_abandon "$NODE_ABS" "$HERE/../scripts/dispatch-check.mjs" "$WORK/$tag-o" "$reason")"
  p="$(_abandon "$PY_ABS" "$HERE/../scripts/dispatch_check.py" "$WORK/$tag-p" "$reason")"
  # NON-VACUITY: the oracle must have SUCCEEDED and actually recorded a reason. Without this a
  # fixture that failed for an unrelated reason (a bad root, a rejected wave id) leaves both
  # sides equal on an error message and reports agreement about a code path neither ran.
  if ! printf '%s' "$o" | grep -q '^exit=0$' || ! printf '%s' "$o" | grep -q '"reason"'; then
    printf 'DIVERGE  %-38s oracle wrote no reason; fixture reached nothing\n' "$label"
    printf '%s\n' "$o" | sed 's/^/    | /'
    fail=$((fail + 1)); FAILED+=("$label")
  elif [ "$o" = "$p" ]; then
    printf '  OK      %-38s decoded identically to stdout and state\n' "$label"
    pass=$((pass + 1))
  else
    printf 'DIVERGE  %-38s\n' "$label"
    diff <(printf '%s\n' "$o") <(printf '%s\n' "$p")
    fail=$((fail + 1)); FAILED+=("$label")
  fi
}

_argv_case "abandon reason, one bad byte" "$BAD_ONE"
_argv_case "abandon reason, truncated seq" "$BAD_SEQ"

# --- CASES 3 and 4: a bad byte in a PATH argument ------------------------------------------
# THESE TWO DO NOT GUARD THE ARGV FIX, and the first draft of this comment claimed they did.
# MEASURED by neutering normalize_argv(): cases 1-2 go red, these two stay GREEN. The reason is
# worth keeping, because it bounds what any print-only differential in this project can prove --
# the port's stream handler substitutes U+FFFD for a lone surrogate on the way OUT (_JS_SURROGATE
# _ERRORS in jsapi.py), so a path that decoded differently still PRINTS the same bytes as node's.
# The argv decode is therefore invisible on every surface that is only printed, and visible only
# where the value is WRITTEN TO A FILE or hashed -- which is exactly what cases 1-2 exercise and
# why they need the state file in their comparison.
#
# WHAT THEY DO GUARD is the errno MESSAGE SHAPE at the two path-taking CLIs, and that is not a
# leftover: case 4 went red on its first ever run against `ledger_check.py:96`, the same
# `err.message`-vs-`str()` divergence 9071a84 fixed in gate-lint, at a second call site no sweep
# had reached. Both rows are proven to discriminate it -- replacing either CLI's
# `_node_fs_message(...) if errno else str(...)` with a bare `str(...)` reddens that row alone.
_lint_missing() {  # exe script
  "$1" "$2" "$WORK/nope-$BAD_ONE.md" > "$WORK/.o" 2> "$WORK/.e"
  printf 'exit=%s\n' "$?"
  _scrub "$WORK" < "$WORK/.o"
  printf -- '--stderr--\n'
  _scrub "$WORK" < "$WORK/.e"
}
o_lint="$(_lint_missing "$NODE_ABS" "$HERE/../scripts/gate-lint.mjs")"
p_lint="$(_lint_missing "$PY_ABS" "$HERE/../scripts/gate_lint.py")"
if ! printf '%s' "$o_lint" | grep -q 'cannot read'; then
  printf 'DIVERGE  %-38s oracle printed no read failure; fixture reached nothing\n' \
    "gate-lint path argument"
  printf '%s\n' "$o_lint" | sed 's/^/    | /'
  fail=$((fail + 1)); FAILED+=("gate-lint path argument")
elif [ "$o_lint" = "$p_lint" ]; then
  printf '  OK      %-38s reported back identically\n' "gate-lint path argument"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "gate-lint path argument"
  diff <(printf '%s\n' "$o_lint") <(printf '%s\n' "$p_lint")
  fail=$((fail + 1)); FAILED+=("gate-lint path argument")
fi

# --- CASE 4: the same message, through the OTHER path-taking CLI ---------------------------
# Two CLIs rather than one because the errno fix is per-call-site and the first one shipped
# without a sweep. See the block above cases 3-4 for what these rows do and do not prove.
_ledger_missing() {  # exe script
  "$1" "$2" "$WORK/nope-$BAD_ONE.md" > "$WORK/.o" 2> "$WORK/.e"
  printf 'exit=%s\n' "$?"
  _scrub "$WORK" < "$WORK/.o"
  printf -- '--stderr--\n'
  _scrub "$WORK" < "$WORK/.e"
}
o_led="$(_ledger_missing "$NODE_ABS" "$HERE/../scripts/ledger-check.mjs")"
p_led="$(_ledger_missing "$PY_ABS" "$HERE/../scripts/ledger_check.py")"
if [ "$(printf '%s' "$o_led" | head -1)" = "exit=0" ]; then
  printf 'DIVERGE  %-38s oracle exited 0 on a missing ledger; fixture reached nothing\n' \
    "ledger-check path argument"
  printf '%s\n' "$o_led" | sed 's/^/    | /'
  fail=$((fail + 1)); FAILED+=("ledger-check path argument")
elif [ "$o_led" = "$p_led" ]; then
  printf '  OK      %-38s reported back identically\n' "ledger-check path argument"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "ledger-check path argument"
  diff <(printf '%s\n' "$o_led") <(printf '%s\n' "$p_led")
  fail=$((fail + 1)); FAILED+=("ledger-check path argument")
fi

echo
if [ "$fail" = 0 ]; then
  echo "--- $pass argv decode(s) identical ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
