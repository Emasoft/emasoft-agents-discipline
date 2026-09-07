#!/usr/bin/env bash
# Differential for NON-ASCII round-tripping across every surface the port WRITES or PRINTS.
#
# WHY THIS FILE EXISTS: two real defects of exactly this shape shipped, and both were found by
# hand rather than by the suite. Each was a bare `json.dumps` whose ensure_ascii defaults to
# True, against a `JSON.stringify` that emits the character raw:
#
#   gates.py claim_leases      the LEASE FILE's bytes          (fixed 87640c8)
#   gates.py parse warnings    the EXPECT warning a USER READS (fixed 13c2c4e)
#
# Neither had regression coverage afterwards, which is how the second one survived the commit
# that fixed the first. lease-diff.sh does exercise the lease machinery -- with ASCII fixtures,
# which is precisely why the divergence was invisible to it. A differential only tests the
# vectors it presents, and "non-ASCII" was a vector nothing in the suite presented.
#
# THE FAILURE IS SILENT IN BOTH CASES, and for the same reason: a backslash-u escape ROUND-TRIPS
# within one runtime. json.load decodes it straight back to the same str, so every behavioural
# assertion passes on either side. Only the BYTES differ -- and the lease file is the one
# artifact whose reader may be the other runtime.
#
# DIVERGE LINES START AT COLUMN 0 (see gate-args-diff.sh's note); every other verdict word keeps
# a 2-space indent so mutate-probe.sh's anchored `^DIVERGE` grep stays the only trigger.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORACLE="$HERE/../scripts/gate-check.mjs"
PORT="$HERE/../scripts/gate_check.py"
export PYTHONDONTWRITEBYTECODE=1
PY_ABS="$(command -v python3)"
NODE_ABS="$(command -v node)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
declare -a FAILED=()

# Built with printf and an explicit UTF-8 byte sequence, never a literal character: a writing
# layer between here and the file has already turned an escape into a raw character once in this
# project, and a fixture that silently loses its non-ASCII tests nothing while still passing.
NON_ASCII="$(printf 'caf\xc3\xa9')"

_scrub() { sed -e "s|$1|<ROOT>|g" -e 's/"pid": [0-9]*/"pid": <PID>/'; }

# --- CASE 1: the lease file's BYTES -------------------------------------------------------
# Two separate roots, one per runtime: a lease records a pid, so replaying the same claim
# against ONE root would make the second runtime collide with the first's live lease and
# manufacture a conflict that is not about either implementation (lease-diff.sh's own note).
_lease_bytes() {
  local exe="$1" script="$2" root="$3"
  mkdir -p "$root/.agents-discipline/s/gates"
  printf '# Gates\n\nOWNS: src/%s/**\n\n- [ ] G1: x\n  CHECK: true\n  EXPECT: ok\n' "$NON_ASCII" \
    > "$root/.agents-discipline/s/gates/leaf.md"
  "$exe" "$script" --root "$root" --claim --scope s >/dev/null 2>&1
  local f; f="$(find "$root" -name '*.lease' -type f | head -1)"
  [ -n "$f" ] || { echo "(no lease file written)"; return; }
  _scrub "$root" < "$f"
}
o_lease="$(_lease_bytes "$NODE_ABS" "$ORACLE" "$WORK/lo")"
p_lease="$(_lease_bytes "$PY_ABS" "$PORT" "$WORK/lp")"
# NON-VACUITY: the fixture must actually have carried the non-ASCII glob into the file. Without
# this, a claim that failed for an unrelated reason would leave both sides equal and "identical".
if ! printf '%s' "$o_lease" | grep -q "$NON_ASCII"; then
  printf 'DIVERGE  %-38s oracle lease lacks the non-ASCII glob; fixture reached nothing\n' \
    "lease file bytes"
  printf '%s\n' "$o_lease" | sed 's/^/    | /'
  fail=$((fail + 1)); FAILED+=("lease file bytes")
elif [ "$o_lease" = "$p_lease" ]; then
  printf '  OK      %-38s non-ASCII glob written identically\n' "lease file bytes"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "lease file bytes"
  diff <(printf '%s\n' "$o_lease") <(printf '%s\n' "$p_lease")
  fail=$((fail + 1)); FAILED+=("lease file bytes")
fi

# --- CASE 2: the EXPECT warning a user READS ----------------------------------------------
# The pathLike branch needs the WRAPPING slashes AND an inner slash. A first attempt at this
# fixture had only the wrapping pair, produced NO warning from either runtime, and that empty
# result was nearly recorded as agreement -- it was a fixture that reached nothing. Hence the
# non-vacuity check below, which is not decoration.
_expect_warning() {
  local exe="$1" script="$2" root="$3"
  mkdir -p "$root/.agents-discipline/s/gates"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: true\n  EXPECT: /src/%s/out.txt/\n' \
    "$NON_ASCII" > "$root/.agents-discipline/s/gates/leaf.md"
  "$exe" "$script" --root "$root" --scope s --status 2>&1 | _scrub "$root"
}
o_warn="$(_expect_warning "$NODE_ABS" "$ORACLE" "$WORK/wo")"
p_warn="$(_expect_warning "$PY_ABS" "$PORT" "$WORK/wp")"
if ! printf '%s' "$o_warn" | grep -q 'read as a regular expression'; then
  printf 'DIVERGE  %-38s oracle printed no pathLike warning; fixture reached nothing\n' \
    "EXPECT warning text"
  printf '%s\n' "$o_warn" | sed 's/^/    | /'
  fail=$((fail + 1)); FAILED+=("EXPECT warning text")
elif [ "$o_warn" = "$p_warn" ]; then
  printf '  OK      %-38s non-ASCII EXPECT reported identically\n' "EXPECT warning text"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "EXPECT warning text"
  diff <(printf '%s\n' "$o_warn") <(printf '%s\n' "$p_warn")
  fail=$((fail + 1)); FAILED+=("EXPECT warning text")
fi

echo
if [ "$fail" = 0 ]; then
  echo "--- $pass non-ASCII surface(s) identical ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
