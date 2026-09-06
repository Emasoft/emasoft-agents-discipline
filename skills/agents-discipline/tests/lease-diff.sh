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
# NON-VACUITY: the sequence must actually claim, conflict and release.
#
# WHAT THIS GATE COVERS, and what it does NOT -- the earlier comment here had it BACKWARDS,
# and this is the sentence a future reader would trust. The counts are read from the ORACLE's
# output (/tmp/lease-js.json), so no change to gates.py can move them:
#   * a degraded PORT (claim_leases always refusing) leaves the counts untouched, so this gate
#     PASSES and the DIFF is what catches it. MEASURED: forcing the no-globs branch produced
#     `DIVERGE lease sequence`, never VACUOUS.
#   * a degraded SEQUENCE (a claim step deleted from BOTH drivers) makes the diff pass, since
#     both sides agree, and this gate is then the ONLY thing that fires.
# The two are complements; neither subsumes the other.
node -e '
const steps = JSON.parse(require("node:fs").readFileSync("/tmp/lease-js.json", "utf8"));
const ok = steps.filter((s) => s.outcome && s.outcome.ok === true).length;
const conflicted = steps.filter((s) => s.outcome && s.outcome.conflicts &&
  s.outcome.conflicts.length > 0).length;
const released = steps.filter((s) => typeof s.outcome === "number" && s.outcome > 0).length;
// EQUALITY, not a floor. These are the exact counts the 22-step sequence produces, so a `<`
// against the observed value is an equality wearing a floor'"'"'s clothes -- it admits no slack
// while implying it does. CHANGING THE SEQUENCE MUST UPDATE THESE NUMBERS DELIBERATELY.
// A floor set BELOW the real counts would silently tolerate steps degrading into no-ops,
// which is the failure this gate exists to catch.
if (ok !== 4 || conflicted !== 8 || released !== 2) {
  console.error(`VACUOUS: ok=${ok} conflicted=${conflicted} released=${released}`);
  process.exit(1);
}' || { echo "VACUOUS lease sequence"; exit 1; }
if diff -q /tmp/lease-js.json /tmp/lease-py.json >/dev/null; then
  echo "OK      lease sequence (22 steps)"; echo "--- all identical ---"; exit 0
fi
echo "DIVERGE lease sequence"; diff /tmp/lease-js.json /tmp/lease-py.json | head -20
echo "--- DIVERGENCES ABOVE ---"; exit 1
