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
# WHICH FIXTURE A ROW USES IS LOAD-BEARING, and getting it wrong cost this file real coverage
# for one commit. The first version gave every path row BAD_ONE and concluded from their staying
# green under a neutered normalize_argv() that a printed value can never discriminate the fix --
# the stream handler substitutes U+FFFD on the way out, so one surrogate prints as node's one
# replacement either way. TRUE OF BAD_ONE ONLY. Under BAD_SEQ the port holds TWO surrogates
# where node has ONE character, so the handler emits SIX bytes against node's THREE. MEASURED on
# the gate-lint row with the fix neutered:
#     node  ... nope-x <3 bytes> y.md ...
#     port  ... nope-x <6 bytes> y.md ...
# So the honest rule is the opposite of what was written here: a printed row discriminates the
# fix whenever its fixture makes the two decoders disagree on the COUNT, and BAD_ONE is exactly
# the fixture where they cannot. Every path row therefore uses BAD_SEQ, which takes the fix from
# one covered CLI to three. This is the same "measured one shape, stated it universally" move the
# rest of this file exists to catch, committed inside the comment that was correcting it.
#
# THE TWO KINDS OF ROW ARE NOT EQUIVALENT EVIDENCE, though, and "all five redden" flattens them.
# Cases 1-3 discriminate the decode AT THE FILE, where nothing intervenes. Cases 4-5 discriminate
# it as expressed THROUGH the per-code-point stream handler -- so they are conditional on that
# handler, which is not a hypothetical dependency: 2c98ecc fixed it emitting one U+FFFD per ERROR
# instead of per CODE POINT, and under that spelling the port would print node's single
# replacement and these rows would go green under the same mutation. Still correct guards (a
# surrogate reaching the stream is a defect either way), just not independent of it.
#
# STILL NOT COVERED, and it is a property of the surface rather than an omission: gate-check.
# Its argv values are charset-closed ids and PATHS, and on macOS a path carrying invalid UTF-8
# cannot be created at all -- the filesystem answers "Illegal byte sequence" -- so `--cwd <bad
# byte>` reports identically with or without the fix. MEASURED. The call there is PROPHYLAXIS,
# said out loud because "the call is in all four" is the shape of claim this port has retracted
# twice. NOTE THE PLATFORM DEPENDENCE rather than inheriting it silently: ext4 and xfs accept
# arbitrary bytes in a filename, so on a Linux runner that vector IS constructible and the call
# stops being prophylactic. Unmeasured here; the claim above is macOS-scoped on purpose.
#
# AND `--reason` IS NOT THE ONLY FREE-TEXT ARGV VALUE -- the first version of this block said it
# was, which is dispatch.py:429's own false premise restated one commit after correcting it.
# `--handle` is guarded by str.isprintable() alone; case 3 covers it. The lesson that generalises:
# "charset-closed" is a claim about a SPECIFIC validator, so it has to be checked per option
# rather than inferred from the ones that happen to share a command line.
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
#
# THROUGH python3, NOT iconv, and the difference is the guard's failure mode. `printf | iconv
# >/dev/null 2>&1` inside an `if` reads a MISSING iconv (exit 127) as "the conversion failed",
# i.e. as the good answer -- so on a machine without it the guard passes and protects nothing,
# with the 2>&1 swallowing the message that would have shown it. That is verbatim the `grep -qP`
# defect removed from encoding-diff.sh's ASCII guard two commits ago, and it was reintroduced
# here. python3 is already a hard requirement of this suite ($PY_ABS), so this removes the
# dependency instead of guarding it, and the exit status distinguishes 0/1 from a crash.
#
# PASSING THE PROBE THROUGH python3's OWN argv IS SOUND, not circular: CPython surrogateescape-
# decodes it and `.encode("utf-8", "surrogateescape")` is that decode's exact inverse, so the
# bytes recovered are the ones the shell passed. The guard measures the FIXTURE, not CPython.
# PROVEN TO DISCRIMINATE, because a guard nobody has seen fail is not a guard: exit 1 (invalid,
# proceed) for both fixtures, exit 0 (valid, refuse) for a plain ASCII string and for a valid
# two-byte UTF-8 one.
for probe in "$BAD_ONE" "$BAD_SEQ"; do
  "$PY_ABS" -c 'import sys
