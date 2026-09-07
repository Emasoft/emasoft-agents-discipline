#!/usr/bin/env bash
# Differential for the --approve classification loop of gate-check.mjs vs scripts/gate_check.py:
# approval_oracle_signature (gate_check.py:588), approval_path (:603), validated_approval_dir
# (:623), read_approval_file (:645), approval_exists (:683), record_approval (:704).
#
# BLACK BOX, ON PURPOSE, same idiom as gate-args-diff.sh. Neither runtime exports these
# functions, so both PROGRAMS are run for real against AGENTS_DISCIPLINE_APPROVAL_DIR
# (gate-check.mjs:368, gate_check.py:597) and the APPROVAL FILE each one writes is compared.
#
# WHY THIS FILE EXISTS: gate-args-diff.sh only ever passes --approve as an argument STRING (it
# never reaches a discovered gate, so recordApproval/approval_exists never run). digest-diff.sh
# compares two hand-built serializers, never the production approval_oracle_signature. So if the
# two runtimes hash a different token for the same (file, gate, oracle), every approval one
# writes is silently unreadable by the other -- and the symptom looks exactly like a missing
# --approve, which is what makes this gap dangerous.
#
# THE ONE LEGITIMATE DIFFERENCE: `approvedAt` is a wall-clock ISO timestamp
# (gate-check.mjs:498, gate_check.py:737) written fresh by each process, so it can never be
# byte-identical across two separate runs. Every OTHER key -- schema/file/gate/signature/oracle,
# in that exact order, since both runtimes build the object with the same field order and
# JSON.stringify(v,null,2)/json.dumps(v,indent=2) format identically -- must match byte for byte.
# `_normalize` strips only that one line before diffing.
#
# NON-VACUITY IS ASSERTED, NOT ASSUMED: case 2 proves the same comparison rejects a real change
# (a one-character CHECK edit must yield a different token/filename) before case 1's agreement is
# trusted as meaningful.
#
# DIVERGE LINES START AT COLUMN 0 (see gate-args-diff.sh's own note); every other verdict word
# here keeps a 2-space indent so mutate-probe.sh's anchored `^DIVERGE` grep stays the only trigger.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORACLE="$HERE/../scripts/gate-check.mjs"
PORT="$HERE/../scripts/gate_check.py"
export PYTHONDONTWRITEBYTECODE=1
# Resolved ONCE, before CASE 5 appends an invalid-UTF8 byte to PATH: `env PATH=<bad>` still
# lets bash resolve a BARE `node`/`python3` through the mutated PATH, and on this machine that
# would pick up macOS system python3.9 (no os.path.realpath(strict=) -- an unrelated TypeError
# masquerading as a code defect). Absolute paths sidestep PATH resolution entirely.
PY_ABS="$(command -v python3)"
NODE_ABS="$(command -v node)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0; crash=0
declare -a FAILED=()

# A two-gate scoped ledger. Both CHECK commands are trivial and POSIX-portable so the run
# reaches a deterministic PASS on every platform this project is measured against; the
# approval loop runs identically whether the gate ultimately passes or fails.
_write_ledger() {
  local dir="$1" g1_check="${2:-printf hello}"
  mkdir -p "$dir/.agents-discipline/onescope"
  cat > "$dir/.agents-discipline/onescope/GATES.md" <<EOF
# Gates: onescope

- [ ] G1: first outcome
  CHECK: $g1_check
  EXPECT: hello

- [ ] G2: second outcome
  CHECK: printf world
  EXPECT: world
EOF
}

# Strips the one legitimately-volatile line so the rest can be diffed byte for byte.
_normalize() { grep -v '"approvedAt"' "$1"; }

_crashed() {
  if printf '%s' "$2" | grep -q '^Traceback (most recent call last)'; then
    printf '  CRASH    %-42s %s\n' "$1" "$(printf '%s' "$2" | tail -1)"
    crash=$((crash + 1)); return 0
  fi
  return 1
}

