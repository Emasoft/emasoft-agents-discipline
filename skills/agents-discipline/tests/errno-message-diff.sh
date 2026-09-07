#!/usr/bin/env bash
# Differential for the ERRNO MESSAGE SHAPE -- node's `error.message` for a failed fs call, which
# is not Python's str().
#
#     oracle  EACCES: permission denied, open '<path>'
#     port    [Errno 13] Permission denied: '<path>'
#
# WHY A RUNNER AND NOT ANOTHER ONE-OFF PROBE. This class has now been found FOUR separate times --
# gate-lint (9071a84), ledger_check.py:96, dispatch.py read_state, and gates.py's own scan site --
# each by hand, each months of commits apart, because no runner reddens when one regresses. The
# read_state instance is the sharpest: its comment DESCRIBED the divergence correctly and
# declared it out of scope, and nothing ever re-read that deferral. A row is the thing that
# re-reads a deferral for you.
#
# EVERY SUITE THAT ALREADY EXISTS PASSES BOTH BEFORE AND AFTER EACH OF THOSE FIXES, which is the
# evidence that none of them covers this: lint-tests for gate-lint, ledger-tests for ledger-check,
# dispatch-tests for read_state. The message is user-facing text on an error path, and error-path
# text is exactly what a behavioural suite does not assert.
#
# EACH ROW DRIVES A DIFFERENT CLI, because the fix is per-call-site and has three times been
# applied to one site while an identical sibling kept the defect.
#
# SIX ROWS ARE FIVE SITES AND TWO ERRNO CODES, not six independent proofs -- rows 2 and 5 share
# gate_lint.py:248 and differ only in the code they deliver to it (EACCES, ENOTDIR). Both axes
# matter and neither subsumes the other: the site axis catches a guard applied to one of two
# adjacent lines, the code axis catches a prose mismatch between os.strerror() and libuv's table.
# Counted honestly here because the same conflation already inflated a "6 of ~13 sites" figure
# once, from a line-counting grep that over- and under-counts in both directions.
#
# WHAT THE MUTATIONS PROVE, and it took two kinds to cover it:
#   DELETING a guard (-> a bare str()) reddens the row, which proves the row notices the guard's
#     ABSENCE. Run per site: each reddens exactly its own row(s) and nothing else.
#   PASSING THE WRONG SYSCALL (node_fs_message(error, "stat")) also reddens it -- and THAT is the
#     failure this sweep actually risks, a one-word difference inside an otherwise perfect
#     message. Three clauses, because an earlier version of this note overstated the gap:
#       (1) the comparison is `[ "$o" = "$p" ]` over full output, so ANY unscrubbed difference
#           reddens the row -- a one-word change included;
#       (2) the delete-mutation proves the output reaches that comparison at all;
#       (3) the wrong-syscall mutation adds ONE thing: that no SCRUB erases the syscall token
#           before the comparison sees it. That is the only way (1) could fail.
#     So it is a test of the SCRUBS, run once per scrub rather than once per site:
#         gate_lint.py:248 under _scrub       -> both its rows red
#         gate_check.py:1357 under _scrub_tmp -> that row red
#     The second is the one that mattered, _scrub_tmp rewriting the tail of the very path the
#     syscall word introduces. THE PROPERTY A NEW SCRUB MUST PRESERVE, stated so it can be
#     checked rather than inherited: no scrub's pattern may overlap the syscall token.
#
# DIVERGE LINES START AT COLUMN 0 (see gate-args-diff.sh's note); every other verdict word keeps
# a 2-space indent so mutate-probe.sh's anchored `^DIVERGE` grep stays the only trigger.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
PY_ABS="$(command -v python3)"
NODE_ABS="$(command -v node)"

WORK="$(mktemp -d)"
[ -n "$WORK" ] && [ -d "$WORK" ] || { echo "mktemp -d failed; refusing to build fixtures" >&2; exit 2; }
# chmod 000 files are created below; make them removable again whatever the exit path.
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

