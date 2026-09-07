#!/usr/bin/env bash
# Differential for the ERRNO MESSAGE SHAPE -- node's `error.message` for a failed fs call, which
# is not Python's str().
#
#     oracle  EACCES: permission denied, open '<path>'
#     port    [Errno 13] Permission denied: '<path>'
#
# WHY A RUNNER AND NOT ANOTHER ONE-OFF PROBE. This class has now been found FOUR separate times --
# gate-lint (9071a84), ledger_check.py:96, dispatch.py read_state, and gates.py's own scan site --
# each by hand, each months of commits apart, because no runner reddens when one regresses. The
# read_state instance is the sharpest: its comment DESCRIBED the divergence correctly and
# declared it out of scope, and nothing ever re-read that deferral. A row is the thing that
# re-reads a deferral for you.
#
# EVERY SUITE THAT ALREADY EXISTS PASSES BOTH BEFORE AND AFTER EACH OF THOSE FIXES, which is the
# evidence that none of them covers this: lint-tests for gate-lint, ledger-tests for ledger-check,
# dispatch-tests for read_state. The message is user-facing text on an error path, and error-path
# text is exactly what a behavioural suite does not assert.
#
# EACH ROW DRIVES A DIFFERENT CLI, because the fix is per-call-site and has three times been
# applied to one site while an identical sibling kept the defect.
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
# chmod 000 files are created below; make them removable again whatever the exit path.
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

pass=0; fail=0
declare -a FAILED=()

# EACCES, NOT ENOENT, for every row. ENOENT is the one shape where several wrong spellings happen
# to look right -- gate-lint's own fix was first measured on a missing file, and a missing file is
# also the shape most likely to be special-cased upstream into a friendlier message (read_state
# returns an empty state on ENOENT; gate-check prints "does not exist"). An unreadable file
# reaches the generic branch in every CLI, which is the branch this file is about.
#
# WHICH MAKES uid 0 A CORRECTNESS PROBLEM, not a portability nit: root bypasses the permission
# bits, so under a root container -- which is what many CI images are -- every chmod 000 file
# opens fine, no row reaches its errno branch, and all four fail their non-vacuity gate at once.
# That reads as four divergences when the truth is "this technique does not work here". Refused
# up front with its own exit code and its own sentence, so the next person sees the cause instead
# of debugging four phantom failures. It is not skipped: skipping would let the class regress
# unnoticed on exactly the machine that runs the suite most often, and this file exists because
# five instances of one defect each survived by being unwatched.
# PROBED BEHAVIOURALLY, not `id -u`. Root is the common reason chmod 000 stops denying, but it is
# not the only one -- an ACL, a permissive mount (many CI volume mounts), a filesystem without
# POSIX bits at all. A uid test answers a PROXY question; opening the file answers the real one,
# and costs the same. EXIT 2, not 1: "this check cannot run here" is a different verdict from
# "the runtimes differ", and the mktemp guard above already uses 2 for exactly that.
_probe="$WORK/.denial-probe"
printf x > "$_probe"; chmod 000 "$_probe"
if cat "$_probe" >/dev/null 2>&1; then
  echo "errno-message-diff: chmod 000 does not deny reads here (running as root?), so every row" >&2
  echo "  below would be vacuous rather than passing. Re-run as an unprivileged user." >&2
  chmod 644 "$_probe"; exit 2
fi
chmod 644 "$_probe"
_unreadable() {  # path -> creates it, unreadable
  printf '%s' "${2:-x}" > "$1"
  chmod 000 "$1"
}

# ROOT SCRUBBED BUT NOT CANONICALISED AWAY: /private/var and /var are the same directory under two
# spellings on macOS, and mapping both to one token would hide a lexical-vs-realpath divergence
# (encoding-diff.sh records the incident). Normalise the spelling, then substitute.
# The temp suffix is scrubbed for row 6 and harmless everywhere else: write_atomic names its
# temporary `<target>.<pid>.<16 hex>.tmp`, so the two runtimes CANNOT produce equal text on any
# write failure -- different pids, different random bytes. Without this, that row would fail for
# a reason unrelated to what it measures.
_scrub() {
  sed -e 's|/private/var/|/var/|g' -e "s|$1|<ROOT>|g" \
      -e 's/\.[0-9][0-9]*\.[0-9a-f][0-9a-f]*\.tmp/.<TMP>/g'
}