# Runs a program with --approve against a repo root and approval dir. Sets g_code/g_out/g_err.
# --reverify is load-bearing, not decoration: the oracle flips a passing gate's checkbox to
# [x] (gate-check.mjs:846), so once it has run once on a shared ledger the gate reads "met"
# and drops out of the pending set on every later run -- of EITHER runtime -- before the
# approval loop is ever reached. --reverify forces the gate back into the pending set
# regardless of prior runs, which is what lets the SAME ledger absolute path (required so the
# token's `resolve(file)` component matches) be reused across oracle/port and across cases.
_run_approve() {
  local prog_kind="$1" repo="$2" approval_dir="$3"
  if [ "$prog_kind" = oracle ]; then
    g_out="$(cd "$WORK" && AGENTS_DISCIPLINE_APPROVAL_DIR="$approval_dir" node "$ORACLE" --approve --reverify --root "$repo" 2>"$WORK/e.err")"
  else
    g_out="$(cd "$WORK" && AGENTS_DISCIPLINE_APPROVAL_DIR="$approval_dir" python3 -B "$PORT" --approve --reverify --root "$repo" 2>"$WORK/e.err")"
  fi
  g_code=$?
  g_err="$(cat "$WORK/e.err")"
}

# Finds the approval json (never the sibling .lock) for a given gate id inside a directory.
_token_for_gate() {
  local dir="$1" gate_id="$2" f
  while IFS= read -r f; do
    if grep -qE "\"gate\": *\"$gate_id\"" "$f"; then printf '%s\n' "$f"; return 0; fi
  done < <(find "$dir" -maxdepth 1 -type f -name '*.json')
  return 1
}

echo "gate-check --approve classification loop -- oracle vs port"
echo "-- CASE 1: a fresh approve writes byte-identical (modulo approvedAt) records --"

REPO="$WORK/repo"; _write_ledger "$REPO"
APPR_O="$WORK/appr-oracle"; APPR_P="$WORK/appr-port"

_run_approve oracle "$REPO" "$APPR_O"; o_code=$g_code; o_out="$g_out"; o_err="$g_err"
_run_approve port   "$REPO" "$APPR_P"; p_code=$g_code; p_out="$g_out"; p_err="$g_err"

if _crashed "approve one gate" "$p_err"; then
  :
else
  ok=1
  tok1_o="" tok1_p=""
  for gid in G1 G2; do
    tf_o="$(_token_for_gate "$APPR_O" "$gid")" || { ok=0; printf 'DIVERGE  %-42s oracle wrote no approval file for %s\n' "approve one gate" "$gid"; break; }
    tf_p="$(_token_for_gate "$APPR_P" "$gid")" || { ok=0; printf 'DIVERGE  %-42s port wrote no approval file for %s\n' "approve one gate" "$gid"; break; }
    [ "$gid" = G1 ] && { tok1_o="$(basename "$tf_o")"; tok1_p="$(basename "$tf_p")"; }
    if [ "$(basename "$tf_o")" != "$(basename "$tf_p")" ]; then
      ok=0
      printf 'DIVERGE  %-42s token filename differs for %s: %s vs %s\n' "approve one gate" "$gid" "$(basename "$tf_o")" "$(basename "$tf_p")"
      break
    fi
    if ! diff -q <(_normalize "$tf_o") <(_normalize "$tf_p") >/dev/null; then
      ok=0
      printf 'DIVERGE  %-42s approval body differs for %s\n' "approve one gate" "$gid"
      diff <(_normalize "$tf_o") <(_normalize "$tf_p") | head -20
      break
    fi
  done
  # The port is now a full end-to-end implementation, so its exit code must agree with the
  # oracle's real PASS/FAIL/verdict exit, not a porting-boundary placeholder.
  if [ "$p_code" != "$o_code" ]; then
    ok=0
    printf 'DIVERGE  %-42s port exit %s, expected %s (oracle)\n' "approve one gate" "$p_code" "$o_code"
  fi
  if [ "$ok" = 1 ]; then pass=$((pass + 1)); else fail=$((fail + 1)); FAILED+=("approve one gate"); fi
fi

echo "-- CASE 2: non-vacuity control -- a changed CHECK must yield a different token --"

REPO_CTRL="$WORK/repo-ctrl"; _write_ledger "$REPO_CTRL" "printf helloX"
APPR_CTRL="$WORK/appr-ctrl"
_run_approve oracle "$REPO_CTRL" "$APPR_CTRL"; c_code=$g_code; c_err="$g_err"

if _crashed "control: mutated CHECK" "$c_err"; then
  :
