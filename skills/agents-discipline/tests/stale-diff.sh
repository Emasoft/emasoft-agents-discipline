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
assertions=0   # rows that check ONE runtime against itself, counted so the summary can keep
               # them apart from the differentials. A hardcoded literal here goes stale the
               # moment a row is added, and reports a wrong differential count while staying
               # green -- which is the defect class this file is about.
declare -a FAILED=()

STALE_MSG="definition or runtime approval oracle changed"

# THE CASE-3 PAIR, DEFINED ONCE. _write_fixture's `cwd-respelled` arm and _assert_pin both need
# the same CHECK and the same two CWD spellings, and they used to hardcode them separately.
# Nothing tied the copies together: editing the spelling in one would leave the premise row
# certifying a pair CASE 3 no longer tests, while both kept passing -- the very failure the
# premise row exists to prevent, displaced one level up.
C3_CHECK='./check.sh'
C3_CWD_BEFORE='.'
C3_CWD_AFTER='./'
# The ROOT NAME is the fourth copy of the same magic string, and hoisting the other three while
# leaving this one hardcoded in two places would have left the identical desync one level up:
# _assert_pin said "$WORK/c3" and _case computed "$WORK/$4" from a literal at its call site, so
# renaming the case's directory would send the premise rows off to certify a path CASE 3 no
# longer uses -- with all rows still green.
C3_ROOT='c3'

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
      # `resolvedGateCwd` is `resolve(base, gate.cwd)` (gate-check.mjs:332-338) and path.resolve
      # NORMALIZES, so `.` and `./` yield the identical absolute string -- while the digest
      # hashes `String(gate.cwd)` verbatim. DIFFERENT to the digest, IDENTICAL to the signature:
      # respelling CWD moves exactly one disjunct, which is what makes this a pin rather than a
      # second copy of CASE 1.
      #
      # MEASURED, not reasoned. With the digest disjunct disabled in the port, CASES 1 and 2
      # stay GREEN and this one diverges in exactly the shape the claim predicts -- the port
      # WRITES the result the oracle refuses:
      #     -   STALE leaf:G1: definition or runtime approval oracle changed; result not written
      #     - - [ ] G1: x        (oracle)
      #     + - [x] G1: x        (port, plus a fresh EVIDENCE line)
      printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: %s\n  EXPECT: ok\n  CWD: %s\n' \
        "$C3_CHECK" "$C3_CWD_BEFORE" > "$root/.agents-discipline/s/gates/leaf.md"
      printf '#!/bin/sh\nprintf "# Gates\\n\\nOWNS: src/**\\n\\n- [ ] G1: x\\n  CHECK: %s\\n  EXPECT: ok\\n  CWD: %s\\n" > .agents-discipline/s/gates/leaf.md\necho ok\n' \
        "$C3_CHECK" "$C3_CWD_AFTER" > "$root/check.sh"
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

