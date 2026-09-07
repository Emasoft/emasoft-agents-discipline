#!/bin/bash
# Compare the APPROVAL DIGEST across the two runtimes, per case.
#
# gate-check.mjs:358 keys every approval on sha256(JSON.stringify(oracle(file, gate))). One
# differing byte and every approval fails to match -- with a symptom indistinguishable from a
# missing --approve, since both print APPROVAL REQUIRED / NOT RUN and then run zero commands.
# This settles the serialization half before gate_check.py exists to be blamed instead.
#
# Usage: bash tests/digest-diff.sh   (run from skills/agents-discipline)
set -u
export PYTHONDONTWRITEBYTECODE=1
node tests/digest-drive.mjs > /tmp/digest-js.json 2>/tmp/digest-js.err; jrc=$?
python3 tests/digest_drive.py > /tmp/digest-py.json 2>/tmp/digest-py.err; prc=$?
if [ $jrc != 0 ] || [ $prc != 0 ]; then
  echo "CRASH   digest drivers (js=$jrc py=$prc)"
  head -5 /tmp/digest-js.err; tail -5 /tmp/digest-py.err
  exit 1
fi

python3 - <<'PY'
import json, sys

js = json.load(open("/tmp/digest-js.json"))
py = json.load(open("/tmp/digest-py.json"))

# EMPTY, and it must stay empty. Unlike regex-worker-diff.sh's pin, there is no legitimate
# "known divergence" here: a digest that differs is a defect with no design question attached,
# because the oracle's bytes ARE the specification. Nothing to emulate-or-document.
EXPECTED_DIVERGENT = set()

# VACUITY: the two case lists are duplicated across the language boundary (they must be -- the
# whole point is that each side builds the value with its own primitives, e.g. a surrogate PAIR
# in JS against a single code point in Python). So a list edited on one side only would compare
# a shorter prefix and could agree by coincidence.
if set(js) != set(py):
    print("VACUOUS: case sets differ")
    print("    js only: %s" % sorted(set(js) - set(py)))
    print("    py only: %s" % sorted(set(py) - set(js)))
    sys.exit(1)
if len(js) < 10:
    print("VACUOUS: only %d cases" % len(js))
    sys.exit(1)

divergent = set()
for name in js:
    if js[name]["sha256"] == py[name]["sha256"]:
        print("ok       %s" % name)
        continue
    divergent.add(name)
    print("DIVERGE  %s" % name)
    # The JSON, not just the digest. A hash tells you THAT they differ; the serialized text
    # tells you WHERE, and that is the whole diagnostic value on this path.
    print("         js json: %s" % js[name]["json"])
    print("         py json: %s" % py[name]["json"])

print()
if divergent != EXPECTED_DIVERGENT:
    print("--- DIGEST DIVERGENCE ---")
    print("    appeared: %s" % sorted(divergent - EXPECTED_DIVERGENT))
    print("    vanished: %s" % sorted(EXPECTED_DIVERGENT - divergent))
    print("    A differing digest means EVERY approval token would fail to match. Fix the")
    print("    serializer; do not update this set.")
    sys.exit(1)
print("--- %d cases, digests identical ---" % len(js))
PY