pass=0; fail=0
declare -a FAILED=()

# EACCES, NOT ENOENT, for every row. ENOENT is the one shape where several wrong spellings happen
# to look right -- gate-lint's own fix was first measured on a missing file, and a missing file is
# also the shape most likely to be special-cased upstream into a friendlier message (read_state
# returns an empty state on ENOENT; gate-check prints "does not exist"). An unreadable file
# reaches the generic branch in every CLI, which is the branch this file is about.
#
# WHICH MAKES uid 0 A CORRECTNESS PROBLEM, not a portability nit: root bypasses the permission
# bits, so under a root container -- which is what many CI images are -- every chmod 000 file
# opens fine, no row reaches its errno branch, and all four fail their non-vacuity gate at once.
# That reads as four divergences when the truth is "this technique does not work here". Refused
# up front with its own exit code and its own sentence, so the next person sees the cause instead
# of debugging four phantom failures. It is not skipped: skipping would let the class regress
# unnoticed on exactly the machine that runs the suite most often, and this file exists because
# five instances of one defect each survived by being unwatched.
# PROBED BEHAVIOURALLY, not `id -u`. Root is the common reason chmod 000 stops denying, but it is
# not the only one -- an ACL, a permissive mount (many CI volume mounts), a filesystem without
# POSIX bits at all. A uid test answers a PROXY question; opening the file answers the real one,
# and costs the same. EXIT 2, not 1: "this check cannot run here" is a different verdict from
# "the runtimes differ", and the mktemp guard above already uses 2 for exactly that.
_probe="$WORK/.denial-probe"
printf x > "$_probe"; chmod 000 "$_probe"
if cat "$_probe" >/dev/null 2>&1; then
  echo "errno-message-diff: chmod 000 does not deny reads here (running as root?), so every row" >&2
  echo "  below would be vacuous rather than passing. Re-run as an unprivileged user." >&2
  chmod 644 "$_probe"; exit 2
fi
chmod 644 "$_probe"
_unreadable() {  # path -> creates it, unreadable
  printf '%s' "${2:-x}" > "$1"
  chmod 000 "$1"
}

# ROOT SCRUBBED BUT NOT CANONICALISED AWAY: /private/var and /var are the same directory under two
# spellings on macOS, and mapping both to one token would hide a lexical-vs-realpath divergence
# (encoding-diff.sh records the incident). Normalise the spelling, then substitute.
_scrub() { sed -e 's|/private/var/|/var/|g' -e "s|$1|<ROOT>|g"; }

# THE TEMP SCRUB IS ROW 6's ALONE, and that scoping is the point. write_atomic names its temporary
# `<target>.<pid>.<16 hex>.tmp`, so the two runtimes cannot produce equal text on a write failure
# -- different pids, different random bytes -- and row 6 would fail for a reason unrelated to what
# it measures without this.
# APPLIED GLOBALLY IN THE FIRST DRAFT, which is the /private failure's exact shape: a rule added
# to remove noise from ONE row, running on all of them, able to eat signal in a row nobody was
# thinking about. `.[0-9]+.[0-9a-f]+.tmp` also matches plain decimals (0-9 is inside 0-9a-f), so a
# future fixture naming a file `data.12.34.tmp` would have its differences normalised away
# silently. Inert across today's six rows -- and "inert today" is what the /private scrub was too.
_scrub_tmp() {
  _scrub "$1" | sed -e 's/\.[0-9][0-9]*\.[0-9a-f][0-9a-f]*\.tmp/.<TMP>/g'
}

# ROW 8's ALONE, for the same reason and a different name shape: a lock file is
# `<24 hex>.filelock`, and the hex is a digest of the RESOLVED scope path -- so two per-runtime
# roots give two different names, guaranteed, with no bearing on either implementation. Scoped
# rather than folded into _scrub, per the finding that a scrub added for one row and applied to
# all of them is how the /private rule came to hide a real divergence class.
_scrub_lock() {
  _scrub "$1" | sed -e 's|/[0-9a-f][0-9a-f]*\.filelock|/<LOCK>.filelock|g'
}