# CASE 3 IS A PIN ONLY WHILE THE TWO CWD SPELLINGS SHARE ONE APPROVAL SIGNATURE, and CASE 3
# cannot see that itself: it asserts the STALE message fired, which stays true even if the
# respelling started tripping BOTH disjuncts -- at which point the case silently stops isolating
# anything and goes on passing. This asserts the premise directly.
#
# The approval token's FILENAME is sha256(resolve(file) + "\0" + gate.id + "\0" +
# approvalOracleSignature(file, gate)) -- gate-check.mjs:372-375. Same file, same gate id, so
# two approvals land on the SAME filename exactly when their oracle signatures are equal.
# Approving both spellings into one directory and counting tokens therefore measures signature
# equality with no access to the hash itself. The control approves a CHECK edit instead, which
# MUST produce 2 -- otherwise a counter stuck at 1 would "prove" the premise for free.
# CHECK and CWD are passed as SEPARATE arguments, never packed into one string split on a
# delimiter. A `${body%%|*}` / `${body##*|}` pair was the first spelling here and it is exactly
# the kind of cleverness that fails silently: a CHECK containing a pipe would mis-split, both
# rows would still produce SOME count, and the control could not tell you the wrong ledgers had
# been measured.
_assert_pin() {
  local exe="$1" script="$2" dir="$3" want="$4" label="$5" check1="$6" cwd1="$7" check2="$8" cwd2="$9"
  # THE SAME ROOT CASE 3 USES. The token hashes resolve(file), so a scratch root would measure
  # signature equality for a DIFFERENT file path and attach the conclusion to c3 by inference.
  # _case rebuilds this directory from scratch before its own run, so there is no interference.
  local root="$WORK/$C3_ROOT" ledger
  rm -rf "$root" "$dir"; mkdir -p "$root/.agents-discipline/s/gates" "$dir"; chmod 700 "$dir"
  printf '#!/bin/sh\necho ok\n' > "$root/check.sh"; chmod +x "$root/check.sh"
  ledger="$root/.agents-discipline/s/gates/leaf.md"
  local i
  for i in 1 2; do
    local c w code
    if [ "$i" = 1 ]; then c="$check1"; w="$cwd1"; else c="$check2"; w="$cwd2"; fi
    printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: %s\n  EXPECT: ok\n  CWD: %s\n' \
      "$c" "$w" > "$ledger"
    AGENTS_DISCIPLINE_APPROVAL_DIR="$dir" \
      "$exe" "$script" --root "$root" --scope s --approve --reverify >/dev/null 2>&1
    code=$?
    # THE INVOCATION'S STATUS IS READ, not discarded. Both gates here PASS, so the only correct
    # exit is 0; a crash on the second call would leave exactly one token behind and the count
    # would read "1, as required" -- a row reporting success for a run that died. The control
    # rows would still catch it, but a check whose command's status is never branched on is the
    # pattern this whole file exists to distrust.
    if [ "$code" != 0 ]; then
      printf 'DIVERGE  %-34s approval run %s exited %s, expected 0\n' "$label" "$i" "$code"
      fail=$((fail + 1)); FAILED+=("$label"); return
    fi
  done
  local n; n=$(find "$dir" -maxdepth 1 -name '*.json' -type f | wc -l | tr -d ' ')
  if [ "$n" = "$want" ]; then
    printf '  OK      %-34s %s approval token(s), as required\n' "$label" "$n"
    # Incremented HERE, beside pass, not on entry. On entry it would count rows that RAN, and
    # `pass - assertions` would then be correct only because a failure takes the other summary
    # branch and never reaches the subtraction -- a property of a different branch, not of the
    # counter. Anyone who later prints one summary line for both outcomes would silently
    # under-report differentials on exactly the runs being debugged.
    pass=$((pass + 1)); assertions=$((assertions + 1)); return
  fi
  printf 'DIVERGE  %-34s %s approval token(s), expected %s\n' "$label" "$n" "$want"
  fail=$((fail + 1)); FAILED+=("$label")
}

# RUN FOR BOTH RUNTIMES. The premise is a property of the code UNDER TEST, not just the oracle:
# if gate_check.py's approval_oracle_signature ever normalized gate.cwd differently, the port
# would mint 2 tokens where the oracle mints 1, CASE 3 would still pass (both print STALE, for
# different reasons), and the pin would be certified on the oracle while being false for the
# port. An oracle-only premise check cannot see that.
#
# WHAT THESE ROWS DO NOT SHOW, stated because the wording invites the stronger reading: each row
# measures respelling-invariance WITHIN one runtime. None compares the two runtimes' token
# filenames, so a port whose signature diverged from the oracle's outright would still mint
# exactly 1 token in its own directory and pass here. That property is covered -- by
# approval-diff.sh, whose whole subject is token agreement across the two -- but it is not
# covered by these four rows.
_assert_pin "$NODE_ABS" "$ORACLE" "$WORK/a1" 1 "PREMISE oracle respelling is sig-equal" \
  "$C3_CHECK" "$C3_CWD_BEFORE" "$C3_CHECK" "$C3_CWD_AFTER"
_assert_pin "$PY_ABS" "$PORT" "$WORK/a2" 1 "PREMISE port respelling is sig-equal" \
  "$C3_CHECK" "$C3_CWD_BEFORE" "$C3_CHECK" "$C3_CWD_AFTER"
_assert_pin "$NODE_ABS" "$ORACLE" "$WORK/a3" 2 "CONTROL a CHECK edit is not" \
  "$C3_CHECK" "$C3_CWD_BEFORE" "$C3_CHECK -q" "$C3_CWD_BEFORE"
_assert_pin "$PY_ABS" "$PORT" "$WORK/a4" 2 "CONTROL port CHECK edit is not" \
  "$C3_CHECK" "$C3_CWD_BEFORE" "$C3_CHECK -q" "$C3_CWD_BEFORE"
_reset_approvals

_case "self-mutating CHECK is STALE"   mutating       present c1
_case "CONTROL non-mutating CHECK"     stable         absent  c2
_case "CWD respelled pins the digest"  cwd-respelled  present c3

echo
if [ "$fail" = 0 ]; then
  # NOT "all identical (N cases)". The rows are two KINDS of evidence and only one of them is a
  # cross-runtime comparison: the PREMISE/CONTROL rows count approval tokens within a SINGLE
  # runtime and compare nothing between them. A single denominator invites the summary to be
  # quoted as N oracle-vs-port comparisons, which over-reads it by four.
  echo "--- $((pass - assertions)) differential(s) identical; $assertions premise/control assertion(s) held ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
