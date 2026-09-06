#!/bin/bash
# Compare the two implementations of filesystem discovery across seven tree shapes.
#
# Usage: bash tests/discovery-diff.sh   (run from skills/agents-discipline)
set -u
# A .pyc is validated on (source mtime, source size) ALONE, so a same-size rewrite inside one
# mtime second -- exactly what a mutation probe does -- runs STALE bytecode while the .py reads
# correct. No .pyc, no staleness.
export PYTHONDONTWRITEBYTECODE=1
# resolveTarget falls back to this variable, so a value inherited from the caller's shell would
# silently steer every row that means to test the no-scope-given path.
unset AGENTS_DISCIPLINE_SCOPE
fail=0
parent=$(mktemp -d)

for variant in one-scope two-scopes no-scopes hostile-entries scope-is-a-file \
               state-is-a-symlink dangling-state-link empty; do
  root=$(python3 tests/build-discovery-tree.py "$variant" "$parent" 2>/tmp/build.err)
  # THE VACUITY GUARD, and it is not hypothetical: an exception in the builder killed it before
  # it printed the root, so `root` was EMPTY and both drivers ran against "" -- and AGREED,
  # perfectly, about a tree that was never built. A differential compares two answers; it
  # cannot tell you they answered the right question. Measured, then guarded.
  if [ -z "$root" ] || [ ! -d "$root" ]; then
    fail=1; echo "BUILD-FAILED $variant"; head -5 /tmp/build.err; continue
  fi
  node tests/discovery-drive.mjs "$root" >/tmp/disc-js.json 2>/tmp/disc-js.err; jrc=$?
  python3 tests/discovery_drive.py "$root" >/tmp/disc-py.json 2>/tmp/disc-py.err; prc=$?
  if [ $jrc != 0 ] || [ $prc != 0 ]; then
    fail=1; echo "CRASH   $variant (js=$jrc py=$prc)"
    head -4 /tmp/disc-js.err /tmp/disc-py.err
  elif diff -q /tmp/disc-js.json /tmp/disc-py.json >/dev/null; then
    echo "OK      $variant"
  else
    fail=1; echo "DIVERGE $variant"; diff /tmp/disc-js.json /tmp/disc-py.json | head -14
  fi
done

# WHAT THIS FILE CANNOT WITNESS, measured rather than assumed. Three mutations survive every
# variant above, and they share ONE cause: _real_directory_inside lstats and rejects symlinks
# itself, which SUBSUMES the guards in front of it.
#   * is_dir(follow_symlinks=False) -> True        (a symlinked scope dir is rejected anyway)
#   * realpath(..., strict=True) -> strict=False   (lstat already failed for a missing path)
#   * os.path.exists -> os.path.lexists            (both answer the same for a dangling link,
#                                                   because the S_ISLNK check runs next)
# The oracle carries exactly the same redundancy, so this is faithful, not a porting defect --
# but it means those three spellings are DEFENCE IN DEPTH, not verified behaviour. Do not read
# a green run here as covering them. dangling-state-link is kept for a different reason it DOES
# earn: it is the only shape reaching resolve_target's state_path branch through a dangling
# link (mode "scope" + a real-directory error, where `empty` answers "no such scope").
#
# Non-vacuity, asserted rather than eyeballed. Two shapes carry the load-bearing claims, and
# both were once satisfied by an empty answer: hostile-entries must KEEP exactly one scope
# while excluding seven decoys (an all-excluding bug reads as [] and passes any diff), and
# one-scope must list BOTH astral filenames in UTF-16 order (the divergence this file found).
root=$(python3 tests/build-discovery-tree.py hostile-entries "$parent")
node tests/discovery-drive.mjs "$root" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["listScopes"] == ["keep"], "hostile-entries is VACUOUS: " + repr(d["listScopes"])
assert d["targets"]["bare"]["scope"] == "keep", "bare target lost the only scope"
' || { fail=1; echo "VACUOUS hostile-entries"; }
root=$(python3 tests/build-discovery-tree.py one-scope "$parent")
node tests/discovery-drive.mjs "$root" | python3 -c '
import json, sys
names = [p.rsplit("/", 1)[-1] for p in json.load(sys.stdin)["scopeFiles"][0]]
assert names[-2:] == [chr(0x1F600) + ".md", chr(0xFFFD) + ".md"], \
    "the astral pair is absent or in code-point order: " + repr(names)
' || { fail=1; echo "VACUOUS one-scope (astral row not exercised)"; }

echo "--- $( [ $fail = 0 ] && echo 'all identical' || echo 'FAILURES ABOVE' ) ---"
exit $fail