elif tf_ctrl="$(_token_for_gate "$APPR_CTRL" G1)"; then
  ctrl_tok="$(basename "$tf_ctrl")"
  if [ -z "$tok1_o" ]; then
    printf 'DIVERGE  %-42s case 1 produced no G1 token to compare against\n' "control: mutated CHECK"
    fail=$((fail + 1)); FAILED+=("control: mutated CHECK")
  elif [ "$ctrl_tok" = "$tok1_o" ]; then
    printf 'DIVERGE  %-42s VACUOUS: a changed CHECK produced the SAME token (%s) -- the\n' "control: mutated CHECK" "$ctrl_tok"
    printf '                                             comparison in case 1 cannot discriminate a real defect\n'
    fail=$((fail + 1)); FAILED+=("control: mutated CHECK")
  else
    pass=$((pass + 1))
    echo "  control fired: G1 token changed ($tok1_o -> $ctrl_tok) when CHECK changed by one character"
  fi
else
  printf 'DIVERGE  %-42s oracle wrote no approval file for the mutated ledger\n' "control: mutated CHECK"
  fail=$((fail + 1)); FAILED+=("control: mutated CHECK")
fi

echo "-- CASE 3: two gates in one ledger both compare, with distinct tokens --"

tf_o_g2="$(_token_for_gate "$APPR_O" G2 2>/dev/null)" || true
if [ -n "$tok1_o" ] && [ -n "${tf_o_g2:-}" ] && [ "$tok1_o" != "$(basename "$tf_o_g2")" ]; then
  pass=$((pass + 1))
  echo "  G1 and G2 produced distinct tokens ($tok1_o vs $(basename "$tf_o_g2")), both agreed above"
else
  printf 'DIVERGE  %-42s G1 and G2 collided on the same token, or G2 was never written\n' "two gates distinct tokens"
  fail=$((fail + 1)); FAILED+=("two gates distinct tokens")
fi

echo "-- CASE 4: re-approving an already-approved gate is a no-op in both runtimes --"

before_o="$(cat "$(_token_for_gate "$APPR_O" G1)")"
before_p="$(cat "$(_token_for_gate "$APPR_P" G1)")"
_run_approve oracle "$REPO" "$APPR_O"; ro_code=$g_code; ro_err="$g_err"
_run_approve port   "$REPO" "$APPR_P"; rp_code=$g_code; rp_err="$g_err"

if _crashed "idempotent re-approve" "$rp_err"; then
  :
else
  after_o="$(cat "$(_token_for_gate "$APPR_O" G1)")"
  after_p="$(cat "$(_token_for_gate "$APPR_P" G1)")"
  # rp_code is now asserted against ro_code -- the port is a full implementation, so a
  # re-approve's exit code must agree with the oracle's, same as case 1.
  if [ "$before_o" = "$after_o" ] && [ "$before_p" = "$after_p" ] && [ "$rp_code" = "$ro_code" ]; then
    pass=$((pass + 1))
  else
    printf 'DIVERGE  %-42s re-approval rewrote an existing approval record\n' "idempotent re-approve"
    [ "$before_o" != "$after_o" ] && echo "    oracle record changed on re-approve"
    [ "$before_p" != "$after_p" ] && echo "    port record changed on re-approve"
    [ "$rp_code" != "$ro_code" ] && printf '    port exit %s, expected %s (oracle)\n' "$rp_code" "$ro_code"
    fail=$((fail + 1)); FAILED+=("idempotent re-approve")
  fi
fi

echo "-- CASE 5: a lone surrogate byte reaching PATH must not crash the port's approval write --"
# Regression case for commit 3eb902d: gate_check.py:739 wrote the approval record with a bare
# json.dumps(..., ensure_ascii=False). CPython surrogateescape-decodes os.environ, so one
# invalid UTF-8 byte in PATH puts a lone surrogate (U+DCFF) into oracle()["path"]; the bare
# dumps left it raw and write_atomic's utf-8 .encode() then raised UnicodeEncodeError -- the
# port crashed and classified the task not_run where the oracle wrote the approval and exited 0.
#
# NOT a byte-for-byte case like 1/2/4: MEASURED (not assumed) that Node and CPython decode an
# invalid PATH byte to DIFFERENT scalars -- Node replaces it with U+FFFD (process.env.PATH
# decodes through the OS's UTF-8-with-replacement, confirmed via `codePointAt` -> 0xfffd, no
# escape at all in the JSON), CPython's surrogateescape instead preserves it as U+DCFF (escaped
# to the literal ASCII sequence `\udcff` by _LONE_SURROGATE_RE). Two genuinely different
# characters, so `oracle.path` -- and everything hashed from it (signature, token) -- can never
# match across runtimes for this vector, with or without the fix. That divergence is real but
# orthogonal to the bug this case targets, so this case asserts OUTCOME agreement (both write a
# valid, uncorrupted approval for G1 without crashing) rather than byte agreement.
#
# `_crashed` is deliberately NOT used here: for every OTHER case in this file a Python traceback
# means the test harness itself broke (guard territory), but here a traceback IS the exact
# defect under test -- classifying it as generic CRASH (not DIVERGE) would make a reverted fix
# invisible to mutate-probe.sh, which counts only DIVERGE as a catch.

