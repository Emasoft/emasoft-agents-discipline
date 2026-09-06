#!/bin/bash
# Assert mutate-probe.sh's own contract. REAL assertions: this exits non-zero on failure.
#
# WHY IT EXISTS. The signal-exit-code fix was "verified" by a one-off typed into a shell:
#     echo "observed=$rc  expected=143"; [ "$rc" = 143 ] && echo MATCH || echo MISMATCH
# which is a computed REPORT, not an assertion -- nothing branched on it, a MISMATCH would
# have printed and the work continued. And it lived in a transcript, not the repo, so nothing
# committed checked that INT->130 and TERM->143; the constants could regress silently. The
# earlier form was worse still (a static label that CONTRADICTED its own output and was read
# as success), but the class never changed: a check nobody gates on.
#
# Usage: bash tests/mutate-probe-selftest.sh   (run from skills/agents-discipline)
set -u
PROBE=tests/mutate-probe.sh
fail=0
check() {                      # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "  ok    $1 ($3)"
  else echo "  FAIL  $1: expected $2, got $3"; fail=1; fi
}

# A target this self-test owns, so a probe can mutate it without touching real source.
# The DIRECTORY is saved, not re-derived with `dirname` at cleanup time: reconstructing a path
# in order to `rm -rf` it means an empty or relative $target would resolve to "." and delete
# the working directory. Standing repo rule; cheap to obey.
tmpdir=$(mktemp -d)
target=$tmpdir/subject.py
printf 'MARKER = "original"\n' > "$target"
runner=$tmpdir/fast.sh
printf '#!/bin/bash\necho "--- all identical ---"\n' > "$runner"; chmod +x "$runner"
# FAST on the first call, SLOW on the second. mutate-probe.sh runs the runner TWICE -- the
# baseline, then the post-mutation run -- so a uniformly-slow runner puts any timed kill inside
# the BASELINE, before the anchor check and before the mutation. That is the wrong path: the
# handler exists for an interrupt during the MUTANT run, and a baseline interrupt "restores" an
# unmodified file over an unmodified file, which no broken restore can fail. Measured: with a
# uniformly-slow runner, breaking the restore entirely reddened NOTHING.
slow=$tmpdir/slow-second.sh
printf '#!/bin/bash\nn=$(cat %s/n 2>/dev/null || echo 0); echo $((n+1)) > %s/n\n[ "$n" = 0 ] || sleep 5\necho "--- all identical ---"\n' \
  "$tmpdir" "$tmpdir" > "$slow"; chmod +x "$slow"
trap 'rm -rf "$tmpdir"' EXIT INT TERM

# 1. A dropped runner argument must be an ERROR, not a negative result about untested code.
bash "$PROBE" t "$target" 'original' 'mutated' >/dev/null 2>&1
check "no runner exits 1" 1 $?

# 2. A non-unique anchor must fail rather than edit an unintended site.
printf 'A = "x"\nB = "x"\n' > "$target"
bash "$PROBE" t "$target" '"x"' '"y"' "$runner" >/dev/null 2>&1
check "non-unique anchor exits 1" 1 $?
printf 'MARKER = "original"\n' > "$target"

# RESET the call counter immediately before case 3. Nothing else uses $slow today, so the
# counter is already 0 here -- but case 3's correctness DEPENDS on that, silently, and this
# mechanism has already mis-targeted the kill once (a both-calls-slow runner put every
# interrupt in the baseline and left case 4 unable to fail). A future case that reuses $slow
# would push the kill back into the baseline with no assertion failing anywhere. One line.
rm -f "$tmpdir/n"

# 3. SIGNAL EXIT CODES: 128+signal. Both were hardcoded to 130 and nothing caught it.
#    SIGTERM only -- a backgrounded script from a non-interactive shell has SIGINT set to
#    SIG_IGN, and a signal ignored on entry CANNOT be re-trapped, so the INT path is not
#    testable here and remains argued rather than measured. Recorded, not papered over.
bash "$PROBE" t "$target" 'original' 'mutated' "$slow" >/dev/null 2>&1 &
probe=$!
sleep 2                       # past the fast baseline, inside the slow post-mutation run
# The kill's failure is REPORTED, not discarded. `kill ... 2>/dev/null` on an
# already-finished probe silently no-ops, and case 3 then asserts against the probe's natural
# exit -- a timing flake wearing the costume of a signal-handling regression.
if ! kill -TERM $probe 2>/dev/null; then
  echo "  FAIL  probe already exited before the kill (timing, not a handler defect)"; fail=1
fi
wait $probe 2>/dev/null
check "SIGTERM exits 143 (128+15)" 143 $?

# 4. The interrupt must leave the target RESTORED -- the whole point of the handler, and the
#    reason case 3 must land in the MUTANT run: only there is there something to restore.
check "target restored after signal" "$(printf 'MARKER = "original"\n')" "$(cat "$target")"
echo "--- $( [ $fail = 0 ] && echo 'all identical' || echo 'SELF-TEST FAILURES ABOVE' ) ---"
exit $fail
