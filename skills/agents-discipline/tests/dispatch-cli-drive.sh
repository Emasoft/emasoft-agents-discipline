#!/bin/bash
# Compare the two dispatch-check CLIs on stdout, stderr and EXIT CODE.
#
# dispatch-tests.mjs asserts behaviour but reaches none of these: terminal_safe at its 500-byte
# budget (its escape and truncation paths are entered only by a control character or an
# over-long message, and every suite message is short ASCII), and the usage/stream routing,
# where the same text goes to stdout on --help and to stderr after an unknown command, with
# exit 2 for no-args and exit 0 for --help.
#
# Usage: bash tests/dispatch-cli-drive.sh   (run from skills/agents-discipline)
set -u
fail=0
run() {                     # run <label> <args...>
  local label="$1"; shift
  local d1 d2 o1 o2 e1 e2
  d1=$(mktemp -d); d2=$(mktemp -d)
  # `; printf X` is a SENTINEL, not decoration: $( ) strips ALL trailing newlines, so without it
  # `console.log(x)` and a port doing `sys.stdout.write(x)` compare EQUAL -- measured, "a\n\n"
  # and "a\n" are indistinguishable through command substitution. This driver's whole claim is a
  # byte comparison of the CLI surface, and that class of difference was invisible to it.
  # `exit $rc` is load-bearing: a bare `cmd; printf X` makes $? the status of PRINTF, so every
  # exit-code row would silently read 0 and the usage/exit rows would stop asserting anything.
  o1=$(node scripts/dispatch-check.mjs "$@" --root "$d1" 2>/tmp/e1; rc=$?; printf X; exit $rc); e1=$?
  o2=$(python3 scripts/dispatch_check.py "$@" --root "$d2" 2>/tmp/e2; rc=$?; printf X; exit $rc); e2=$?
  local s1 s2
  s1="exit=$e1|out=$o1|err=$(sed "s|$d1|<R>|g" /tmp/e1; printf X)"
  s2="exit=$e2|out=$o2|err=$(sed "s|$d2|<R>|g" /tmp/e2; printf X)"
  if [ "$s1" = "$s2" ]; then echo "OK      $label"; else
    fail=1; echo "DIVERGE $label"; echo "  js: ${s1:0:170}"; echo "  py: ${s2:0:170}"
  fi
}
# raw(): NO --root appended. `run` always adds one, so a "no args" row run through it is not a
# no-args invocation at all -- measured, mutating `sys.exit(0 if args else 2)` to `sys.exit(0)`
# changed nothing, because that branch was never reached.
raw() {
  local label="$1"; shift
  local o1 o2 e1 e2 s1 s2
  o1=$(node scripts/dispatch-check.mjs "$@" 2>/tmp/e1; rc=$?; printf X; exit $rc); e1=$?
  o2=$(python3 scripts/dispatch_check.py "$@" 2>/tmp/e2; rc=$?; printf X; exit $rc); e2=$?
  s1="exit=$e1|out=$o1|err=$(cat /tmp/e1; printf X)"
  s2="exit=$e2|out=$o2|err=$(cat /tmp/e2; printf X)"
  if [ "$s1" = "$s2" ]; then echo "OK      $label"; else
    fail=1; echo "DIVERGE $label"; echo "  js: ${s1:0:170}"; echo "  py: ${s2:0:170}"
  fi
}
# Usage and stream routing: stdout for --help (exit 0), stdout+exit 2 for a bare invocation.
raw "no args (exit 2, stdout)"
raw "--help alone (exit 0, stdout)" --help
raw "-h alone" -h
run "--help" --help
run "unknown command" bogus --scope api --wave w1
run "unknown option" open --scope api --wave w1 --nope x
run "missing value" open --scope api --wave
run "value looks like a flag" open --scope api --wave w1 --leaf --root
run "repeated single" open --scope api --scope other --wave w1 --leaf a
run "no scope" open --wave w1 --leaf a
run "open without leaf" open --scope api --wave w1
run "start without handle" start --scope api --wave w1 --leaf a
run "abandon blank reason" abandon --scope api --wave w1 --reason "   "
run "status on empty root" status --scope api --wave w1
# terminal_safe at 500: a control character in a message reaches the ESCAPE branch, and a long
# one reaches the TRUNCATION backoff. Both arrive through die()'s message.
# MEASURED CORRECTION: a hostile --wave/--scope is rejected by the id validator, whose message
# does NOT echo the value -- so the first version of these rows exercised the validator and never
# reached the escaper at all. `unknown command` and `unknown option` DO echo raw argv into die().
raw "control char escaped" "$(printf 'bogus\001cmd')"
raw "RTL override escaped" "$(printf 'a\u202eb')"
# U+0080-U+009F is the ONLY range where terminal_safe's `code <= 0xff` boundary decides between
# the \xNN and \uNNNN forms for a MULTI-BYTE character -- \x01 and U+202E sit on the same side of
# it either way. Found by mutating the boundary to 0x7f: with only those two rows the suite stayed
# green, so the branch had no coverage at all.
raw "C1 control escaped" "$(python3 -c 'print("a" + chr(0x9b) + "b")')"
raw "DEL escaped" "$(python3 -c 'print("a" + chr(0x7f) + "b")')"
# NOT a bare emoji: U+1F600 is not in _UNSAFE_TERMINAL and 4 bytes is far under the budget, so
# a bare-emoji row duplicates the plain unknown-command row and NO mutation of the escape or
# truncation logic can redden it -- the name claimed more than the row could show.
#
# MEASURED, and it corrects TWO wrong reasons I wrote here before. terminal_safe does NOT slice:
# it walks CHARACTERS accumulating BYTE sizes and breaks before adding one that would not fit, so
# a surrogate pair is never split and js_slice is not involved at all (that is _safe_diagnostic
# in dispatch.py, which really does slice by UTF-16 units -- the surrogate-split row belongs
# THERE, not here). And the message carries a 16-byte "unknown command " prefix that has to be
# counted, which is what made my first labels wrong:
#   497 z + 4 emoji -> 513 bytes, the cut lands in the ASCII run; no emoji ever enters `pieces`
#   480 z + 4 emoji -> 512 bytes, ONE emoji enters `pieces` (496+4 = 500, not over), then the
#                      marker backoff has to pop that 4-BYTE piece
#   400 z + 4 emoji -> 432 bytes, fits whole: the control proving the emoji survive intact
#
# The first two produce BYTE-IDENTICAL output (528 bytes, zero emoji), so they look redundant --
# I labelled the second "just under the budget", and it truncates. What separates them is only
# visible under mutation: replacing `total -= sizes.pop()` with `total -= 1` in the backoff
# reddens the EMOJI-RUN row and NOTHING else in this file. It is the only row here that checks
# the backoff subtracts a piece's real byte size rather than one byte.
# The ascii-run row that sat here (497 z + 4 emoji) is GONE, not renamed. Its emoji never
# entered `pieces`, so it was behaviourally "z"*497 -- an ASCII-run truncation already covered by
# the 900-byte row above and by the boundary rows below, and no mutation measured here isolates
# it from either. A row that duplicates another row's coverage while its name claims a mechanism
# it does not reach is worse than no row: it makes the file look like it tests more than it does.
raw "astral cut in the emoji run" "$(python3 -c 'print("z"*480 + chr(0x1F600)*4)')"
raw "astral under budget, intact"  "$(python3 -c 'print("z"*400 + chr(0x1F600)*4)')"
# EXACTLY on the boundary: "unknown command " is 16 bytes, so 484 z makes the last character the
# one where total+size == max_bytes == 500. `>` appends it and returns the whole message; `>=`
# truncates. Found by mutation -- `>` -> `>=` was caught by NOTHING in this file, because every
# other row lands strictly inside or strictly outside the budget and an off-by-one is invisible
# from either side. 483 and 485 bracket it, so a future off-by-one in the other direction has a
# witness too.
raw "exactly at the budget (484)" "$(python3 -c 'print("z"*484)')"
raw "one byte under (483)"        "$(python3 -c 'print("z"*483)')"
raw "one byte over (485)"         "$(python3 -c 'print("z"*485)')"
raw "over-budget truncates" "$(python3 -c 'print("z"*900)')"
# MEASURED: the old spelling of this row ("z"*495 + 4 RLO) never reached an RLO at all. The
# 16-byte "unknown command " prefix puts the cut at 484 z, so the break happens deep inside the
# z run and no escaped piece ever enters `pieces`. It reddened under the 500->1024 mutation only
# because a wider budget lets the escapes appear -- a budget row wearing an escape row's name,
# the same defect as the astral labels below. A review proposed 470 z; measured, that does not
# work either: two escapes DO enter `pieces`, and the marker backoff then pops both, so the
# output again carries none. The escapes have to sit early enough that the backoff eats the
# FILLER instead -- 400 z, then the escapes, then enough z to overrun the budget.
raw "escaped pieces survive truncation" "$(python3 -c 'print("z"*400 + chr(0x202e)*4 + "z"*200)')"
raw "unknown option echoes value" open --scope api --wave w1 --leaf a "$(printf -- '--o\001pt')"
echo "--- $( [ $fail = 0 ] && echo 'all identical' || echo 'DIVERGENCES ABOVE' ) ---"
exit $fail
