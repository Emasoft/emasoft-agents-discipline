#!/bin/bash
# Compare node:path against its Python counterparts, per FUNCTION, and pin the known gaps.
#
# `gate-check.mjs:13` imports EIGHT node:path functions and only two have JS-faithful ports.
# That `join` and `basename` each needed one is the evidence that os.path is NOT node:path in
# this codebase; this file measures the other six instead of assuming either way.
#
# Usage: bash tests/path-api-diff.sh   (run from skills/agents-discipline)
set -u
export PYTHONDONTWRITEBYTECODE=1
node tests/path-api-drive.mjs > /tmp/path-js.json 2>/tmp/path-js.err; jrc=$?
python3 tests/path_api_drive.py > /tmp/path-py.json 2>/tmp/path-py.err; prc=$?
if [ $jrc != 0 ] || [ $prc != 0 ]; then
  echo "CRASH   path api (js=$jrc py=$prc)"
  head -5 /tmp/path-js.err; tail -5 /tmp/path-py.err
  exit 1
fi

# PARSED, per key, not a byte diff of the two files -- the established policy here
# (python-lib-checks.py:131-136). A byte diff would report every key as different over
# json.dumps' ", " against JSON.stringify's ",", which has already cost this project one
# reverted production edit and one throwaway probe that printed DIVERGES for identical data.
python3 - <<'PY'
import json, sys

js = json.load(open("/tmp/path-js.json"))
py = json.load(open("/tmp/path-py.json"))

# KNOWN GAPS: functions with no JS-faithful port yet. Pinned as a SET of names, not a count --
# a count holds steady under a swap (one gap closed while another opens), which is the exact
# defect found in regex-worker-diff.sh's first pin.
#
# `resolve` is here because of a TWO-WRONGS-CANCEL that an uncommitted probe could not see.
# That probe built Python's resolve as `abspath(os.path.join(...))` and reported AGREEMENT.
# But os.path.join DISCARDS everything before an absolute segment (`join("/a","/b")` -> "/b")
# where node's join does NOT (-> "/a/b"). Python's join is therefore a WRONG port of node's
# join -- and its wrongness is exactly the reset that node's RESOLVE performs, so the bad
# helper produced the right answer and hid the gap. Using the real `_js_join` (which correctly
# does not discard) makes `resolve("/a","/b")` come out "/a/b" against node's "/b", which is
# the truth: node:path's resolve has no port at all.
EXPECTED_DIVERGENT = {"dirname", "relative", "resolve"}

# VACUITY: compare the corpus SIZES first. The two drivers duplicate their case lists rather
# than sharing them across the language boundary, so a list edited on one side only would
# otherwise be compared element-by-element against a different corpus and could agree by
# coincidence on the shorter prefix.
sizes = {k: (len(v), len(py.get(k, []))) for k, v in js.items() if isinstance(v, list)}
bad_sizes = {k: v for k, v in sizes.items() if v[0] != v[1]}
if bad_sizes:
    print("VACUOUS: corpus sizes differ between drivers: %s" % bad_sizes)
    sys.exit(1)
if sum(n for n, _ in sizes.values()) < 40:
    print("VACUOUS: only %d cases total" % sum(n for n, _ in sizes.values()))
    sys.exit(1)

divergent = set()
for key in sorted(js):
    if js[key] == py.get(key):
        print("ok       %s" % key)
        continue
    divergent.add(key)
    print("DIVERGE  %s" % key)
    if isinstance(js[key], list):
        for i, (a, b) in enumerate(zip(js[key], py.get(key, []))):
            if a != b:
                print("         [%d] js=%r  py=%r" % (i, a, b))
    else:
        print("         js=%r  py=%r" % (js[key], py.get(key)))

print()
if divergent != EXPECTED_DIVERGENT:
    print("--- DIVERGENCE SET CHANGED ---")
    print("    appeared: %s" % sorted(divergent - EXPECTED_DIVERGENT))
    print("    vanished: %s" % sorted(EXPECTED_DIVERGENT - divergent))
    print("    An APPEARED name is a port defect or a newly-probed shape. A VANISHED one means")
    print("    that function was ported -- update this set in the SAME commit as the port.")
    sys.exit(1)
print("--- known port gaps only (%s), set unchanged ---" % ", ".join(sorted(EXPECTED_DIVERGENT)))
PY