# ROW 9's ALONE, and it needs TWO substitutions the others do not. The approval directory cannot
# live inside the root -- gate-check refuses that, measured -- so the two runtimes use sibling
# dirs whose names differ, and the file inside is named by a digest that BINDS THE RESOLVED
# LEDGER PATH, which differs for the same reason. Both are per-runtime by construction and say
# nothing about either implementation. The `.lock` suffix is write_atomic's, handled here rather
# than by chaining _scrub_tmp so this row's scrub reads as one thing.
# TIED TO ROW 9's FIXTURE NAMES, not to the concept its name suggests. A second approval row
# using different directory names gets a silent no-op from this, and reports the unscrubbed
# per-runtime path as a divergence that is not one. It needs its own scrub, not this one.
# ROW 12's ALONE, per the scoping rule above. THE PROPERTY EVERY NEW SCRUB MUST BE CHECKED
# AGAINST, and it is checked here rather than assumed: no scrub's pattern may overlap the syscall
# token. `/mk-[op]/` requires a leading slash, a literal `-`, and a trailing slash; the token
# `mkdir` has none of those, so the wrong-syscall mutation stays visible through this scrub.
_scrub_mkdir() { _scrub "$1" | sed -e 's|/mk-[op]/|/<MKDIR>/|g'; }
_scrub_approval() {
  _scrub "$1" \
    | sed -e 's|/appr-dir-[op]/|/<APPRDIR>/|g' \
          -e 's|/[0-9a-f]\{64\}\.json|/<DIGEST>.json|g'
}

_row() {  # label  oracle-root  port-root  required-substring  [scrub-fn]
  local label="$1" o p needle="$4" scrub="${5:-_scrub}"
  # THE SCRUB NAME IS CHECKED BEFORE IT IS CALLED, because a misspelled one degrades into a
  # confusing verdict rather than a clear one. MEASURED: an unknown name gives exit 127 and EMPTY
  # output, so both sides come back empty. The non-vacuity gate below then fires -- so the row
  # does fail, loudly, which is the important half -- but it reports "fixture reached nothing",
  # sending the reader to look at the FIXTURE when the fault is in the caller's fifth argument.
  # This parameter was added in the same commit that scoped the temp scrub; a new indirection is
  # exactly when to ask what its typo looks like.
  # PER ROW, AFTER the fixtures, ON PURPOSE: this guards an AUTHORING error, hit on the first run
  # after the typo at a cost of milliseconds. Hoisting it would mean restructuring the rows into
  # a data table they are not. It also covers the DEFAULT path -- `${5:-_scrub}` substitutes the
  # literal name, so a renamed _scrub is caught too, which is the likelier accident.
  # WHAT IT CANNOT CATCH, and no cheap check can: passing _scrub where _scrub_tmp was meant, or
  # the reverse. Both names resolve; the comparison silently changes. That is the live residual.
  # `declare -F`, not `command -v`: the question is "is this a shell FUNCTION", and command -v
  # would also accept an external program that happens to share the name -- which would then be
  # invoked with a root as argv and the stream on stdin, producing plausible garbage instead of
  # a refusal.
  if ! declare -F "$scrub" >/dev/null 2>&1; then
    echo "errno-message-diff: no such scrub function '$scrub' for row '$label'" >&2
    exit 2
  fi
  o="$("$scrub" "$2" < "$WORK/.o")"; p="$("$scrub" "$3" < "$WORK/.p")"
  # NON-VACUITY: the oracle must have produced the message this row is about. Without it a
  # fixture that failed earlier (a bad flag, a missing arg) leaves both sides equal on some other
  # error and reports agreement about a branch neither reached.
  if ! printf '%s' "$o" | grep -q "$needle"; then
    printf 'DIVERGE  %-34s oracle did not reach the errno branch; fixture reached nothing\n' "$label"
    printf '%s\n' "$o" | sed 's/^/    | /'
    fail=$((fail + 1)); FAILED+=("$label")
  elif [ "$o" = "$p" ]; then
    printf '  OK      %-34s node shape reproduced\n' "$label"
    pass=$((pass + 1))
  else
    printf 'DIVERGE  %-34s\n' "$label"
    diff <(printf '%s\n' "$o") <(printf '%s\n' "$p")
    fail=$((fail + 1)); FAILED+=("$label")
  fi
}

