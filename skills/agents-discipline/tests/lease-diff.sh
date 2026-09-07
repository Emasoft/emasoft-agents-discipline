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
status=0
if diff -q /tmp/lease-js.json /tmp/lease-py.json >/dev/null; then
  echo "OK      lease sequence (22 steps)"
else
  echo "DIVERGE lease sequence"; diff /tmp/lease-js.json /tmp/lease-py.json | head -20
  status=1
fi

# --- CLI-level differential for gate_check.py's --claim/--release ACTION WIRING itself
# (gate-check.mjs:251-295), which the sequence above never exercises: lease_drive.py/mjs call
# claim_leases/release_leases DIRECTLY, bypassing gate_check.py entirely -- a bug in argv
# handling, stems-from-filename, OWNS extraction from a real ledger, or the CONFLICT/CLAIMED/
# "released N lease(s)" message text would be invisible to it. Two FRESH roots (one per
# runtime), never one shared root: a lock file records a pid, so replaying the identical
# sequence against ONE root would make the second runtime's claim collide with the first
# runtime's still-live lease and manufacture a CONFLICT that has nothing to do with either
# implementation being wrong.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORACLE="$HERE/../scripts/gate-check.mjs"
PORT="$HERE/../scripts/gate_check.py"
LEDGER='# Gates

OWNS: src/**

- [ ] G1: x
  CHECK: true
  EXPECT: ok
'
cli_root() {
  # `.agents-discipline/s/leaf.md`, NOT a bare `gates/leaf.md` -- resolveTarget only honors
  # legacy `gates/*.md` discovery when NO --scope is given. The CLI sequence below always
  # passes `--scope s`, so a ledger anywhere else makes every claim fail at "no such scope"
  # BEFORE it ever reaches the ported --claim/--release code this section exists to exercise.
  # MEASURED: the first draft used `gates/leaf.md` and both claim calls failed identically on
  # "no such scope", so the section reported OK while testing nothing past resolve_target.
  # _scope_discovery (gates.py) only looks at `<scope>/GATES.md` and `<scope>/gates/*.md` --
  # a bare `<scope>/leaf.md` is invisible to it and silently falls through to "no gate files
  # found", which is what the first two drafts of this fixture did.
  local root; root=$(mktemp -d)
  mkdir -p "$root/.agents-discipline/s/gates"
  printf '%s' "$LEDGER" > "$root/.agents-discipline/s/gates/leaf.md"
  printf '%s' "$root"
}
# Scrubs the root's own path out of the transcript so two DIFFERENT tmp roots compare equal.
cli_scrub() { sed "s|$1|<R>|g"; }

cli_sequence() {  # interpreter script root
  local interpreter="$1" script="$2" root="$3"
  { "$interpreter" "$script" --root "$root" --claim --scope s; echo "exit=$?"
    "$interpreter" "$script" --root "$root" --claim --scope s; echo "exit=$?"  # CONFLICT
    "$interpreter" "$script" --root "$root" --release --scope s; echo "exit=$?"
    "$interpreter" "$script" --root "$root" --release --scope s; echo "exit=$?"
  } 2>&1 | cli_scrub "$root"
}
js_root=$(cli_root); py_root=$(cli_root)
js_cli="$(cli_sequence node "$ORACLE" "$js_root")"
py_cli="$(cli_sequence python3 "$PORT" "$py_root")"
if [ "$js_cli" = "$py_cli" ]; then
  echo "OK      lease CLI wiring (claim, conflict, release, no-op release)"
else
  echo "DIVERGE lease CLI wiring"
  diff <(printf '%s\n' "$js_cli") <(printf '%s\n' "$py_cli")
  status=1
fi
rm -rf "$js_root" "$py_root"

if [ "$status" = 0 ]; then echo "--- all identical ---"; else echo "--- DIVERGENCES ABOVE ---"; fi
exit "$status"
