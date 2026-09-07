#!/usr/bin/env bash
# Differential for the STALE branch of the post-run ledger rewrite: gate-check.mjs:826-840 vs
# gate_check.py:1266-1282. Under a per-file lock each runtime RE-READS the ledger it just ran
# against and compares the fresh gate's definition digest + approval-oracle signature to the
# ones captured before the CHECK ran. A mismatch means the ledger changed underneath the run,
# so the result is DISCARDED rather than written.
#
# WHY THIS FILE EXISTS, stated precisely, because a previous fixture claimed to cover this and
# provably could not:
#
#   Editing a gate's CHECK *before* the run does NOT reach this branch. The run then reads the
#   already-edited ledger, computes `definitionDigest` FROM that text, and the lock-time re-read
#   matches it -- staleResults stays EMPTY and the whole branch is skipped. What that fixture
#   actually exercised was an ordinary unmet gate. It was accepted as stale coverage because the
#   substring "stale" appeared once in the output; MEASURED, the oracle emits that substring from
#   three unrelated places -- the `stale-unmet` state literal (gate-check.mjs:842), the "evidence
#   is stale or unbound" report line (:888), and this branch's own message -- so a substring count
#   cannot tell them apart. This file greps for the whole message instead.
#
# THE TRIGGER IS DETERMINISTIC, not a race. The CHECK command is a script inside the root that
# REWRITES ITS OWN LEDGER and then succeeds. By the time the runtime takes the lock the file on
# disk differs from the one it hashed, every time, with no sleep and no background writer. A
# timing-based fixture would test the scheduler as much as the code.
#
# SAME DIRECTORY FOR BOTH RUNTIMES, restored in between. The approval token binds the resolved
# CWD (gate-check.mjs:498) and the run prints `cwd=` and the APPROVED path, so two separate temp
# roots diverge on the path text alone and the diff says nothing about the branch.
#
# NON-VACUITY IS ASSERTED, NOT ASSUMED: CASE 2 runs the identical fixture with a CHECK that does
# NOT touch the ledger and requires the STALE message to be ABSENT. Without it, a build in which
# that line were printed unconditionally would pass CASE 1 while proving nothing.
#
# STILL UNEXERCISED, named rather than left to be discovered later: the `(stale result discarded)`
# label at gate-check.mjs:925 / gate_check.py:1374. It needs staleResults non-empty AND the final
# reloaded gate to read `met` -- i.e. a concurrent writer that lands VALID evidence for the new
# definition while this run is in flight. That is a genuine race, not a scripted mutation, so it
# is left out rather than approximated with a flaky one.
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
APPROVALS="$(mktemp -d)"   # MUST be outside the root: gate-check refuses an in-root approval
                           # dir with exit 2, which would make every case below "identical" on
                           # a usage error and test nothing.
export AGENTS_DISCIPLINE_APPROVAL_DIR="$APPROVALS"
trap 'rm -rf "$WORK" "$APPROVALS"' EXIT

pass=0; fail=0
declare -a FAILED=()

STALE_MSG="definition or runtime approval oracle changed"

# The ledger, and the CHECK script it names. The gate is written UNCHECKED so the run has to
# execute it; --approve records the approval in APPROVALS on the first pass.
_write_fixture() {
  local root="$1" mode="$2"
  mkdir -p "$root/.agents-discipline/s/gates"
  case "$mode" in
    mutating)
      # Rewrites the ledger it was launched from, then succeeds. The EXPECT still matches, so
      # the result is a PASS that the lock-time re-read must then refuse to write.
      printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: ./check.sh\n  EXPECT: ok\n' \
        > "$root/.agents-discipline/s/gates/leaf.md"
      printf '#!/bin/sh\nprintf "%s" > .agents-discipline/s/gates/leaf.md\necho ok\n' \
        '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: ./other.sh\n  EXPECT: ok\n' \
        > "$root/check.sh"
      ;;
    cwd-respelled)
      # ISOLATES THE DEFINITION-DIGEST DISJUNCT, which CASE 1 provably does not.
      #
      # The branch is a three-way OR: gate vanished, definition digest changed, approval-oracle
      # signature changed. MEASURED with mutate-probe.sh: disabling ONLY the digest comparison
      # left CASE 1 GREEN -- because the oracle signature hashes `check` and `expect` too, so
      # any edit to those trips both disjuncts and the digest one is never load-bearing there.
      #
      # The two hash sets are not nested, though, and the gap is CWD's SPELLING:
      #   gateDefinitionDigest (gates.mjs:445-455) hashes the RAW `gate.cwd` string
      #   oracle()             (gate-check.mjs:340-356) hashes resolvedGateCwd(gate, file)
      # so `.` and `./` are DIFFERENT to the digest and IDENTICAL to the signature. Respelling
      # CWD moves exactly one disjunct, which is what makes this case a pin rather than a
      # second copy of CASE 1.
      printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: ./check.sh\n  EXPECT: ok\n  CWD: .\n' \
        > "$root/.agents-discipline/s/gates/leaf.md"
      printf '#!/bin/sh\nprintf "%s" > .agents-discipline/s/gates/leaf.md\necho ok\n' \
        '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: ./check.sh\n  EXPECT: ok\n  CWD: ./\n' \
        > "$root/check.sh"
      ;;
    stable)
      printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: ./check.sh\n  EXPECT: ok\n' \
        > "$root/.agents-discipline/s/gates/leaf.md"
      printf '#!/bin/sh\necho ok\n' > "$root/check.sh"
      ;;
    *) echo "unknown fixture mode: $mode" >&2; exit 2 ;;
  esac
  chmod +x "$root/check.sh"
}