# --- ROW 1: dispatch-check, through read_state's wrap ----------------------------------------
# The message arrives wrapped ("invalid dispatch state: " + the node shape), which is itself part
# of what is compared: read_state re-raises with a prefix, and a port that fixed the inner text
# while dropping the prefix would look right in a hand probe and wrong here.
for rt in o p; do
  R="$WORK/disp-$rt"; mkdir -p "$R/.agents-discipline/s"
  _unreadable "$R/.agents-discipline/s/dispatch.json" '{}'
done
"$NODE_ABS" "$HERE/../scripts/dispatch-check.mjs" open --root "$WORK/disp-o" --scope s --wave w1 --leaf a \
  > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/dispatch_check.py" open --root "$WORK/disp-p" --scope s --wave w1 --leaf a \
  > "$WORK/.p" 2>&1
_row "dispatch-check unreadable state" "$WORK/disp-o" "$WORK/disp-p" "invalid dispatch state"

# --- ROW 2: gate-lint, the site 9071a84 fixed ------------------------------------------------
for rt in o p; do
  R="$WORK/lint-$rt"; mkdir -p "$R"
  _unreadable "$R/gates.md" '# Gates'
done
"$NODE_ABS" "$HERE/../scripts/gate-lint.mjs" "$WORK/lint-o/gates.md" > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/gate_lint.py" "$WORK/lint-p/gates.md" > "$WORK/.p" 2>&1
_row "gate-lint unreadable ledger" "$WORK/lint-o" "$WORK/lint-p" "cannot read"

# --- ROW 3: ledger-check, the sibling site no sweep had reached -------------------------------
for rt in o p; do
  R="$WORK/led-$rt"; mkdir -p "$R"
  _unreadable "$R/DELEGATION.md" '| # |'
done
"$NODE_ABS" "$HERE/../scripts/ledger-check.mjs" "$WORK/led-o/DELEGATION.md" > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/ledger_check.py" "$WORK/led-p/DELEGATION.md" > "$WORK/.p" 2>&1
_row "ledger-check unreadable ledger" "$WORK/led-o" "$WORK/led-p" "cannot read"

# --- ROW 4: gate-check, where the SWEEP'S REMAINING SITES LIVE --------------------------------
# Included before those sites are fixed, deliberately: this row is what tells the next pass
# whether gate-check's own `cannot read` already matches or is the next defect. If it is green
# today that is information, not a wasted row -- gate_check.py:478 has its own spelling and the
# only way to know which it is, is to compare.
for rt in o p; do
  R="$WORK/gc-$rt"; mkdir -p "$R/.agents-discipline/s/gates"
  _unreadable "$R/.agents-discipline/s/gates/leaf.md" '# Gates'
done
"$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/gc-o" --scope s --status > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/gc-p" --scope s --status > "$WORK/.p" 2>&1
# The needle names the BRANCH, like every other row. It was "." -- match any non-empty output --
# in the first draft, which disabled the non-vacuity gate on the one row that found a defect: a
# usage error, a bad flag or a missing scope would have satisfied it, and two runtimes agreeing on
# some OTHER error would have reported OK about a branch neither reached.
_row "gate-check unreadable gate file" "$WORK/gc-o" "$WORK/gc-p" "cannot inspect gate file"

