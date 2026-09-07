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
  # `; printf X` is a SENTINEL: $( ) strips ALL trailing newlines, so a reply written with and
  # without one compare EQUAL without it. Same reason dispatch-cli-drive.sh carries it.
  o1=$(printf '%s' "$msg" | node tests/regex-worker-drive.mjs 2>/tmp/rw-e1; rc=$?; printf X; exit $rc); e1=$?
  o2=$(printf '%s' "$msg" | python3 scripts/lib/regex_worker.py 2>/tmp/rw-e2; rc=$?; printf X; exit $rc); e2=$?
  if [ "$o1" = "$o2" ] && [ "$e1" = "$e2" ]; then
    printf 'ok       %s\n' "$label"
  else
    differed=$((differed + 1)); fail=1
    printf 'DIVERGE  %s\n         js(%s): %s\n         py(%s): %s\n' \
      "$label" "$e1" "${o1%X}" "$e2" "${o2%X}"
  fi
}

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
row 'lazy quantifier'                'a.*?b'       ''  'axxbxxb'
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
# PINNED, not zero. `RegExp` and `re` genuinely disagree on 8 of these rows and the fix is an
# undecided design question (emulate JS semantics, or document the divergence -- see the TRDD).
# A check that stays RED forever gets ignored, and the next person "fixes" it by weakening it;
# a check pinned to the KNOWN set stays green while still failing the moment the set CHANGES,
# in either direction. EQUALITY, not a floor: a floor here would hide a regression that adds a
# divergence, which is the only thing this file can still catch.
EXPECTED_DIVERGENT=8
if [ "$differed" != "$EXPECTED_DIVERGENT" ]; then
  echo "--- DIVERGENCE SET CHANGED: expected $EXPECTED_DIVERGENT, got $differed ---"
  echo "    Re-read the rows above. Do NOT just update the number -- a NEW divergence is a"
  echo "    port defect, and a VANISHED one means a row stopped discriminating."
  exit 1
fi
echo "--- $EXPECTED_DIVERGENT known engine divergences, none new ---"
exit 0
