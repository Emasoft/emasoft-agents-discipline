#!/bin/bash
# Compare the two regex workers on one JSON message each, byte for byte.
#
# This is the port's WIDEST divergence surface and the only one where the two runtimes
# disagree by DESIGN rather than by accident: `RegExp` and `re` are different engines, so a
# pattern can compile in both and mean different things. The corpus below is chosen for that,
# not for coverage of the worker's plumbing (9 lines of it).
#
# Usage: bash tests/regex-worker-diff.sh   (run from skills/agents-discipline)
set -u
export PYTHONDONTWRITEBYTECODE=1
fail=0
rows=0
differed=0

row() {                     # row <label> <source> <flags> <output>
  local label="$1" src="$2" flags="$3" out="$4"
  rows=$((rows + 1))
  # The pin's keys are ONE LINE EACH and compared after a line-based `sort`, so a SOURCE
  # containing a newline would split into two entries and misalign the whole set -- the pin
  # would then fail, or worse pass, for a reason unrelated to divergence. No current row does
  # (only OUTPUTS are multi-line, which is fine, they are not part of the key), and this keeps
  # it that way. A regex with a literal newline should be written `\n` anyway.
  case "$src" in
    *"
"*) echo "PROBE FAILED: row '$label' has a newline in its SOURCE, which breaks the key set" >&2
        exit 1 ;;
  esac
  # EVERY row's key, divergent or not. The set pin below covers only the divergent ones, so
  # without this an AGREEING row -- including the trap row the swap control depends on -- could
  # be deleted with nothing noticing.
  ALL_KEYS="$ALL_KEYS$src<$flags>
"
  local msg o1 o2 e1 e2
  # jq builds the JSON so a backslash-heavy pattern reaches each side unmangled. Hand-rolled
  # string concatenation is how a `\d` becomes a `d` in exactly one of the two paths and the
  # row then reports a divergence that is the harness's, not the port's.
  msg=$(jq -cn --arg s "$src" --arg f "$flags" --arg o "$out" \
    '{source: $s, flags: $f, output: $o}')
  # PARSED, not byte-compared -- the SAME policy python-lib-checks.py:131-136 already
  # established, and adopting it here rather than inventing a second one is the point. Two
  # differences are contractual, not defects: the serialisers disagree on whitespace
  # (`{"matched":true}` vs `{"matched": true}`), and on an invalid pattern the message text is
  # the ENGINE'S own, so demanding equality would demand the port reimplement V8's diagnostics.
  # `{error: true}` collapses the second to its shape. A first version of this file compared
  # BYTES, reported 21/21 divergent on whitespace, and led to "fixing" the PRODUCTION
  # serialiser to satisfy a test -- a redundant test, since these two files both drive the same
  # pair. The contract is: same keys, and equal `matched` when there is no error.
  o1=$(printf '%s' "$msg" | node tests/regex-worker-drive.mjs 2>/tmp/rw-e1 \
       | jq -cS 'if has("error") then {error:true} else . end'; exit ${PIPESTATUS[0]}); e1=$?
  o2=$(printf '%s' "$msg" | python3 scripts/lib/regex_worker.py 2>/tmp/rw-e2 \
       | jq -cS 'if has("error") then {error:true} else . end'; exit ${PIPESTATUS[0]}); e2=$?
  if [ "$o1" = "$o2" ] && [ "$e1" = "$e2" ]; then
    printf 'ok       %s\n' "$label"
  else
    differed=$((differed + 1))
    # KEYED ON THE INPUTS, not on $label. The label is prose -- documentation that should stay
    # freely editable -- while the row's IDENTITY is the pattern and flags it feeds the worker.
    # A label-keyed pin fails on a pure rename with "a row APPEARED / a row VANISHED", both
    # false, and the correct response to a rename is exactly the "just update the list" the
    # message forbids. This round renamed a row (`lazy quantifier`), which only escaped that
    # trap because the row happens to sit in the AGREEING set.
    DIVERGENT_KEYS="$DIVERGENT_KEYS$src<$flags>
"
    printf 'DIVERGE  %s\n         js(%s): %s\n         py(%s): %s\n' \
      "$label" "$e1" "$o1" "$e2" "$o2"
  fi
}
DIVERGENT_KEYS=""
ALL_KEYS=""