# --- ROW 5: a SECOND errno code, because the four above only ever exercise EACCES -------------
# The rows above prove the message SHAPE. They cannot show a divergence in the PROSE, which
# node_fs_message produces with `os.strerror(n).lower()` against libuv's own table -- two
# independent strings that happen to agree for the codes measured, with no guarantee they agree
# for the next one. Its docstring already names ELOOP as a live counterexample on macOS.
#
# ELOOP CANNOT SERVE AS THAT ROW, measured: read_stable_regular_file catches ELOOP explicitly and
# converts it to its own "must be one unchanged regular single-link file" message, so it never
# reaches the helper at all -- both runtimes print the authored text, identically, and the
# counterexample the docstring warns about is UNREACHABLE from these call sites. Worth knowing:
# the helper's riskiest code is one no CLI can deliver to it.
# ENOTDIR does reach it, via a path whose parent is a regular file, and it is a different code
# with different prose ("not a directory") from a different strerror entry.
mkdir -p "$WORK/nd-o" "$WORK/nd-p"
printf 'x' > "$WORK/nd-o/afile"; printf 'x' > "$WORK/nd-p/afile"
"$NODE_ABS" "$HERE/../scripts/gate-lint.mjs" "$WORK/nd-o/afile/child.md" > "$WORK/.o" 2>&1
"$PY_ABS" "$HERE/../scripts/gate_lint.py" "$WORK/nd-p/afile/child.md" > "$WORK/.p" 2>&1
_row "gate-lint ENOTDIR prose" "$WORK/nd-o" "$WORK/nd-p" "ENOTDIR"

# --- ROW 6: a WRITE, where every other row is a read ------------------------------------------
# The five rows above all fail at an OPEN FOR READING, so they cannot show whether the hardcoded
# `open` generalises past that. This one fails while WRITING -- an unwritable gates directory, so
# write_atomic cannot create its temp file. MEASURED: node still says `open`, because the failing
# call IS an open, of the temp rather than the target. That is why the row exists and also why it
# needs the temp scrub above.
for rt in o p; do
  R="$WORK/wr-$rt"; mkdir -p "$R/.agents-discipline/s/gates"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: echo ok\n  EXPECT: ok\n' \
    > "$R/.agents-discipline/s/gates/leaf.md"
  chmod 500 "$R/.agents-discipline/s/gates"
done
# STDERR ONLY, and NOT `2>&1`. The first draft merged them and the row failed on ORDERING: the
# same "cannot update" line appeared at position 11 in the oracle and position 1 in the port,
# because node and CPython flush the two streams in different orders. That is verbatim the defect
# encoding-diff.sh's own comment warns about -- "a merge diffs flush order rather than content" --
# repeated in a new file by the person who wrote the warning. This message goes to stderr in both
# runtimes (console.error / the port's error()), so stderr alone is the comparison, and the
# stdout report is not what this row is about.
#
# AND AN APPROVAL DIR INSIDE THE FIXTURE. `--approve` writes a token keyed by the RESOLVED ledger
# path, so two per-runtime roots produce two different digests -- a guaranteed diff that says
# nothing about either implementation. Pointing both at their own fixture dir also keeps the row
# from writing into the user's real ~/.agents-discipline/approved/, which a test must not do.
AGENTS_DISCIPLINE_APPROVAL_DIR="$WORK/approvals-o" \
  "$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/wr-o" --scope s --approve \
  > /dev/null 2> "$WORK/.o"
AGENTS_DISCIPLINE_APPROVAL_DIR="$WORK/approvals-p" \
  "$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/wr-p" --scope s --approve \
  > /dev/null 2> "$WORK/.p"
chmod 700 "$WORK/wr-o/.agents-discipline/s/gates" "$WORK/wr-p/.agents-discipline/s/gates"
_row "gate-check unwritable ledger dir" "$WORK/wr-o" "$WORK/wr-p" "cannot update" _scrub_tmp

