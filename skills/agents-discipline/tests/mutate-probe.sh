#!/bin/bash
# Run ONE mutation probe against a differential runner, and refuse to call anything a catch
# that it has not earned.
#
#   bash tests/mutate-probe.sh <label> <file> <old> <new> <runner...>
#
# PASS THE ANCHORS AS ORDINARY QUOTED ARGUMENTS. Never `"$(eval echo $old)"`: measured, that
# word-splits an unquoted anchor and rejoins it with single spaces, so a 16-space indent
# arrives with NONE. Four probes ran that way and matched only because the stripped string
# happened to still be unique -- and a multi-line anchor's `\n` then depended on whether the
# invoking shell's `echo` interprets escapes (zsh does, bash does not).
#
# WHY THIS FILE EXISTS, in two failures it now prevents:
#   * a probe reported "reddens 9" for a mutant that was an IndentationError. gates.py would
#     not import, the driver exited non-zero on every variant, and CRASH was counted as a
#     catch. The fix it claimed to cover had no coverage at all.
#   * a probe reports REDDENS for ANY anchor when the tree was ALREADY diverging -- which
#     happened twice in one session (a bad splice, and a corpus row inserted at a different
#     position in each driver). Hence the baseline run below.
#
# Guards, each for a failure this harness actually had:
#   0. the runner must be GREEN before mutating   (else every probe "catches" something)
#   1. the anchor must be UNIQUE                  (an earlier probe edited a different function)
#   2. the edit must CHANGE the file              (a stale anchor silently no-ops)
#   3. a .py mutant must still IMPORT             (a syntax error is not a divergence)
# and only DIVERGE counts as reddening -- never CRASH.
set -u
label="$1"; target="$2"; old="$3"; new="$4"; shift 4
# MEASURED: with no runner, `"$@"` is an empty simple command -- status 0, and exempt from
# set -u -- so the baseline "passed", `output` was empty, and the probe printed a confident
# NOTHING REDDENED about code it never executed. A typo that drops the runner must be an
# error, not a negative result.
if [ $# -lt 1 ]; then echo "$label -> PROBE FAILED (no runner given)" >&2; exit 1; fi
backup=$(mktemp)
cp "$target" "$backup"
# PRINTED, because a SIGKILL skips the trap and leaves the mutant in place; without this the
# only copy of the original is at an unknown temp path and recovery needs a gated git checkout.
echo "$label -> backup: $backup" >&2   # stderr: stdout carries the VERDICT
# The restore is CHECKED: an unchecked cp that fails (read-only target, full disk) leaves the
# mutant in the working tree while the script still prints a normal verdict, and the next
# probe's baseline guard would report it as somebody else's problem.
#
# INT/TERM get their OWN handler that EXITS. A bash signal handler that does not exit returns
# control to the script, so Ctrl-C during the runner killed the child, restored the file, and
# then fell through to the verdict block -- printing NOTHING REDDENED about source that was no
# longer mutated. MEASURED: interrupting a probe mid-run produced exactly that, which is this
# harness's own headline failure mode reachable by a keystroke. 130 = 128 + SIGINT.
# The backup is REMOVED once the restore succeeds -- every probe otherwise left a full copy of
# the target in /tmp forever, and dozens accumulated in one session. It is kept, and its path
# reported, ONLY when the restore failed, which is the one case where it is the recovery path
# the startup line advertises.
restored=0
restore() {
  # IDEMPOTENT VIA A FLAG, not via "does the backup still exist". The EXIT trap still runs
  # after the INT/TERM handler, so restore() is called TWICE on a signal, and the first call
  # removes the backup -- the second then failed with "cannot stat" and reported "may still be
  # MUTATED" about a file it had just correctly restored.
  #
  # The obvious guard, `[ -f "$backup" ] || return 0`, INFERS state from a file's existence and
  # conflates two cases: "already restored" and "the backup vanished" (a /tmp reaper, an
  # external rm). In the second the target is STILL MUTATED and the function returns success
  # silently -- the exact false negative this harness exists to prevent, reintroduced by the
  # guard meant to fix a false alarm. A flag records what actually happened.
  [ "$restored" = 1 ] && return 0
  # STDOUT: see interrupted(). A caller's `2>/dev/null` must never be able to hide the one
  # message saying the working tree may still hold a mutant.
  if cp "$backup" "$target"; then restored=1; rm -f "$backup"; return 0
  else echo "RESTORE FAILED -- $target may still be MUTATED; original at $backup"; return 1; fi
}
interrupted() {   # interrupted <signal-number>
  # 128+signal, per convention: SIGINT 2 -> 130, SIGTERM 15 -> 143. Both were hardcoded to 130,
  # and the test that "verified" it printed `TERM exit=130 (143 = handled SIGTERM)` -- the label
  # stating the expected value, the output contradicting it, and the run reported as a success.
  # Nothing consumes these codes today; the defect was reading a disagreement as agreement.
  #
  # The message is CONDITIONAL on the restore, because asserting "file restored" one line after
  # restore() printed "may still be MUTATED" gives a skimmer two contradictory claims with the
  # wrong one last. It cannot branch on restore()'s old return value -- its failure arm ended in
  # a successful echo, so it returned 0 either way; that is what the explicit `return 1` fixes.
  # STDOUT, not stderr. The backup line was moved to stderr so callers could filter it, and
  # every invocation since has been `... 2>/dev/null` -- which then swallowed these two
  # messages as well. The one notice that the run DIED and the one notice that the file may
  # still be MUTATED were sharing a channel with the noise the redirect existed to suppress,
  # so an interrupted probe under the standard invocation printed nothing at all.
  if restore; then echo "$label -> INTERRUPTED (no verdict; file restored)"
  else echo "$label -> INTERRUPTED (no verdict; RESTORE FAILED -- see below)"; fi
  exit $((128 + $1))
}
trap restore EXIT
trap 'interrupted 2' INT
trap 'interrupted 15' TERM

# Guard 0. A pre-existing divergence makes every subsequent probe a false positive, so the
# harness must establish its own baseline rather than assume one.
if ! "$@" >/tmp/mutate-baseline.out 2>&1; then
  echo "$label -> PROBE FAILED (runner was ALREADY failing before the mutation)"
  tail -3 /tmp/mutate-baseline.out
  exit 1
fi
baseline_out=$(cat /tmp/mutate-baseline.out)
# GUARD 0 TRUSTED THE EXIT STATUS, AND THAT IS NOT THE SAME AS "not already diverging".
# regex-worker-diff.sh prints SEVEN DIVERGE lines and deliberately exits 0, because its port
# divergences are known and unresolved -- so guard 0 passed, and EVERY probe run against it
# scored those seven as its own catch. Measured: mutating an unrelated line of jsapi.py (the
# "0x" alone return, which regex-worker-diff cannot reach) reported REDDENS (7 diverging
# variant(s)). A free catch for a mutation with no relationship to the code under test, which
# is precisely the false positive guard 0 exists to prevent.
#
# The fix is to count the baseline's own DIVERGE lines and report the DELTA. Exit status is a
# summary the runner is free to define; the lines are the measurement.
baseline_diverged=$(printf '%s\n' "$baseline_out" | grep -cE '^DIVERGE')
# A success-marker grep ("--- all identical ---") sat here and was REMOVED. Both runners print
# that banner IFF they exit 0, so it was exactly redundant with the check above; its only
# effect that was not redundant was rejecting any OTHER differential runner, and the realistic
# case it shared -- a dropped runner argument -- is caught by the guard at the top.
# NOTE THE REMAINING LIMIT: a baseline that merely exits 0 is not proof the runner can OBSERVE
# anything. A true positive control needs a canary mutation known to diverge, run once per
# batch. That does not exist yet; do not read a NOTHING REDDENED as proof of no coverage
# without one.

if ! OLD="$old" NEW="$new" python3 -B -c '
import os, pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
n = s.count(old)
if n != 1:
    sys.exit("ANCHOR NOT UNIQUE (%d occurrences)" % n)
p.write_text(s.replace(old, new))
' "$target"; then
  echo "$label -> PROBE FAILED (anchor)"; exit 1
fi

if cmp -s "$target" "$backup"; then echo "$label -> PROBE FAILED (edit was a no-op)"; exit 1; fi

# Guard 3, for Python targets only. Mutating a DRIVER is a legitimate probe ("is this row
# actually compared?"), and `import discovery-drive.mjs` is a SyntaxError that would have
# reported PROBE FAILED for a perfectly good edit.
case "$target" in
  *.py)
    if ! PYTHONDONTWRITEBYTECODE=1 python3 -c "
