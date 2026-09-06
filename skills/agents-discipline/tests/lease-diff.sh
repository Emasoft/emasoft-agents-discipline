#!/bin/bash
# Compare the two lease implementations over one stateful sequence of claims and releases.
#
# Usage: bash tests/lease-diff.sh   (run from skills/agents-discipline)
set -u
export PYTHONDONTWRITEBYTECODE=1
parent=$(mktemp -d)
node tests/lease-drive.mjs "$parent/js" >/tmp/lease-js.json 2>/tmp/lease-js.err; jrc=$?
python3 tests/lease_drive.py "$parent/py" >/tmp/lease-py.json 2>/tmp/lease-py.err; prc=$?
if [ $jrc != 0 ] || [ $prc != 0 ]; then
  echo "CRASH   lease sequence (js=$jrc py=$prc)"
  head -3 /tmp/lease-js.err; tail -3 /tmp/lease-py.err   # node message first, Python's last
  exit 1
fi
# NON-VACUITY: the sequence must actually claim, conflict and release. A port that refused
# every claim would produce two identical all-refused runs and pass a bare diff.
node -e '
const steps = JSON.parse(require("node:fs").readFileSync("/tmp/lease-js.json", "utf8"));
const ok = steps.filter((s) => s.outcome && s.outcome.ok === true).length;
const conflicted = steps.filter((s) => s.outcome && s.outcome.conflicts &&
  s.outcome.conflicts.length > 0).length;
const released = steps.filter((s) => typeof s.outcome === "number" && s.outcome > 0).length;
if (ok < 4 || conflicted < 8 || released < 2) {
  console.error(`VACUOUS: ok=${ok} conflicted=${conflicted} released=${released}`);
  process.exit(1);
}' || { echo "VACUOUS lease sequence"; exit 1; }
if diff -q /tmp/lease-js.json /tmp/lease-py.json >/dev/null; then
  echo "OK      lease sequence (20+ steps)"; echo "--- all identical ---"; exit 0
fi
echo "DIVERGE lease sequence"; diff /tmp/lease-js.json /tmp/lease-py.json | head -20
echo "--- DIVERGENCES ABOVE ---"; exit 1