# --- ROW 7: the status append, written BEFORE its site was touched ---------------------------
# ROW FIRST, FIX SECOND, and the reason is evidential rather than stylistic: a row written before
# the fix is verified against a defect that ACTUALLY EXISTS, so its red-then-green transition
# proves it detects the real shape. A row written after the fix and then mutation-tested proves
# only that it detects a shape I planted -- and this suite has already had one commit where the
# planted shape was cruder than the real risk.
# An unwritable scope directory is the vector: append_status opens the log for appending inside
# it, so the open fails while every earlier step succeeds.
for rt in o p; do
  R="$WORK/log-$rt"; mkdir -p "$R/.agents-discipline/s"
  chmod 500 "$R/.agents-discipline/s"
done
"$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/log-o" --scope s --log 'note' \
  > /dev/null 2> "$WORK/.o"
"$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/log-p" --scope s --log 'note' \
  > /dev/null 2> "$WORK/.p"
chmod 700 "$WORK/log-o/.agents-discipline/s" "$WORK/log-p/.agents-discipline/s"
_row "gate-check unwritable status log" "$WORK/log-o" "$WORK/log-p" "cannot append status"

# --- ROW 8: claim leases, also written before its site was touched ----------------------------
# An unwritable LOCK directory: claim_leases writes a .lease file into it, so the open fails
# after the ledger has been read and parsed. A different helper from every row above.
for rt in o p; do
  R="$WORK/lease-$rt"; mkdir -p "$R/.agents-discipline/s/gates" "$R/.agents-discipline/locks"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: echo ok\n  EXPECT: ok\n' \
    > "$R/.agents-discipline/s/gates/leaf.md"
  # `locks/`, NOT `<scope>/leases/`. The first draft guessed the latter, the claim SUCCEEDED,
  # and the non-vacuity gate caught it -- "fixture reached nothing" on an empty oracle stderr,
  # which is the gate doing its job on a row that would otherwise have compared two successes.
  chmod 500 "$R/.agents-discipline/locks"
done
"$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/lease-o" --scope s --claim \
  > /dev/null 2> "$WORK/.o"
"$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/lease-p" --scope s --claim \
  > /dev/null 2> "$WORK/.p"
chmod 700 "$WORK/lease-o/.agents-discipline/locks" "$WORK/lease-p/.agents-discipline/locks"
_row "gate-check unwritable lease dir" "$WORK/lease-o" "$WORK/lease-p" "cannot claim leases" \
  _scrub_lock

# --- ROW 9: recording an approval, the last pair of sites in this sweep ----------------------
# An unwritable APPROVAL directory. Both approval sites (validate at :928, record at :987) catch
# around helpers that open a file under it, so one fixture reaches the second and the first is
# fixed as its structural twin.
for rt in o p; do
  R="$WORK/appr-$rt"; mkdir -p "$R/.agents-discipline/s/gates" "$WORK/appr-dir-$rt"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: echo ok\n  EXPECT: ok\n' \
    > "$R/.agents-discipline/s/gates/leaf.md"
  chmod 500 "$WORK/appr-dir-$rt"
done
AGENTS_DISCIPLINE_APPROVAL_DIR="$WORK/appr-dir-o" \
  "$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/appr-o" --scope s --approve \
  > /dev/null 2> "$WORK/.o"
AGENTS_DISCIPLINE_APPROVAL_DIR="$WORK/appr-dir-p" \
  "$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/appr-p" --scope s --approve \
  > /dev/null 2> "$WORK/.p"
chmod 700 "$WORK/appr-dir-o" "$WORK/appr-dir-p"
_row "gate-check unwritable approval dir" "$WORK/appr-o" "$WORK/appr-p" \
  "could not record approval" _scrub_approval

