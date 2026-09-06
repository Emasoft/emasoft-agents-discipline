#!/bin/bash
# Run ONE mutation probe against a differential runner, and refuse to call a broken mutant a
# catch.
#
#   bash tests/mutate-probe.sh <label> <file> <old-python-literal> <new-python-literal> <runner...>
#
# WHY THIS EXISTS. A probe reported "reddens 9 variants" for a mutation that had produced an
# IndentationError: the anchor omitted the `for` line above it, the mutant could not be
# IMPORTED, the Python driver exited non-zero on every variant, and the probe's
# `grep -cE '^(DIVERGE|CRASH)'` counted each crash as a catch. The fix it claimed to cover had
# no coverage at all -- every row in that corpus was a string, so the behaviour was
# unobservable either way.
#
# Three guards, each for a failure this harness actually had:
#   1. the anchor must be UNIQUE          (an earlier probe edited a different function)
#   2. the edit must CHANGE the file      (a stale anchor silently no-ops)
#   3. the mutant must still IMPORT       (a syntax error is not a behavioural divergence)
# and only DIVERGE counts as reddening -- never CRASH.
set -u
label="$1"; target="$2"; old="$3"; new="$4"; shift 4
backup=$(mktemp)
cp "$target" "$backup"
restore() { cp "$backup" "$target"; }
trap restore EXIT INT TERM

if ! OLD="$old" NEW="$new" python3 -B -c '
import os, pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
n = s.count(old)
if n != 1:
    sys.exit("ANCHOR NOT UNIQUE (%d occurrences)" % n)
p.write_text(s.replace(old, new))
' "$target"; then
  echo "$label -> PROBE FAILED (anchor)"; exit 1
fi

if cmp -s "$target" "$backup"; then echo "$label -> PROBE FAILED (edit was a no-op)"; exit 1; fi

# Guard 3. Import the MUTATED module by its own directory, so a syntax or indentation error is
# reported as a broken probe rather than silently counted as nine catches.
if ! PYTHONDONTWRITEBYTECODE=1 python3 -c "
import sys; sys.path.insert(0, '$(dirname "$target")')
import $(basename "$target" .py)" 2>/tmp/mutate-import.err; then
  echo "$label -> PROBE FAILED (mutant does not import): $(tail -1 /tmp/mutate-import.err)"
  exit 1
fi

output=$("$@" 2>&1)
diverged=$(printf '%s\n' "$output" | grep -cE '^DIVERGE')
crashed=$(printf '%s\n' "$output" | grep -cE '^(CRASH|BUILD-FAILED|VACUOUS)')
if [ "$diverged" -gt 0 ]; then
  echo "$label -> REDDENS ($diverged diverging row(s)$([ "$crashed" -gt 0 ] && echo ", $crashed crash(es)"))"
elif [ "$crashed" -gt 0 ]; then
  # A crash is NOT a catch. It means the mutant broke the harness, so the probe proved nothing.
  echo "$label -> INCONCLUSIVE (only crashes, $crashed) -- the mutant broke the runner"
else
  echo "$label -> NOTHING REDDENED"
fi
