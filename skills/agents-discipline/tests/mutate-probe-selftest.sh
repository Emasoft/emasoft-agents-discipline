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
# The DIVERGE line is printed UNCONDITIONALLY, on every call. A first version guarded it behind
# an `$FORCE_DIVERGE` that nothing ever sets, and defended that as "the predicate is 'this script
# CAN report divergence'". That defense is only valid if the predicate IS the property, and it
# is not: the predicate is a source grep, an approximation, and satisfying the approximation
# while leaving the property untouched is the exact "assertion satisfied by something other than
# the property it names" this project keeps catching. It was dead code added to pass a check,
# with a five-line justification standing three cases away from $noisy doing it correctly.
#
# Printing it every call costs nothing: the line appears in BOTH the baseline and the
# post-mutation run, so the set difference is empty and the verdict is unchanged.
printf '#!/bin/bash\necho "DIVERGE  fixture-constant"\necho "--- all identical ---"\n' \
  > "$runner"; chmod +x "$runner"
# FAST on the first call, SLOW on the second. mutate-probe.sh runs the runner TWICE -- the
# baseline, then the post-mutation run -- so a uniformly-slow runner puts any timed kill inside
# the BASELINE, before the anchor check and before the mutation. That is the wrong path: the
# handler exists for an interrupt during the MUTANT run, and a baseline interrupt "restores" an
# unmodified file over an unmodified file, which no broken restore can fail. Measured: with a
# uniformly-slow runner, breaking the restore entirely reddened NOTHING.
slow=$tmpdir/slow-second.sh
printf '#!/bin/bash\nn=$(cat %s/n 2>/dev/null || echo 0); echo $((n+1)) > %s/n\n[ "$n" = 0 ] || sleep 5\necho "DIVERGE  fixture-constant"\necho "--- all identical ---"\n' \
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

# 5. GUARD -1: a runner that cannot emit DIVERGE must be REFUSED, not scored. Without this the
#    verdict grep can only ever say NOTHING REDDENED, which is indistinguishable from "the
#    mutation has no coverage" -- the false negative that hid a two-row catch for a whole turn.
printf 'MARKER = "original"\n' > "$target"
mute=$tmpdir/no-vocab.sh
printf '#!/bin/bash\necho "--- all identical ---"\n' > "$mute"; chmod +x "$mute"
bash "$PROBE" t "$target" 'original' 'mutated' "$mute" >/dev/null 2>&1
check "runner with no DIVERGE emission exits 1" 1 $?
check "target untouched by a refused probe" "$(printf 'MARKER = "original"\n')" "$(cat "$target")"

# 6. THE VERDICT IS A SET DIFFERENCE, not a count. A runner carrying KNOWN divergences (green by
#    its own exit status, as regex-worker-diff.sh deliberately is) must not award them to a
#    mutation that did not cause them. This runner prints the SAME divergence before and after,
#    so the correct verdict is NOTHING REDDENED; the pre-fix code scored it as a catch.
noisy=$tmpdir/already-diverging.sh
printf '#!/bin/bash\necho "DIVERGE  pre-existing case"\necho "--- 1 KNOWN ---"\nexit 0\n' \
  > "$noisy"; chmod +x "$noisy"
out=$(bash "$PROBE" t "$target" 'original' 'mutated' "$noisy" 2>&1 | tail -1)
check "a baseline divergence is not awarded to the mutation" "t -> NOTHING REDDENED" "$out"

# 7. THE HEADLINE CHANGE, WHICH HAD NO CASE UNTIL NOW. Case 6 pins count -> delta: it passes
#    against BOTH the delta version (1-1=0) and the set difference. It cannot tell them apart.
#    This one can: the runner HEALS one known divergence and INTRODUCES a different one, so the
#    COUNT is unchanged (1 before, 1 after) and a delta reports NOTHING REDDENED while a row
#    genuinely reddened. Only comparing the LABELS catches it. That failure mode is not exotic
#    -- against regex-worker-diff.sh, whose seven divergences live in one function's flag and
#    anchor handling, a mutation shifting WHICH rows match is the likely shape.
printf 'MARKER = "original"\n' > "$target"
swap=$tmpdir/heals-and-breaks.sh
printf '#!/bin/bash\nn=$(cat %s/m 2>/dev/null || echo 0); echo $((n+1)) > %s/m\nif [ "$n" = 0 ]; then echo "DIVERGE  case-A"; else echo "DIVERGE  case-B"; fi\necho "--- 1 KNOWN ---"\nexit 0\n' \
  "$tmpdir" "$tmpdir" > "$swap"; chmod +x "$swap"
out=$(bash "$PROBE" t "$target" 'original' 'mutated' "$swap" 2>&1 | tail -1)
check "a compensating change is not netted to zero" \
  "t -> REDDENS (1 NEWLY diverging variant(s), 1 healed)" "$out"
echo "--- $( [ $fail = 0 ] && echo 'all identical' || echo 'SELF-TEST FAILURES ABOVE' ) ---"
exit $fail