# --- ROW 10: the `stat` syscall, and the two sites that hand-rolled the node shape -------------
# EVERY ROW ABOVE MEASURES `open`, so none of them would notice a site that needs a DIFFERENT
# syscall getting the wrong one. gate-check's `--root` inspection catches around a stat, and the
# oracle says `stat` there -- so this row is the control for the constant itself, not just for
# the message shape.
# It also guards a REFACTOR: these two sites (:329 and :908) reproduced node_fs_message's output
# by hand -- `name + ": " + prose + ", stat '" + path + "'"` -- and were CORRECT, which is why no
# earlier row found them and why the spelling-grep for `str(exc)` did not list them. A private
# copy of a function whose measured errno scope is documented elsewhere drifts silently when that
# scope changes, so they now call the helper and this row is what says the output did not move.
mkdir -p "$WORK/root-o" "$WORK/root-p"
chmod 000 "$WORK/root-o" "$WORK/root-p"
"$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/root-o/x" --scope s --status \
  > /dev/null 2> "$WORK/.o"
"$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/root-p/x" --scope s --status \
  > /dev/null 2> "$WORK/.p"
chmod 700 "$WORK/root-o" "$WORK/root-p"
# THE NEEDLE IS WHAT SEPARATES THE TWO BRANCHES here, not decoration: `chmod 000` on the parent
# denies SEARCH, so the stat fails at the parent with EACCES before it can learn whether `x`
# exists. If a filesystem ever answered ENOENT instead, the code exits earlier with
# `--root does not exist:` -- which does NOT contain this needle, so the row would report
# "fixture reached nothing" rather than comparing the wrong branch.
_row "gate-check unreadable root (stat)" "$WORK/root-o" "$WORK/root-p" "cannot inspect --root"

# --- ROW 11: `scandir`, the THIRD syscall, and gates.py's only helper call site ---------------
# gates.py:1383 is the one place OUTSIDE the CLIs that calls node_fs_message, and it is in the
# module that DEFINES the helper -- so a defect there is inherited by every site the sweep has
# already fixed. That is why gates.py was pulled forward instead of left for last: deferring the
# module that defines the contract puts the nine dependent fixes on unverified ground.
# An unreadable gates DIRECTORY, so the scan fails rather than any file read. Node names the
# syscall `scandir` for a failed readdir -- a third constant after `open` and `stat`, and this
# row is its only control.
for rt in o p; do
  R="$WORK/scan-$rt"; mkdir -p "$R/.agents-discipline/s/gates"
  chmod 000 "$R/.agents-discipline/s/gates"
done
"$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/scan-o" --scope s --status \
  > /dev/null 2> "$WORK/.o"
"$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/scan-p" --scope s --status \
  > /dev/null 2> "$WORK/.p"
chmod 700 "$WORK/scan-o/.agents-discipline/s/gates" "$WORK/scan-p/.agents-discipline/s/gates"
_row "gates.py unreadable gate dir (scandir)" "$WORK/scan-o" "$WORK/scan-p" \
  "cannot inspect gate directory"

# --- ROW 12: `mkdir`, the FOURTH syscall, and the only row whose fix it caught ----------------
# SAME SITE AS ROW 9, DIFFERENT SYSCALL DELIVERED TO IT -- the site/code split the header
# describes for rows 2 and 5, and here it is the whole point. Row 9 pre-creates the approval
# directory and chmod 000s it, so the failure is the file OPEN inside it. Point
# AGENTS_DISCIPLINE_APPROVAL_DIR at a MISSING child of an unwritable parent instead and the same
# site fails one syscall earlier, at the mkdir that would have created it. Row 9 was green
# throughout; nothing here reached a mkdir until this row existed.
#
# WHAT IT CAUGHT, which is why it is not a formality: gate_check.py used a bare
# `os.makedirs(directory, mode=0o700, exist_ok=True)` where the oracle uses recursive mkdirSync.
# MEASURED before the fix -- node `EACCES: permission denied, mkdir '<dir>'` against the port's
# `... open '<dir>'`, because the catch upstream hardcodes `open`. The same line also applied
# 0o700 to the FINAL component only, leaving an intermediate at the umask default under an
# approval tree; both defects are gone with one call to the shared `mkdirs`.
#
# HONEST ABOUT ITS EVIDENCE, in both directions: the DEFECT was measured directly before the fix
# (the node/python pair above is that measurement), so it was never hypothetical. What was never
# observed is THIS ROW reddening against it -- the row was written afterwards and is proven only
# against a re-planted mutation. That is the weaker form, and the reason to write the row first;
# stated precisely because the previous wording read as though the defect itself were unproven.
for rt in o p; do
  R="$WORK/mkroot-$rt"; mkdir -p "$R/.agents-discipline/s/gates" "$WORK/mk-$rt"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: echo ok\n  EXPECT: ok\n' \
    > "$R/.agents-discipline/s/gates/leaf.md"
  chmod 500 "$WORK/mk-$rt"