_row() {  # label  oracle-cmd-file  port-cmd-file  required-substring
  local label="$1" o p needle="$4"
  o="$(_scrub "$2" < "$WORK/.o")"; p="$(_scrub "$3" < "$WORK/.p")"
  # NON-VACUITY: the oracle must have produced the message this row is about. Without it a
  # fixture that failed earlier (a bad flag, a missing arg) leaves both sides equal on some other
  # error and reports agreement about a branch neither reached.
  if ! printf '%s' "$o" | grep -q "$needle"; then
    printf 'DIVERGE  %-34s oracle did not reach the errno branch; fixture reached nothing\n' "$label"
    printf '%s\n' "$o" | sed 's/^/    | /'
    fail=$((fail + 1)); FAILED+=("$label")
  elif [ "$o" = "$p" ]; then
    printf '  OK      %-34s node shape reproduced\n' "$label"
    pass=$((pass + 1))
  else
    printf 'DIVERGE  %-34s\n' "$label"
    diff <(printf '%s\n' "$o") <(printf '%s\n' "$p")
    fail=$((fail + 1)); FAILED+=("$label")
  fi
}

# --- ROW 1: dispatch-check, through read_state's wrap ----------------------------------------
# The message arrives wrapped ("invalid dispatch state: " + the node shape), which is itself part
# of what is compared: read_state re-raises with a prefix, and a port that fixed the inner text
# while dropping the prefix would look right in a hand probe and wrong here.
for rt in o p; do
  R="$WORK/disp-$rt"; mkdir -p "$R/.agents-discipline/s"
  _unreadable "$R/.agents-discipline/s/dispatch.json" '{}'
done
"$NODE_ABS" "$HERE/../scripts/dispatch-check.mjs" open --root "$WORK/disp-o" --scope s --wave w1 --leaf a \
  > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/dispatch_check.py" open --root "$WORK/disp-p" --scope s --wave w1 --leaf a \
  > "$WORK/.p" 2>&1
_row "dispatch-check unreadable state" "$WORK/disp-o" "$WORK/disp-p" "invalid dispatch state"

# --- ROW 2: gate-lint, the site 9071a84 fixed ------------------------------------------------
for rt in o p; do
  R="$WORK/lint-$rt"; mkdir -p "$R"
  _unreadable "$R/gates.md" '# Gates'
done
"$NODE_ABS" "$HERE/../scripts/gate-lint.mjs" "$WORK/lint-o/gates.md" > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/gate_lint.py" "$WORK/lint-p/gates.md" > "$WORK/.p" 2>&1
_row "gate-lint unreadable ledger" "$WORK/lint-o" "$WORK/lint-p" "cannot read"

# --- ROW 3: ledger-check, the sibling site no sweep had reached -------------------------------
for rt in o p; do
  R="$WORK/led-$rt"; mkdir -p "$R"
  _unreadable "$R/DELEGATION.md" '| # |'
done
"$NODE_ABS" "$HERE/../scripts/ledger-check.mjs" "$WORK/led-o/DELEGATION.md" > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/ledger_check.py" "$WORK/led-p/DELEGATION.md" > "$WORK/.p" 2>&1
_row "ledger-check unreadable ledger" "$WORK/led-o" "$WORK/led-p" "cannot read"

# --- ROW 4: gate-check, where the SWEEP'S REMAINING SITES LIVE --------------------------------
# Included before those sites are fixed, deliberately: this row is what tells the next pass
# whether gate-check's own `cannot read` already matches or is the next defect. If it is green
# today that is information, not a wasted row -- gate_check.py:478 has its own spelling and the
# only way to know which it is, is to compare.
for rt in o p; do
  R="$WORK/gc-$rt"; mkdir -p "$R/.agents-discipline/s/gates"
  _unreadable "$R/.agents-discipline/s/gates/leaf.md" '# Gates'
