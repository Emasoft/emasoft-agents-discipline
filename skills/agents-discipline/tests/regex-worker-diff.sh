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
    DIVERGENT_LABELS="$DIVERGENT_LABELS$label
"
    printf 'DIVERGE  %s\n         js(%s): %s\n         py(%s): %s\n' \
      "$label" "$e1" "$o1" "$e2" "$o2"
  fi
}
DIVERGENT_LABELS=""

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
if [ "$rows" -lt 15 ]; then
  # NON-VACUITY: a corpus that silently shrank (an editing accident, a `row` call lost to a
  # bad heredoc) would report "all identical" over nothing at all. Same gate as lease-diff.sh,
  # and for the same reason: the diff passing is not evidence the diff ran.
  echo "VACUOUS: only $rows rows executed"; exit 1
fi
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
EXPECTED_DIVERGENT_SET='JS named group (?<n>)
Python named group (?P<n>)
backslash-A anchor
backslash-Z anchor
dollar before trailing newline
empty character class
unicode property escape'
actual=$(printf '%s' "$DIVERGENT_LABELS" | LC_ALL=C sort)
expected=$(printf '%s' "$EXPECTED_DIVERGENT_SET" | LC_ALL=C sort)
if [ "$actual" != "$expected" ]; then
  echo "--- DIVERGENCE SET CHANGED ---"
  echo "    Do NOT just update the list. A row that APPEARED is a port defect; a row that"
  echo "    VANISHED means it stopped discriminating. Diff (expected < / actual >):"
  diff <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") | sed 's/^/    /'
  exit 1
fi
echo "--- known engine divergences only, set unchanged ---"
exit 0
