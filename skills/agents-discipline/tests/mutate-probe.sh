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
restore() {
  # IDEMPOTENT. The EXIT trap still runs after the INT/TERM handler, so restore() is called
  # TWICE on a signal -- and the first call removes the backup. Without this guard the second
  # cp failed with "cannot stat" and printed "may still be MUTATED" about a file it had just
  # correctly restored: a false alarm introduced by the cleanup and the signal handler landing
  # in the same edit. A missing backup means the restore already happened.
  [ -f "$backup" ] || return 0
  if cp "$backup" "$target"; then rm -f "$backup"
  else echo "RESTORE FAILED -- $target may still be MUTATED; original at $backup" >&2; fi
}
trap restore EXIT
trap 'restore; echo "$label -> INTERRUPTED (no verdict; file restored)" >&2; exit 130' INT TERM

# Guard 0. A pre-existing divergence makes every subsequent probe a false positive, so the
# harness must establish its own baseline rather than assume one.
if ! "$@" >/tmp/mutate-baseline.out 2>&1; then
  echo "$label -> PROBE FAILED (runner was ALREADY failing before the mutation)"
  tail -3 /tmp/mutate-baseline.out
  exit 1
fi
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
# VARIANTS, not rows. These runners print one DIVERGE line per tree, and a corpus row is
# identical in every tree -- so ONE divergence in a shared row reports as nine. Calling them
# rows is how "reddens 9" got quoted forward as nine independent catches.
diverged=$(printf '%s\n' "$output" | grep -cE '^DIVERGE')
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