# --- the engine-difference corpus -------------------------------------------------------
# Each row names the property under test. A row that cannot distinguish the engines is a
# CONTROL and is labelled as one -- it proves the harness reaches both sides at all, which is
# the check every probe in this project has needed and half of them lacked.
row 'CONTROL literal hit'            'ok'          ''  'ok'
row 'CONTROL literal miss'           'nope'        ''  'ok'
row 'unanchored (test, not match)'   'kay'         ''  'okay'
row 'dollar before trailing newline' 'ok$'         ''  'ok
'
row 'caret with m flag'              '^b'          'm' 'a
b'
row 'dot vs newline, s flag'         'a.b'         's' 'a
b'
row 'dot vs newline, no s flag'      'a.b'         ''  'a
b'
row 'backslash-d is ASCII only'      '^\d+$'       ''  '٣٤'
row 'backslash-w is ASCII only'      '^\w+$'       ''  'naïve'
row 'backslash-b word boundary'      '\bcat\b'     ''  'a cat here'
row 'JS named group (?<n>)'          '(?<y>\d{4})' ''  '2026'
row 'Python named group (?P<n>)'     '(?P<y>\d+)'  ''  '2026'
row 'backslash-A anchor'             '\Aok'        ''  'ok'
row 'backslash-Z anchor'             'ok\Z'        ''  'ok'
row 'empty character class'          'a[]b'        ''  'ab'
row 'lookbehind'                     '(?<=v)\d'    ''  'v7'
# NOT "lazy quantifier". The worker contract is `{matched: bool}`, and lazy-vs-greedy changes
# WHAT is captured, never WHETHER a match exists -- so a boolean harness is architecturally
# blind to laziness and this row could never fail for the property its old name claimed.
row 'a.*?b matches at all'           'a.*?b'       ''  'axxbxxb'
# THE TRAP ROW. `$` and `m` AGREE (JS `$` with `m` matches at every line end, and so does
# Python's with MULTILINE) -- so the obvious emulation recipe "translate `$` -> `\Z`" would
# convert this PASSING row into a divergence. Without this row the harness would see only a
# count move, which is exactly why the pin below is a SET and not a count.
row 'dollar with m flag agrees'      'b$'          'm' 'b
c'
row 'i flag'                         'OK'          'i' 'ok'
row 'unmatched paren is an error'    'a('          ''  'a'
row 'octal-looking escape'           '\101'        ''  'A'
row 'unicode property escape'        '\p{L}'       'u' 'a'

echo
# NON-VACUITY: a corpus that silently shrank (an editing accident, a `row` call lost to a bad
# heredoc) would report "all identical" over nothing at all. Same gate as lease-diff.sh.
#
# A FLOOR, and deliberately not an equality. The original floor was `-lt 15` against 22 rows,
# so seven could be deleted unnoticed -- but the fix briefly went to `EXPECTED_ROWS=22`, and an
# equality fails on every legitimate ADDITION, with a message telling the reader to bump the
# number. Forty lines below, the set pin's message says the opposite: "do NOT just update the
# list". Two adjacent guards teaching opposite lessons is worse than either alone, because
# which one is right then depends on the reader noticing which fired. Adding rows is the normal
# edit to this corpus; a floor cannot fire on one.
#
# The floor is set to the count the OTHER guards actually protect: 7 divergent rows (set pin) +
# the named trap row. Deleting a non-load-bearing agreeing row is not worth a false alarm on
# every addition.
if [ "$rows" -lt 8 ]; then
  echo "VACUOUS: only $rows rows executed"; exit 1
