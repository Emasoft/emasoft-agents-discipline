#!/usr/bin/env bash
# Kept out of the `-diff.sh` suffix ON PURPOSE: `test:diff` is `for f in tests/*-diff.sh` and
# this row costs ~34s, so it belongs to `test:slow` only -- renaming it to match its siblings
# puts the 34s back into every per-commit run with nothing reddening.
#
# Differential for run_check's post-spawn wait in gate_check.py (no oracle counterpart bounds
# the success path at all -- the oracle settles on `child.once("close", settle)`,
# gate-check.mjs:696, with no fixed cap). MEASURED 2026-09-08, before the wait-first fix: a
# CHECK of `sleep 20; echo ok` under the default 120s --timeout PASSES on the oracle at ~20s and
# is SIGKILLed by the port at ~15s with `signal=SIGKILL`, `matched=false`, no "timed out after"
# text -- the three chained 5.0s join/wait bounds in run_check expired before the Timer that
# owns the real deadline could fire.
#
# 17s is the smallest sleep that crosses the observed ~15s kill point without sitting on the
# whole-second bucket edge 16s did in the first draft. This row asserts that the CHECK is NOT
# killed before its budget -- it does not assert the kill happens AT the budget, so a machine
# that merely stalls exec past the budget (a busy CI runner, cold interpreter start) reddens
# this row for an environmental reason, not necessarily the join/wait bound under test. This
# file does not assert a literal duration, only that BOTH runtimes PASS (proving the port's
# kill window widened past 17s) and BOTH print the exact PASS line, which a SIGKILLed run never
# does (it prints FAIL with signal=SIGKILL instead).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORACLE="$HERE/../scripts/gate-check.mjs"
PORT="$HERE/../scripts/gate_check.py"
export PYTHONDONTWRITEBYTECODE=1
PY_ABS="$(command -v python3)"
NODE_ABS="$(command -v node)"

WORK="$(mktemp -d)"
APPROVALS="$(mktemp -d)"
export AGENTS_DISCIPLINE_APPROVAL_DIR="$APPROVALS"
trap 'rm -rf "$WORK" "$APPROVALS"' EXIT

_write_fixture() {
  local root="$1"
  mkdir -p "$root/.agents-discipline/s/gates"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: sleep 17; echo ok\n  EXPECT: ok\n' \
    > "$root/.agents-discipline/s/gates/leaf.md"
}

_run() {  # interpreter script root outvar codevar
  local interpreter="$1" script="$2" root="$3"
  local tmp_out="$WORK/.out" code
  "$interpreter" "$script" --root "$root" --scope s --approve --reverify > "$tmp_out" 2>&1
  code=$?
  printf -v "$4" '%s' "$(cat "$tmp_out")"
  printf -v "$5" '%s' "$code"
}

label="CHECK past 15s still PASSes (not SIGKILLed early)"
root="$WORK/r1"
rm -rf "$root" "$APPROVALS"; mkdir -p "$root" "$APPROVALS"; chmod 700 "$APPROVALS"
_write_fixture "$root"
o_out=""; o_code=""
_run "$NODE_ABS" "$ORACLE" "$root" o_out o_code
if ! printf '%s' "$o_out" | grep -qF '  PASS leaf:G1: x'; then
  printf 'DIVERGE  %-40s oracle did not PASS -- fixture premise broken\n' "$label"
  printf '%s\n' "$o_out" | sed 's/^/    | /'
  exit 1
fi

rm -rf "$root" "$APPROVALS"; mkdir -p "$root" "$APPROVALS"; chmod 700 "$APPROVALS"
_write_fixture "$root"
p_out=""; p_code=""
_run "$PY_ABS" "$PORT" "$root" p_out p_code
if [ "$o_code" = "$p_code" ] && printf '%s' "$p_out" | grep -qF '  PASS leaf:G1: x'; then
  printf '  OK      %-40s both PASS; exit oracle=%s port=%s\n' "$label" "$o_code" "$p_code"
  echo
  echo "--- 1 differential(s) identical ---"
  exit 0
fi
printf 'DIVERGE  %-40s exit oracle=%s port=%s\n' "$label" "$o_code" "$p_code"
printf '%s\n' "$p_out" | sed 's/^/    | /'
echo
printf -- '--- 1 DIVERGENCE(S): %s ---\n' "$label"
exit 1
