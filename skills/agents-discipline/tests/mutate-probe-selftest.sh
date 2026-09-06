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
target=$(mktemp -d)/subject.py
printf 'MARKER = "original"\n' > "$target"
runner=$(mktemp)
printf '#!/bin/bash\necho "--- all identical ---"\n' > "$runner"; chmod +x "$runner"
slow=$(mktemp)
printf '#!/bin/bash\nsleep 5\necho "--- all identical ---"\n' > "$slow"; chmod +x "$slow"

# 1. A dropped runner argument must be an ERROR, not a negative result about untested code.
bash "$PROBE" t "$target" 'original' 'mutated' >/dev/null 2>&1
check "no runner exits 1" 1 $?

# 2. A non-unique anchor must fail rather than edit an unintended site.
printf 'A = "x"\nB = "x"\n' > "$target"
bash "$PROBE" t "$target" '"x"' '"y"' "$runner" >/dev/null 2>&1
check "non-unique anchor exits 1" 1 $?
printf 'MARKER = "original"\n' > "$target"

# 3. SIGNAL EXIT CODES: 128+signal. Both were hardcoded to 130 and nothing caught it.
#    SIGTERM only -- a backgrounded script from a non-interactive shell has SIGINT set to
#    SIG_IGN, and a signal ignored on entry CANNOT be re-trapped, so the INT path is not
#    testable here and remains argued rather than measured. Recorded, not papered over.
bash "$PROBE" t "$target" 'original' 'mutated' "$slow" >/dev/null 2>&1 &
probe=$!; sleep 1; kill -TERM $probe 2>/dev/null; wait $probe 2>/dev/null
check "SIGTERM exits 143 (128+15)" 143 $?

# 4. And the interrupt must leave the target RESTORED, which is the point of the handler.
check "target restored after signal" "$(printf 'MARKER = "original"\n')" "$(cat "$target")"

rm -rf "$(dirname "$target")" "$runner" "$slow"
echo "--- $( [ $fail = 0 ] && echo 'all identical' || echo 'SELF-TEST FAILURES ABOVE' ) ---"
exit $fail