raw = sys.argv[1].encode("utf-8", "surrogateescape")
try:
    raw.decode("utf-8")
except UnicodeDecodeError:
    sys.exit(1)
sys.exit(0)' "$probe"
  case "$?" in
    1) : ;;  # invalid, which is what this file needs
    0) echo "DIVERGE  fixture argv is valid UTF-8; this file's whole subject is absent" >&2
       exit 1 ;;
    *) echo "the UTF-8 validity guard could not run; refusing to proceed unchecked" >&2
       exit 2 ;;
  esac
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
      -e 's/"\(openedAt\|abandonedAt\|startedAt\|sealedAt\|completedAt\|at\)": "[^"]*"/"\1": "<TS>"/g' \
      -e 's/^[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}T[0-9]\{2\}:[0-9]\{2\}:[0-9]\{2\}\.[0-9]\{3\}Z /<TS> /'
}
# THE TIMESTAMP RULE SPELLS _iso_now()'s EXACT SHAPE rather than a loose class. The first version
# was `^[0-9-]\{10\}T[0-9:.]*Z `, which also matches `1234567890T::Z ` -- and "a scrub that
# over-matches" is precisely how the /private rule hid a real divergence class two commits ago.
# CHECKED AGAINST SYNTHETIC LINES -- a control on the REGEX, not on the system, and worth saying
# in those words because "MEASURED" elsewhere in this suite means "ran both runtimes and compared
# their output". It does not eat: a date-only line, the indented `"openedAt":` line (the `^`
# anchor stops it; the JSON rule above handles that one), or -- the case that matters -- a
# timestamp INSIDE an abandon reason, where only the leading one is replaced, so a reason that
# diverged in a timestamp-shaped substring would still show.
#
# TIGHTENING A SCRUB CUTS BOTH WAYS, but the two directions FAIL DIFFERENTLY and that is what
# makes this the safe one. A pattern that OVER-matches eats signal silently -- the /private
# failure. A pattern that UNDER-matches leaves clock noise in the comparison and the row goes
# RED. Moving from a loose class to an exact one trades a silent failure mode for a loud one.
#
# It cannot happen anyway, on the format contract rather than on the runs that passed: ECMA-262
# pins Date.prototype.toISOString to `YYYY-MM-DDTHH:mm:ss.sssZ` with the fraction zero-PADDED, so
# a whole second is `.000Z` and never elided, and JS Date is POSIX time with no leap second to
# make a 60th. The port's _iso_now() uses isoformat(timespec="milliseconds") and its docstring
# states the same contract. Confirmed anyway over 2000 samples each across a four-billion-second
# range: zero non-matching on either side.
#
# AND USER TEXT CANNOT REACH LINE START IN THESE FIXTURES, which is what makes the `^` anchor
# sufficient rather than lucky -- stated about the fixtures, not about status.log in general,
# since gate-check has its own status-append path that never runs against these roots. Each
# helper mkdir's a FRESH mktemp root per runtime per case, so the file holds exactly the lines
# its own two commands wrote; and neither can carry a newline, because dispatch.py's _CONTROL
# class covers U+0000-U+001F and _valid_reason refuses any reason matching it.

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
  # THE STATUS LOG IS A SECOND FILE THE SAME VALUE REACHES, through a DIFFERENT writer:
  # append_status(root, scope, iso + " " + event["text"]), where the event text interpolates the
  # reason. Compared because a per-writer divergence there would otherwise pass unseen -- the
  # first version of this helper captured stdout, stderr and dispatch.json only, on the strength
  # of a comment claiming the reason "reaches a FILE", singular. It reaches two.
  printf -- '--status--\n'
  _scrub "$root" < "$root/.agents-discipline/s/status.log" 2>/dev/null || echo "(no status log)"
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