REPO_SG="$WORK/repo-sg"; _write_ledger "$REPO_SG"
APPR_SG_O="$WORK/appr-sg-oracle"; APPR_SG_P="$WORK/appr-sg-port"
BADPATH="${PATH}:/opt/$(printf '\xff')junk"

sg_o_out="$(cd "$WORK" && env PATH="$BADPATH" AGENTS_DISCIPLINE_APPROVAL_DIR="$APPR_SG_O" "$NODE_ABS" "$ORACLE" --approve --reverify --root "$REPO_SG" 2>"$WORK/sg-o.err")"
sg_o_code=$?
sg_p_out="$(cd "$WORK" && env PATH="$BADPATH" AGENTS_DISCIPLINE_APPROVAL_DIR="$APPR_SG_P" "$PY_ABS" -B "$PORT" --approve --reverify --root "$REPO_SG" 2>"$WORK/sg-p.err")"
sg_p_code=$?
sg_p_err="$(cat "$WORK/sg-p.err")"

sg_ok=1
if printf '%s' "$sg_p_err" | grep -q '^Traceback (most recent call last)'; then
  sg_ok=0
  printf 'DIVERGE  %-42s port crashed serializing the approval record: %s\n' \
    "surrogate PATH byte" "$(printf '%s' "$sg_p_err" | tail -1)"
else
  # No exit-code assertion here: the note above already establishes that Node and CPython
  # decode the invalid PATH byte to different scalars, so oracle.path (and everything hashed
  # from it) genuinely differs across runtimes for this one vector -- this case asserts OUTCOME
  # agreement (no crash, a valid approval file) rather than exit-code or byte agreement.
  if tf_sg_p="$(_token_for_gate "$APPR_SG_P" G1)"; then
    if ! grep -q '\\udcff' "$tf_sg_p"; then
      sg_ok=0
      printf 'DIVERGE  %-42s port approval file lacks the expected \\udcff escape\n' "surrogate PATH byte"
    fi
    # A raw surrogate can never appear in legally-encoded UTF-8, so this is really "the file is
    # valid UTF-8 end to end" -- the exact property write_atomic's own .encode() failed on.
    if ! python3 -B -c "open('$tf_sg_p', encoding='utf-8').read()" >/dev/null 2>&1; then
      sg_ok=0
      printf 'DIVERGE  %-42s port approval file is not valid UTF-8\n' "surrogate PATH byte"
    fi
  else
    sg_ok=0
    printf 'DIVERGE  %-42s port wrote no approval file for G1\n' "surrogate PATH byte"
  fi
fi
if ! _token_for_gate "$APPR_SG_O" G1 >/dev/null; then
  sg_ok=0
  printf 'DIVERGE  %-42s oracle wrote no approval file for G1 (env setup, not the port, is broken)\n' "surrogate PATH byte"
fi
if [ "$sg_ok" = 1 ]; then pass=$((pass + 1)); else fail=$((fail + 1)); FAILED+=("surrogate PATH byte"); fi

printf '\n  %d pass  %d fail  %d crash\n' "$pass" "$fail" "$crash"
if [ "$crash" -gt 0 ] || [ "$fail" -gt 0 ]; then
  [ "${#FAILED[@]}" -gt 0 ] && printf '  failed: %s\n' "${FAILED[*]}"
  exit 1
fi
echo "  approval classification loop agrees: same tokens, same bodies, same idempotence -- and the control proved the comparison can fail."