import sys; sys.path.insert(0, '$(dirname "$target")')
import $(basename "$target" .py)" 2>/tmp/mutate-import.err; then
      echo "$label -> PROBE FAILED (mutant does not import): $(tail -1 /tmp/mutate-import.err)"
      exit 1
    fi ;;
esac

output=$("$@" 2>&1)
# THE RUNNER MUST SPEAK THIS SCRIPT'S VOCABULARY, and until this guard existed nothing checked
# it. The verdict below is `grep -cE '^DIVERGE'` over the runner's stdout, so ANY runner that
# reports divergence some other way scores zero and lands in the NOTHING REDDENED branch --
# indistinguishable from a mutation with genuinely no coverage.
#
# MEASURED, not hypothetical: `mutate-probe.sh M1 scripts/lib/jsapi.py <regex> <regex>
# python3 tests/python-lib-checks.py` reported NOTHING REDDENED for a mutation independently
# measured to redden two rows. python-lib-checks prints `FAIL <name>`, never `DIVERGE`. The
# comment above already warned "do not read a NOTHING REDDENED as proof of no coverage without
# a canary" -- a file documenting its own hazard did not stop its author walking into it, which
# is the argument for a guard over a warning.
#
# A first version of this guard grepped the runner's SOURCE for the token, and DID NOT FIRE:
# python-lib-checks.py contains the word DIVERGE once, in a COMMENT. Searching source text for
# a string is the weak-predicate mistake this project keeps making -- a comment is not a
# behaviour. The test has to be over what the runner PRINTS.
if [ "$(printf '%s\n' "$baseline_out" | grep -cE '^(ok|DIVERGE) ')" = 0 ]; then
  echo "$label -> PROBE FAILED (runner printed no 'ok'/'DIVERGE' lines, so the verdict grep"
  echo "    below can only ever say NOTHING REDDENED. Use a *-diff.sh runner.)"
  exit 1