done
"$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/gc-o" --scope s --status > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/gc-p" --scope s --status > "$WORK/.p" 2>&1
# The needle names the BRANCH, like every other row. It was "." -- match any non-empty output --
# in the first draft, which disabled the non-vacuity gate on the one row that found a defect: a
# usage error, a bad flag or a missing scope would have satisfied it, and two runtimes agreeing on
# some OTHER error would have reported OK about a branch neither reached.
_row "gate-check unreadable gate file" "$WORK/gc-o" "$WORK/gc-p" "cannot inspect gate file"

# --- ROW 5: a SECOND errno code, because the four above only ever exercise EACCES -------------
# The rows above prove the message SHAPE. They cannot show a divergence in the PROSE, which
# node_fs_message produces with `os.strerror(n).lower()` against libuv's own table -- two
# independent strings that happen to agree for the codes measured, with no guarantee they agree
# for the next one. Its docstring already names ELOOP as a live counterexample on macOS.
#
# ELOOP CANNOT SERVE AS THAT ROW, measured: read_stable_regular_file catches ELOOP explicitly and
# converts it to its own "must be one unchanged regular single-link file" message, so it never
# reaches the helper at all -- both runtimes print the authored text, identically, and the
# counterexample the docstring warns about is UNREACHABLE from these call sites. Worth knowing:
# the helper's riskiest code is one no CLI can deliver to it.
# ENOTDIR does reach it, via a path whose parent is a regular file, and it is a different code
# with different prose ("not a directory") from a different strerror entry.
mkdir -p "$WORK/nd-o" "$WORK/nd-p"
printf 'x' > "$WORK/nd-o/afile"; printf 'x' > "$WORK/nd-p/afile"
"$NODE_ABS" "$HERE/../scripts/gate-lint.mjs" "$WORK/nd-o/afile/child.md" > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/gate_lint.py" "$WORK/nd-p/afile/child.md" > "$WORK/.p" 2>&1
_row "gate-lint ENOTDIR prose" "$WORK/nd-o" "$WORK/nd-p" "ENOTDIR"

# --- ROW 6: a WRITE, where every other row is a read ------------------------------------------
# The five rows above all fail at an OPEN FOR READING, so they cannot show whether the hardcoded
# `open` generalises past that. This one fails while WRITING -- an unwritable gates directory, so
# write_atomic cannot create its temp file. MEASURED: node still says `open`, because the failing
# call IS an open, of the temp rather than the target. That is why the row exists and also why it
# needs the temp scrub above.
for rt in o p; do
  R="$WORK/wr-$rt"; mkdir -p "$R/.agents-discipline/s/gates"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: echo ok\n  EXPECT: ok\n' \
    > "$R/.agents-discipline/s/gates/leaf.md"
  chmod 500 "$R/.agents-discipline/s/gates"
done
# STDERR ONLY, and NOT `2>&1`. The first draft merged them and the row failed on ORDERING: the
# same "cannot update" line appeared at position 11 in the oracle and position 1 in the port,
# because node and CPython flush the two streams in different orders. That is verbatim the defect
# encoding-diff.sh's own comment warns about -- "a merge diffs flush order rather than content" --
# repeated in a new file by the person who wrote the warning. This message goes to stderr in both
# runtimes (console.error / the port's error()), so stderr alone is the comparison, and the
# stdout report is not what this row is about.
#
# AND AN APPROVAL DIR INSIDE THE FIXTURE. `--approve` writes a token keyed by the RESOLVED ledger
# path, so two per-runtime roots produce two different digests -- a guaranteed diff that says
# nothing about either implementation. Pointing both at their own fixture dir also keeps the row
# from writing into the user's real ~/.agents-discipline/approved/, which a test must not do.
AGENTS_DISCIPLINE_APPROVAL_DIR="$WORK/approvals-o" \
  "$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/wr-o" --scope s --approve \
  > /dev/null 2> "$WORK/.o"
AGENTS_DISCIPLINE_APPROVAL_DIR="$WORK/approvals-p" \
  "$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/wr-p" --scope s --approve \
  > /dev/null 2> "$WORK/.p"
chmod 700 "$WORK/wr-o/.agents-discipline/s/gates" "$WORK/wr-p/.agents-discipline/s/gates"
_row "gate-check unwritable ledger dir" "$WORK/wr-o" "$WORK/wr-p" "cannot update"

echo
if [ "$fail" = 0 ]; then
  echo "--- $pass errno message(s) identical ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