fi
# And name the trap row explicitly, because a count alone permits swapping it for another.
case "$ALL_KEYS" in
  *'b$<m>'*) ;;
  *) echo "VACUOUS: the 'dollar with m flag agrees' trap row (b\$<m>) is gone"; exit 1 ;;
esac
echo "$rows rows, $differed divergent"
# PINNED TO THE SET OF LABELS, NOT THEIR COUNT. `RegExp` and `re` genuinely disagree on these
# rows and the fix is an undecided design question (emulate JS semantics, or document the
# divergence -- see the TRDD). A check that stays RED forever gets ignored and is then "fixed"
# by weakening it, so a pin is right; but the FIRST version of this pin compared `$differed`
# to a number and printed "none new". Under a SWAP -- one row stops discriminating while a new
# port defect appears -- the count holds and the script prints that sentence, false by
# construction in the one case it exists to catch. That is this project's own catalogued
# failure shape (`reddens 9`: a label asserting more than the measurement), rebuilt inside the
# check meant to prevent it.
#
# A swap is not hypothetical here: the pending work is an emulation pass, which fixes some rows
# and can break others in the SAME commit. A set pin fails on any single change in either
# direction, so it needs no separate swap control -- which is fortunate, because a swap control
# is genuinely hard to construct and that is likely why the count version shipped.
#
# Entries are `<source><flags-in-angle-brackets>`, i.e. the row's INPUTS. Renaming a row's
# prose label does not touch this list; changing what it TESTS does.
EXPECTED_DIVERGENT_SET='(?<y>\d{4})<>
(?P<y>\d+)<>
\Aok<>
\p{L}<u>
a[]b<>
ok$<>
ok\Z<>'
actual=$(printf '%s' "$DIVERGENT_KEYS" | LC_ALL=C sort)
expected=$(printf '%s' "$EXPECTED_DIVERGENT_SET" | LC_ALL=C sort)
if [ "$actual" != "$expected" ]; then
  echo "--- DIVERGENCE SET CHANGED ---"
  echo "    Do NOT just update the list. A row that APPEARED is a port defect; a row that"
  echo "    VANISHED means it stopped discriminating. Diff (expected < / actual >):"
  diff <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") | sed 's/^/    /'
  exit 1
fi
# THE MESSAGE MUST NOT CLAIM HEALTH. Exit 0 means "no REGRESSION since the set was pinned" --
# it does NOT mean the port is correct, and the earlier wording ("known engine divergences
# only, set unchanged") was read as a clean bill in a regression tally that then reported
# "11 suites PASS". Five of these seven are ways the PORT behaves differently from the ORACLE,
# including the highest-ranked hazard in the TRDD: `EXPECT: /ok$/` fails in the oracle and
# PASSES in the port, on output gate-check assembles without trimming. The emulate-vs-document
# decision is still open, so those are UNRESOLVED DEFECTS, not neutral facts about two
# runtimes. A pin is the right tool here; asserting health in its passing message was not.
# WHY STILL EXIT 0, having considered the alternatives:
#   exit 1  -> permanently red. Rejected earlier in this file for the reason that still holds:
#             a check that is always red gets ignored, then "fixed" by weakening it.
#   exit 3  -> a distinct "known defects, no regression" code IS the right shape, and a machine
#             could then never tally this as PASS. NOT added, because nothing in this repo
#             invokes this script: an exit-code protocol with no reader was added here once
#             before and deleted for exactly that (R7). Add it in the SAME change that writes a
#             batch runner, never speculatively.
#   exit 0 + a message that refuses to claim health -> what is here. It relies on a human
#             reading the message, which is a real weakness and is why the wording is blunt.
echo "--- $differed KNOWN PORT DIVERGENCES, UNRESOLVED (see TRDD) — set unchanged, no regression ---"
echo "    exit 0 means NOTHING NEW, not 'the port is correct'. Do not tally this as a plain PASS."
exit 0