fi
# VARIANTS, not rows. These runners print one DIVERGE line per tree, and a corpus row is
# identical in every tree -- so ONE divergence in a shared row reports as nine. Calling them
# rows is how "reddens 9" got quoted forward as nine independent catches.
# THE DELTA, not the count. See guard 0 above: a runner may carry known divergences and still
# exit 0, and counting absolutely awards those to every probe.
diverged=$(( $(printf '%s\n' "$output" | grep -cE '^DIVERGE') - baseline_diverged ))
[ "$diverged" -lt 0 ] && diverged=0   # a mutation that FIXES a known divergence reddens nothing
crashed=$(printf '%s\n' "$output" | grep -cE '^(CRASH|BUILD-FAILED|VACUOUS)')
# NO exit-code protocol for the verdicts. A 0/1/2/3 scheme was added here and then REMOVED:
# nothing in this repo invokes this script, so the codes had no reader -- and the DIFFERS
# branch shared exit 0 with REDDENS, reinstating the very "distinction lives only in prose"
# flaw the codes were meant to remove. If a batch runner is ever wanted, write THAT and let it
# define the codes. `PROBE FAILED` keeps exit 1, which is useful for `||` today.
#
# Read the verdict from stdout, and do NOT pipe this script through a filter: a pipeline
# returns its LAST command's status, which discards even the PROBE FAILED code.
if [ "$diverged" -gt 0 ]; then
  # A DIFFERS branch for driver targets was DELETED here, by the same reasoning that deleted
  # the exit codes: no probe has ever targeted a driver, so it was a dead branch carrying a
  # caveat nobody had read, behind a glob nobody had exercised (and one that misfires for a
  # repo checked out under a directory named tests/). Applying that reasoning to one unconsumed
  # addition and not the other, in the same file, was the inconsistency.
  # If you DO mutate a driver: DIVERGE then means the two drivers no longer match, NOT that an
  # implementation fix is load-bearing. The harness cannot tell those apart; you must.
  echo "$label -> REDDENS ($diverged diverging variant(s)$([ "$crashed" -gt 0 ] && echo ", $crashed crash(es)"))"
elif [ "$crashed" -gt 0 ]; then
  # A crash is NOT a catch: the mutant broke the harness, so the probe proved nothing.
  echo "$label -> INCONCLUSIVE (only crashes, $crashed) -- the mutant broke the runner"
else
  echo "$label -> NOTHING REDDENED"
fi