done
AGENTS_DISCIPLINE_APPROVAL_DIR="$WORK/mk-o/child" \
  "$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/mkroot-o" --scope s --approve \
  > /dev/null 2> "$WORK/.o"
AGENTS_DISCIPLINE_APPROVAL_DIR="$WORK/mk-p/child" \
  "$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/mkroot-p" --scope s --approve \
  > /dev/null 2> "$WORK/.p"
chmod 700 "$WORK/mk-o" "$WORK/mk-p"
# THE NEEDLE IS THE SYSCALL TOKEN, not the outer prefix, and that choice is load-bearing. This
# very path was MEASURED substituting an authored `must be a real directory` message under a
# different fixture -- and `could not record approval` would still match it, so both runtimes
# would agree on text that says nothing about mkdir and the row would pass while testing nothing.
# Row 10's comment states this rule; the first version of this row broke it.
_row "gate-check unwritable approval PARENT (mkdir)" "$WORK/mkroot-o" "$WORK/mkroot-p" \
  ", mkdir '" _scrub_mkdir

# --- ROW 13: the first row to exercise an OVERRIDDEN PROSE code -------------------------------
# EVERY OTHER ROW DELIVERS EACCES, ENOTDIR OR ENOENT -- the three codes where os.strerror() and
# libuv's table AGREE. So none of them can see _LIBUV_PROSE at all, and the four codes it
# overrides (EEXIST, EISDIR, ELOOP, ENAMETOOLONG) were verified only against hand-built OSErrors
# compared to separately-measured node strings. That is the same "correct but uncovered" shape
# this file exists to close.
# A FILE where the locks DIRECTORY belongs, so _lock_directory's mkdirs hits EEXIST. The prose is
# the whole point: libuv says "file already exists" where os.strerror says "file exists", so a
# regression that dropped the override reddens HERE and nowhere else.
for rt in o p; do
  R="$WORK/lockfile-$rt"; mkdir -p "$R/.agents-discipline/s/gates"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: echo ok\n  EXPECT: ok\n' \
    > "$R/.agents-discipline/s/gates/leaf.md"
  : > "$R/.agents-discipline/locks"
done
"$NODE_ABS" "$HERE/../scripts/gate-check.mjs" --root "$WORK/lockfile-o" --scope s --claim \
  > /dev/null 2> "$WORK/.o"
"$PY_ABS" "$HERE/../scripts/gate_check.py" --root "$WORK/lockfile-p" --scope s --claim \
  > /dev/null 2> "$WORK/.p"
# THE NEEDLE IS THE OVERRIDDEN PROSE ITSELF, for the reason spelled out on row 12: `cannot claim
# leases` is the outer prefix and would survive any authored message replacing the inner clause,
# leaving this row green while proving nothing about _LIBUV_PROSE. `file already exists` is
# precisely the string the override supplies and strerror does not.
_row "gate-check locks path is a file (EEXIST prose)" "$WORK/lockfile-o" "$WORK/lockfile-p" \
  "file already exists"

echo
if [ "$fail" = 0 ]; then
  echo "--- $pass errno message(s) identical ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