# --- CASE 3: `--handle`, the OTHER free-text argv value, and an INPUT GATE the fix moved -----
# `--reason` is not the only one, and saying it was repeated the exact error this commit set out
# to correct: dispatch.py:429's exemption reasoned that every string in `state` is charset-closed,
# which is false for `reason` AND for `handle`. `--scope`/`--wave`/`--leaf` really are closed
# (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`); `handle` is guarded only by str.isprintable().
#
# AND IT IS A SECOND INSTANCE OF THE CRASH, not the gate change an earlier version of this
# comment claimed. That version reasoned: isprintable() is False for every surrogate, so pre-fix
# the port must have REFUSED a handle the oracle accepts. RUN INSTEAD OF REASONED, with
# normalize_argv neutered:
#     oracle  exit 0, STARTED w1 a (1/1 started)
#     port    exit 1, UnicodeEncodeError ... position 206-207 ... gates.py write_atomic
# The port did not refuse it -- it accepted it past the validator and died at the same line as
# the reason defect. The premise was false because THE CODE DOES NOT CALL isprintable(): the
# guard is `_CONTROL.search(handle)` at dispatch.py:97, and surrogates are outside that class.
# (isprintable() IS False for them, measured -- the reasoning was sound about a function nothing
# invokes.) That is dispatch.py:429's own error one level down: its exemption comment cites
# isprintable() as the handle gate too, so BOTH halves of its charset-closed premise are wrong,
# and the comment naming the wrong validator is why a second crash site sat next to the first.
# MEASURED after the fix, both runtimes: exit 0 and `"handle": "h<U+FFFD>nd"` in dispatch.json.
_handle_start() {  # exe script root -> "<exit>\n<stdout>\n--stderr--\n<stderr>\n--json--\n<file>"
  local exe="$1" script="$2" root="$3"
  mkdir -p "$root"
  "$exe" "$script" open --root "$root" --scope s --wave w1 --leaf a >/dev/null 2>&1
  "$exe" "$script" start --root "$root" --scope s --wave w1 --leaf a --handle "$BAD_SEQ" \
    > "$WORK/.o" 2> "$WORK/.e"
  printf 'exit=%s\n' "$?"
  _scrub "$root" < "$WORK/.o"
  printf -- '--stderr--\n'
  _scrub "$root" < "$WORK/.e"
  printf -- '--json--\n'
  _scrub "$root" < "$root/.agents-discipline/s/dispatch.json" 2>/dev/null || echo "(no state file)"
}
o_handle="$(_handle_start "$NODE_ABS" "$HERE/../scripts/dispatch-check.mjs" "$WORK/handle-o")"
p_handle="$(_handle_start "$PY_ABS" "$HERE/../scripts/dispatch_check.py" "$WORK/handle-p")"
# NON-VACUITY: the oracle must have ACCEPTED it. If a future guard closes this charset, both
# runtimes would refuse identically and the row would report agreement about a gate that no
# longer admits the input -- true, but no longer evidence about the decode.
if ! printf '%s' "$o_handle" | grep -q '^exit=0$' || ! printf '%s' "$o_handle" | grep -q '"handle"'; then
  printf 'DIVERGE  %-38s oracle no longer accepts this handle; the SPEC changed -- re-derive the fixture, do not weaken it\n' \
    "start handle, truncated seq"
  printf '%s\n' "$o_handle" | sed 's/^/    | /'
  fail=$((fail + 1)); FAILED+=("start handle, truncated seq")
elif [ "$o_handle" = "$p_handle" ]; then
  printf '  OK      %-38s accepted and recorded identically\n' "start handle, truncated seq"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "start handle, truncated seq"
  diff <(printf '%s\n' "$o_handle") <(printf '%s\n' "$p_handle")
  fail=$((fail + 1)); FAILED+=("start handle, truncated seq")
fi

# --- CASES 4 and 5: a bad byte in a PATH argument ------------------------------------------
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
  "$1" "$2" "$WORK/nope-$BAD_SEQ.md" > "$WORK/.o" 2> "$WORK/.e"
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

# --- CASE 5: the same message, through the OTHER path-taking CLI ---------------------------
# Two CLIs rather than one because the errno fix is per-call-site and the first one shipped
# without a sweep. See the block above cases 4-5 for what these rows do and do not prove.
_ledger_missing() {  # exe script
  "$1" "$2" "$WORK/nope-$BAD_SEQ.md" > "$WORK/.o" 2> "$WORK/.e"
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