# Replaces every occurrence of the run-specific absolute paths so two runs compare as text.
_scrub() { sed -e "s|$1|<ROOT>|g" -e "s|$APPROVALS|<APPROVALS>|g"; }

# Reset to the state mktemp -d leaves: an EMPTY 0700 directory. `mkdir -p` alone applies the
# umask (0755 here), and gate-check REFUSES an approval dir that grants group or other
# permissions -- which fails the run at exit 2 with "infrastructure failure prevented 1
# approval(s)", i.e. before any gate executes and long before the branch under test. MEASURED:
# the first version of this file did exactly that and reported the STALE message as "absent",
# a verdict indistinguishable from the branch being broken.
_reset_approvals() { rm -rf "$APPROVALS"; mkdir -p "$APPROVALS"; chmod 700 "$APPROVALS"; }

# Streams are captured SEPARATELY, never merged with 2>&1. Merging makes the comparison
# sensitive to each runtime's flush order rather than to what it printed: node and python
# interleave the same two lines differently, and the resulting diff accuses the port of a
# defect it does not have.
_run() {  # interpreter script root outvar errvar codevar
  local interpreter="$1" script="$2" root="$3"
  local tmp_out="$WORK/.out" tmp_err="$WORK/.err" code
  "$interpreter" "$script" --root "$root" --scope s --approve --reverify \
    > "$tmp_out" 2> "$tmp_err"
  code=$?
  printf -v "$4" '%s' "$(_scrub "$root" < "$tmp_out")"
  printf -v "$5" '%s' "$(_scrub "$root" < "$tmp_err")"
  printf -v "$6" '%s' "$code"
}

# One case = build the fixture, run the ORACLE, snapshot its transcript and its ledger, rebuild
# the SAME directory from scratch, run the PORT, compare both artifacts.
_case() {
  local label="$1" mutating="$2" want="$3"   # want: present|absent
  local root="$WORK/$4"
  local ledger="$root/.agents-discipline/s/gates/leaf.md"

  rm -rf "$root"; mkdir -p "$root"; _reset_approvals
  _write_fixture "$root" "$mutating"
  local o_out o_err o_code o_ledger
  _run "$NODE_ABS" "$ORACLE" "$root" o_out o_err o_code
  o_ledger="$(cat "$ledger")"

  rm -rf "$root"; mkdir -p "$root"; _reset_approvals
  _write_fixture "$root" "$mutating"
  local p_out p_err p_code p_ledger
  _run "$PY_ABS" "$PORT" "$root" p_out p_err p_code
  p_ledger="$(cat "$ledger")"

  # The branch must have been reached (or provably not, for the control) IN THE ORACLE, before
  # any agreement between the two is worth reading.
  local seen=absent
  printf '%s' "$o_out" | grep -qF "$STALE_MSG" && seen=present
  if [ "$seen" != "$want" ]; then
    # Print what the oracle actually said. A bare "absent, expected present" is the same
    # opaque verdict this whole file exists to stop trusting -- it cannot distinguish "the
    # branch did not fire" from "the fixture never reached the run".
    printf 'DIVERGE  %-34s oracle STALE message %s, expected %s\n' "$label" "$seen" "$want"
    printf '%s\n' "$o_out" | grep -vE 'PATH' | sed 's/^/    | /' | tail -8
    fail=$((fail + 1)); FAILED+=("$label"); return
  fi

  if [ "$o_out" = "$p_out" ] && [ "$o_err" = "$p_err" ] && [ "$o_code" = "$p_code" ] \
     && [ "$o_ledger" = "$p_ledger" ]; then
    printf '  OK      %-34s STALE %s; exit %s, stdout, stderr and rewritten ledger identical\n' \
      "$label" "$want" "$o_code"
    pass=$((pass + 1)); return
  fi
  printf 'DIVERGE  %-34s exit oracle=%s port=%s\n' "$label" "$o_code" "$p_code"
  [ "$o_out" != "$p_out" ] && diff <(printf '%s\n' "$o_out") <(printf '%s\n' "$p_out")
  [ "$o_err" != "$p_err" ] && diff <(printf '%s\n' "$o_err") <(printf '%s\n' "$p_err")
  [ "$o_ledger" != "$p_ledger" ] && diff <(printf '%s\n' "$o_ledger") <(printf '%s\n' "$p_ledger")
  fail=$((fail + 1)); FAILED+=("$label")
}

_case "self-mutating CHECK is STALE"   mutating       present c1
_case "CONTROL non-mutating CHECK"     stable         absent  c2
_case "CWD respelled pins the digest"  cwd-respelled  present c3

echo
if [ "$fail" = 0 ]; then
  echo "--- all identical ($pass cases) ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
