#!/usr/bin/env bash
# Differential for NON-ASCII round-tripping across every surface the port WRITES or PRINTS.
#
# WHY THIS FILE EXISTS: two real defects of exactly this shape shipped, and both were found by
# hand rather than by the suite. Each was a bare `json.dumps` whose ensure_ascii defaults to
# True, against a `JSON.stringify` that emits the character raw:
#
#   gates.py claim_leases      the LEASE FILE's bytes          (fixed 87640c8)
#   gates.py parse warnings    the EXPECT warning a USER READS (fixed 13c2c4e)
#
# Neither had regression coverage afterwards, which is how the second one survived the commit
# that fixed the first. lease-diff.sh does exercise the lease machinery -- with ASCII fixtures,
# which is precisely why the divergence was invisible to it. A differential only tests the
# vectors it presents, and "non-ASCII" was a vector nothing in the suite presented.
#
# THE FAILURE IS SILENT IN BOTH CASES, and for the same reason: a backslash-u escape ROUND-TRIPS
# within one runtime. json.load decodes it straight back to the same str, so every behavioural
# assertion passes on either side. Only the BYTES differ -- and the lease file is the one
# artifact whose reader may be the other runtime.
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
# CHECKED, because _rebuild_c3 now runs `rm -rf "$WORK/c3"` fourteen times per run. `set -u`
# does NOT catch an empty assignment from a failed command substitution, so an unchecked
# mktemp failure would make $c3 the literal "/c3" and turn a fixture reset into a recursive
# delete at the filesystem root. Unlikely; unguarded destructive commands whose blast radius
# grew in the same commit that multiplied their call count are worth one line.
[ -n "$WORK" ] && [ -d "$WORK" ] || { echo "mktemp -d failed; refusing to build fixtures" >&2; exit 2; }
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
declare -a FAILED=()

# Built with printf and an explicit UTF-8 byte sequence, never a literal character: a writing
# layer between here and the file has already turned an escape into a raw character once in this
# project, and a fixture that silently loses its non-ASCII tests nothing while still passing.
NON_ASCII="$(printf 'caf\xc3\xa9')"
# AND ASSERTED TO BE NON-ASCII, because the per-case gates below CANNOT check this. They grep
# the oracle's output for "$NON_ASCII" -- the same variable -- so if the printf ever yielded the
# literal text backslash-x-c-3 instead of the byte, both runtimes would handle it as plain ASCII,
# agree, and the gate would match the literal and pass. A whole file testing nothing, with three
# green rows. That is a control validating itself, the exact shape this suite exists to distrust,
# so the check is made against the BYTES and not against the variable's provenance.
# MEASURED today: 5 bytes under both bash and zsh (c, a, f, and the two UTF-8 bytes of U+00E9).
if ! printf '%s' "$NON_ASCII" | LC_ALL=C grep -q '[^ -~]'; then
  echo "DIVERGE  fixture text is pure ASCII; every case below would be vacuous" >&2
  exit 1
fi

# THIS FILE'S OWN SOURCE MUST BE PURE ASCII, and that is not tidiness. Every non-ASCII value
# here is built from code points (printf byte escapes, String.fromCharCode, chr) precisely
# because a literal character or a backslash-u escape has been silently normalised on the way
# into this file four times -- twice producing a "divergence" whose two sides rendered
# identically. A literal that creeps back in is invisible to every case above, so it is checked
# directly. Tabs and newlines are ASCII and pass; only bytes >= 0x80 fail.
# `tr -d`, NOT `grep -P`. MEASURED: /usr/bin/grep on macOS exits 2 on -P ("unsupported"), and
# `if grep -qP ...; then fail; fi` reads a non-zero exit as "no match" -- so on any machine whose
# grep lacks -P (stock macOS, most BSD) the guard would PASS SILENTLY and protect nothing. It
# happens to work here only because this PATH prepends GNU grep. A guard whose failure mode is
# "quietly approve" is the thing this file exists to distrust, so it is written with tools that
# cannot be missing: tr deletes every ASCII byte and wc counts what is left.
# BASH_SOURCE is checked before it is used. The tools were made unmissable (tr and wc are POSIX,
# LC_ALL=C pins the byte semantics) and then the PATH EXPRESSION was left as a bashism: under a
# shell without BASH_SOURCE it could expand empty, `tr < ""` fails, `wc -c` prints 0, and the
# guard reads "clean" -- the grep -P defect one shell over, in the line that replaced it.
[ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] \
  || { echo "DIVERGE  the ASCII-source guard cannot locate its own source" >&2; exit 2; }
_non_ascii_bytes=$(LC_ALL=C tr -d '\000-\177' < "${BASH_SOURCE[0]}" | wc -c | tr -d ' ')
if [ "$_non_ascii_bytes" != 0 ]; then
  echo "DIVERGE  this script's source carries $_non_ascii_bytes literal non-ASCII byte(s); build them from code points" >&2
  exit 1
fi

# DIFFERENT ROOTS PER RUNTIME ARE SAFE HERE, and that needs saying because stale-diff.sh
# documents the opposite for ITS cases: there, both runtimes must share one directory because
# the approval token and the EVIDENCE line bind to the resolved path, so separate roots
# manufacture a diff the port did not cause. Neither surface below has that binding. `--claim`
# writes {scope, leaf, globs, pid} -- no path at all -- and `--status` never approves and never
# writes EVIDENCE. The only path that reaches either transcript is the ledger's own, which the
# scrub normalizes; MEASURED by reading a real DIVERGE line, where the warning appears as
# `<ROOT>/.agents-discipline/s/gates/leaf.md`. Separate roots are in fact REQUIRED for case 1:
# a lease records a pid, so replaying the same claim into one root would make the second
# runtime collide with the first's live lease (lease-diff.sh's own note).
#
# The two substitutions are not equally load-bearing, and the honest split is: <ROOT> does the
# work in case 2 and NOTHING in case 1 (that file's content carries no path); <PID> is the
# reverse. Both are applied to both because a scrub that is inert is free, and one that is
# missing where it was needed is a false divergence.
# THE RESOLVED FORM IS SCRUBBED TOO. On macOS `mktemp -d` yields /var/folders/... while a path
# that has been through realpath prints as /private/var/folders/... -- the same directory under
# two spellings. A literal substitution of only the mktemp form leaves `/private<ROOT>` in the
# output, which is a difference between the two runtimes only if they resolve differently, and
# noise otherwise. MEASURED in this file's own dispatch row, whose first draft printed exactly
# that. Both spellings map to <ROOT>, longest first so the /private form is consumed whole.
_scrub() {
  sed -e "s|/private$1|<ROOT>|g" -e "s|$1|<ROOT>|g" -e 's/"pid": [0-9]*/"pid": <PID>/'
}

# --- CASE 1: the lease file's BYTES -------------------------------------------------------
# Two separate roots, one per runtime: a lease records a pid, so replaying the same claim
# against ONE root would make the second runtime collide with the first's live lease and
# manufacture a conflict that is not about either implementation (lease-diff.sh's own note).
_lease_bytes() {
  local exe="$1" script="$2" root="$3"
  mkdir -p "$root/.agents-discipline/s/gates"
  printf '# Gates\n\nOWNS: src/%s/**\n\n- [ ] G1: x\n  CHECK: true\n  EXPECT: ok\n' "$NON_ASCII" \
    > "$root/.agents-discipline/s/gates/leaf.md"
  "$exe" "$script" --root "$root" --claim --scope s >/dev/null 2>&1
  local f; f="$(find "$root" -name '*.lease' -type f | head -1)"
  [ -n "$f" ] || { echo "(no lease file written)"; return; }
  _scrub "$root" < "$f"
}
o_lease="$(_lease_bytes "$NODE_ABS" "$ORACLE" "$WORK/lo")"
p_lease="$(_lease_bytes "$PY_ABS" "$PORT" "$WORK/lp")"
# NON-VACUITY: the fixture must actually have carried the non-ASCII glob into the file. Without
# this, a claim that failed for an unrelated reason would leave both sides equal and "identical".
if ! printf '%s' "$o_lease" | grep -q "$NON_ASCII"; then
  printf 'DIVERGE  %-38s oracle lease lacks the non-ASCII glob; fixture reached nothing\n' \
    "lease file bytes"
  printf '%s\n' "$o_lease" | sed 's/^/    | /'
  fail=$((fail + 1)); FAILED+=("lease file bytes")
elif [ "$o_lease" = "$p_lease" ]; then
  printf '  OK      %-38s non-ASCII glob written identically\n' "lease file bytes"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "lease file bytes"
  diff <(printf '%s\n' "$o_lease") <(printf '%s\n' "$p_lease")
  fail=$((fail + 1)); FAILED+=("lease file bytes")
fi

# --- CASE 2: the EXPECT warning a user READS ----------------------------------------------
# The pathLike branch needs the WRAPPING slashes AND an inner slash. A first attempt at this
# fixture had only the wrapping pair, produced NO warning from either runtime, and that empty
# result was nearly recorded as agreement -- it was a fixture that reached nothing. Hence the
# non-vacuity check below, which is not decoration.
# STREAMS CAPTURED SEPARATELY, never merged with 2>&1. stale-diff.sh documents the loud half --
# node and python interleave differently, so a merge diffs flush order rather than content. The
# SILENT half is worse and is the reason this matters for a WARNING specifically: if the oracle
# wrote it to stderr and the port to stdout, the merged text would be byte-identical and a real
# stream-routing divergence (console.warn vs console.log vs print(file=sys.stderr) is exactly
# the kind a port gets wrong) would pass unseen. The merge shipped here once already, three
# commits after being repudiated elsewhere in this same suite.
_expect_warning() {  # exe script root outvar errvar
  local exe="$1" script="$2" root="$3"
  mkdir -p "$root/.agents-discipline/s/gates"
  printf '# Gates\n\nOWNS: src/**\n\n- [ ] G1: x\n  CHECK: true\n  EXPECT: /src/%s/out.txt/\n' \
    "$NON_ASCII" > "$root/.agents-discipline/s/gates/leaf.md"
  "$exe" "$script" --root "$root" --scope s --status > "$WORK/.o" 2> "$WORK/.e"
  printf -v "$4" '%s' "$(_scrub "$root" < "$WORK/.o")"
  printf -v "$5" '%s' "$(_scrub "$root" < "$WORK/.e")"
}
_expect_warning "$NODE_ABS" "$ORACLE" "$WORK/wo" o_warn o_warn_err
_expect_warning "$PY_ABS" "$PORT" "$WORK/wp" p_warn p_warn_err
# The warning's STREAM is part of what is compared: concatenating with a marker keeps one
# equality test while making a routing difference visible in the diff rather than cancelling out.
o_warn="$(printf '%s\n--stderr--\n%s' "$o_warn" "$o_warn_err")"
p_warn="$(printf '%s\n--stderr--\n%s' "$p_warn" "$p_warn_err")"
if ! printf '%s' "$o_warn" | grep -q 'read as a regular expression'; then
  printf 'DIVERGE  %-38s oracle printed no pathLike warning; fixture reached nothing\n' \
    "EXPECT warning text"
  printf '%s\n' "$o_warn" | sed 's/^/    | /'
  fail=$((fail + 1)); FAILED+=("EXPECT warning text")
elif [ "$o_warn" = "$p_warn" ]; then
  printf '  OK      %-38s non-ASCII EXPECT reported identically\n' "EXPECT warning text"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "EXPECT warning text"
  diff <(printf '%s\n' "$o_warn") <(printf '%s\n' "$p_warn")
  fail=$((fail + 1)); FAILED+=("EXPECT warning text")
fi

# --- CASE 3: a NON-UTF-8 STDOUT, which is the environment the other two cases cannot reach ----
# node writes UTF-8 to stdout/stderr whatever the locale says; CPython follows the locale. On an
# ASCII stream the port therefore backslash-escapes on stderr and CRASHES on stdout, where there
# is no backslashreplace fallback -- MEASURED, gate-lint died with UnicodeEncodeError partway
# through its report while the oracle printed the warning.
#
# THE ENV IS THREE VARIABLES, NOT ONE, and that is the whole reason this case exists as its own
# row. A bare LC_ALL=C measures nothing: PEP 538 locale coercion and PEP 540 UTF-8 mode promote
# it back to UTF-8 before any output happens, so `sys.stdout.encoding` reports utf-8 and the
# suite passes. An earlier commit recorded "green under LC_ALL=C" on exactly that basis and it
# was worthless. Both escape hatches must be shut to reach the ASCII stream that a bare
# container, a cron job, or a CI runner can still produce.
#
# EVERY CLI THAT CALLS force_utf8_streams IS DRIVEN, not just gate-lint. The first version ran
# gate-lint alone, on the reasoning that it writes findings to STDOUT -- the stream with no
# backslashreplace fallback, so it fails loudly. That is still why gate-lint is the best single
# row, and it was the wrong place to stop: three of the four call sites had NO case that would
# notice their force_utf8_streams() call being deleted, while the commit's framing implied the
# surface was covered. The library-level CASE 4 cannot see a missing call site either, by
# construction.
c3="$WORK/c3"
_rebuild_c3() {
  rm -rf "$c3"; mkdir -p "$c3/.agents-discipline/s/gates"
  printf '# Gates\n\n- [ ] G1: x\n  CHECK: true\n  EXPECT: /src/%s/out.txt/\n' "$NON_ASCII" \
    > "$c3/leaf.md"
  cp "$c3/leaf.md" "$c3/.agents-discipline/s/gates/leaf.md"
  printf '# Delegation\n\n| # | Unit | Files (mine) | Worker | Acceptance | Status |\n|---|---|---|---|---|---|\n| 1 | %s unit | src/a | w | `true` | pending |\n' \
    "$NON_ASCII" > "$c3/DELEGATION.md"
}
_rebuild_c3
_hostile() {  # exe script args... -> stdout + exit + stderr, scrubbed
  local exe="$1" script="$2"; shift 2
  PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C "$exe" "$script" "$@" > "$WORK/.ho" 2> "$WORK/.he"
  # $? CAPTURED ON ITS OWN LINE, BEFORE any expansion. The first version interpolated "$?"
  # inside the printf below, where bash expands arguments LEFT TO RIGHT: the preceding
  # $(_scrub ...) ran first and overwrote it, so the field carried sed's status -- 0 on both
  # sides, forever. An exit-code comparison that can only ever compare 0 with 0, in the file
  # whose subject is checks that cannot fail. The case still reddened, but on the stderr text,
  # not on the code it claimed to compare.
  local code=$?
  printf '%s\n--exit--\n%s\n--stderr--\n%s' \
    "$(_scrub "$c3" < "$WORK/.ho")" "$code" "$(_scrub "$c3" < "$WORK/.he")"
}
_hostile_case() {  # label oracle-basename port-basename args...
  local label="$1" ora="$HERE/../scripts/$2" prt="$HERE/../scripts/$3"; shift 3
  local o p
  # THE FIXTURE IS REBUILT BETWEEN THE TWO RUNS, because one of these CLIs REWRITES the file it
  # is handed: ledger-check appends a `receipt:` line, so the oracle ran first, modified the
  # fixture, and the port then reported a receipt the oracle had not printed. MEASURED as a
  # DIVERGE on `receipt: binds this exact content, last checked <ISO>` -- a harness artifact
  # presented as a port defect. stale-diff.sh has the SAME hazard and it is worth naming
  # precisely, because that file carries two different rules that are easy to merge into one
  # wrong lineage: its rows must share ONE directory (the approval token binds the resolved
  # path, so separate roots manufacture a diff), and SEPARATELY its _case rebuilds that
  # directory between the two runs (the first run rewrites the ledger). This is the second rule,
  # not an inversion of the first.
  _rebuild_c3
  o="$(_hostile "$NODE_ABS" "$ora" "$@")"
  _rebuild_c3
  p="$(_hostile "$PY_ABS" "$prt" "$@")"
  # NON-VACUITY per row: the ORACLE must have carried the non-ASCII text into its own output.
  # Without it a CLI that printed nothing (wrong args, missing fixture) would compare equal to a
  # port that also printed nothing, and the row would report OK for a command that did nothing.
  if ! printf '%s' "$o" | grep -q "$NON_ASCII"; then
    printf 'DIVERGE  %-38s oracle output lacks the non-ASCII text; fixture reached nothing\n' "$label"
    printf '%s\n' "$o" | sed 's/^/    | /' | head -6
    fail=$((fail + 1)); FAILED+=("$label"); return
  fi
  if [ "$o" = "$p" ]; then
    printf '  OK      %-38s identical on an ASCII stdout\n' "$label"
    pass=$((pass + 1)); return
  fi
  printf 'DIVERGE  %-38s\n' "$label"
  diff <(printf '%s\n' "$o") <(printf '%s\n' "$p")
  fail=$((fail + 1)); FAILED+=("$label")
}

_hostile_case "non-UTF-8 stdout: gate-lint"  gate-lint.mjs    gate_lint.py    "$c3/leaf.md"
_hostile_case "non-UTF-8 stdout: gate-check" gate-check.mjs   gate_check.py   --root "$c3" --scope s --status
_hostile_case "non-UTF-8 stdout: ledger"     ledger-check.mjs ledger_check.py "$c3/DELEGATION.md"

# dispatch-check needs its OWN block: the non-ASCII text reaches its stdout only through an
# abandon REASON, which requires a two-command sequence (open, then abandon) that
# _hostile_case's single-invocation shape cannot express.
#
# THIS ROW EXISTS BECAUSE THE OMISSION WAS NOT PRINCIPLED. dispatch-check was the fourth CLI
# calling force_utf8_streams and the only one still without a row, on the strength of an earlier
# turn failing to construct a vector -- leaf ids are ASCII by validation, and a non-ASCII reason
# did not surface in `status`. It surfaces in ABANDON's own line, which that attempt never ran.
# "I could not build a vector" is a fact about the attempt, not about the surface.
_dispatch_hostile() {  # exe script root -> the abandon line, scrubbed
  local exe="$1" script="$2" root="$3"
  PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C \
    "$exe" "$script" open --root "$root" --scope s --wave w1 --leaf a >/dev/null 2>&1
  PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C \
    "$exe" "$script" abandon --root "$root" --scope s --wave w1 --reason "$NON_ASCII reason" \
    > "$WORK/.do" 2> "$WORK/.de"
  local code=$?
  printf '%s\n--exit--\n%s\n--stderr--\n%s' \
    "$(_scrub "$root" < "$WORK/.do")" "$code" "$(_scrub "$root" < "$WORK/.de")"
}
# Separate roots per runtime, never one shared: dispatch.json is written by the first run and
# read by the second, so a shared root would have the port reporting a wave the oracle had
# already abandoned -- the same mutation hazard _rebuild_c3 exists for, one file over.
mkdir -p "$WORK/dispo" "$WORK/dispp"
o_disp="$(_dispatch_hostile "$NODE_ABS" "$HERE/../scripts/dispatch-check.mjs" "$WORK/dispo")"
p_disp="$(_dispatch_hostile "$PY_ABS" "$HERE/../scripts/dispatch_check.py" "$WORK/dispp")"
if ! printf '%s' "$o_disp" | grep -q "$NON_ASCII"; then
  printf 'DIVERGE  %-38s oracle abandon line lacks the non-ASCII reason; fixture reached nothing\n' \
    "non-UTF-8 stdout: dispatch"
  printf '%s\n' "$o_disp" | sed 's/^/    | /' | head -6
  fail=$((fail + 1)); FAILED+=("non-UTF-8 stdout: dispatch")
elif [ "$o_disp" = "$p_disp" ]; then
  printf '  OK      %-38s identical on an ASCII stdout\n' "non-UTF-8 stdout: dispatch"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "non-UTF-8 stdout: dispatch"
  diff <(printf '%s\n' "$o_disp") <(printf '%s\n' "$p_disp")
  fail=$((fail + 1)); FAILED+=("non-UTF-8 stdout: dispatch")
fi

# --- CASE 4: the STREAM HANDLER itself, on lone surrogates -----------------------------------
# The one surface here that needs no CLI, so it is not contaminated by either defect that keeps
# the other cases narrow (gate-lint's errno message shape, write_atomic raising into
# dispatch.json). It was skipped for that reason anyway, and it is the fix with the WORST
# regression history in this suite: shipped as a crash, then as a silently-reset `strict`
# handler, then as one U+FFFD for a whole run where node emits one PER code point. Three
# regressions, zero coverage, until now.
#
# Every string is built from CODE POINTS on both sides -- String.fromCharCode in JS, chr() in
# Python -- never as a literal character and never as a backslash-u escape. Both of those
# spellings were silently normalised on the way into this file, four times while it was written,
# each time producing a "divergence" whose two sides were byte-identical. The script asserts its
# own source is pure ASCII below, so a literal that creeps back in fails loudly.
#
# The last two rows are CONTROLS. An astral character is a surrogate PAIR to UTF-16 and ONE code
# point to Python, so it must pass through untouched; if the handler ever mangles it, every emoji
# in every message breaks. A plain non-ASCII character must likewise be emitted raw. Without
# them, a handler that replaced everything with U+FFFD would satisfy the first four rows.
#
# WHAT THIS CASE DOES NOT COVER, stated because it drives the LIBRARY and not a CLI: it proves
# the handler matches node, not that any shipped command reaches it. A CLI that never calls
# force_utf8_streams would still diverge and this case would stay green -- CASE 3 is what holds
# an actual CLI end-to-end. A CLI-level surrogate case is blocked on gate-lint's errno message
# shape (recorded as deferred in d406b47), which contaminates the only argv vector that carries
# a surrogate into a command.
#
# The repetition count is Python CODE POINTS while node replaces per UTF-16 CODE UNIT, and the
# two cannot disagree here: the only characters UTF-8 refuses are the surrogates U+D800-U+DFFF,
# and each of those is exactly one code point AND one code unit. No astral character is
# unencodable, so no run can contain a code point worth two units.
_surrogate_bytes() {  # runtime(node|py) -> hex bytes of the whole corpus
  # An explicit reject on anything else, matching _write_fixture's `*)` guard. With a two-branch
  # if/else, a typo in a caller ("nodejs") would silently take the Python branch and compare the
  # PORT AGAINST ITSELF -- reporting OK for a comparison that never ran the oracle.
  case "$1" in node|py) ;; *) echo "unknown runtime: $1" >&2; exit 2 ;; esac
  if [ "$1" = node ]; then
    "$NODE_ABS" -e '
      // String.fromCharCode / fromCodePoint on BOTH the surrogate and the plain non-ASCII row,
      // so this file stays pure ASCII. A raw character here was silently normalised on the way
      // in four times while this suite was written; the source is grepped for non-ASCII bytes
      // as part of the run below, and a literal would fail that grep.
      const rows = [
        "a" + String.fromCharCode(0xd800) + "b",
        "a" + String.fromCharCode(0xdfff) + "b",
        String.fromCharCode(0xdc00) + String.fromCharCode(0xd800),
        String.fromCharCode(0xd800) + String.fromCharCode(0xd801) + String.fromCharCode(0xd802),
        "x" + String.fromCodePoint(0x1f600) + "y",
        "caf" + String.fromCharCode(0xe9),
      ];
      process.stdout.write(rows.join("|"));'
  else
    # The lib directory arrives as ARGV, not interpolated into the source. Interpolation was the
    # first spelling and it embeds an arbitrary filesystem path inside a Python string literal:
    # a repository checked out under a directory containing an apostrophe would produce a
    # SyntaxError, and the case would fail for a reason that has nothing to do with encoding.
    "$PY_ABS" -c '
import sys, os
sys.path.insert(0, sys.argv[1])
from jsapi import force_utf8_streams
force_utf8_streams()
rows = ["a" + chr(0xD800) + "b", "a" + chr(0xDFFF) + "b",
        chr(0xDC00) + chr(0xD800), chr(0xD800) + chr(0xD801) + chr(0xD802),
        "x" + chr(0x1F600) + "y", "caf" + chr(0xE9)]
sys.stdout.write("|".join(rows))' "$(cd "$(dirname "$PORT")/lib" && pwd)"
  fi | od -An -tx1 | tr -s ' \n' ' '
}
o_sur="$(_surrogate_bytes node)"
p_sur="$(_surrogate_bytes py)"
# NON-VACUITY: the oracle must actually have emitted a U+FFFD (ef bf bd). If node ever stopped
# substituting, every row would still compare equal against a port that also stopped, and the
# case would pass while testing nothing.
if ! printf '%s' "$o_sur" | grep -q 'ef bf bd'; then
  printf 'DIVERGE  %-38s oracle emitted no U+FFFD; the corpus reached no surrogate\n' \
    "lone surrogates on the stream"
  fail=$((fail + 1)); FAILED+=("lone surrogates on the stream")
elif [ "$o_sur" = "$p_sur" ]; then
  printf '  OK      %-38s U+FFFD per code point, astral pair intact\n' "lone surrogates on the stream"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "lone surrogates on the stream"
  printf '    oracle: %s\n    port  : %s\n' "$o_sur" "$p_sur"
  fail=$((fail + 1)); FAILED+=("lone surrogates on the stream")
fi

echo
if [ "$fail" = 0 ]; then
  echo "--- $pass non-ASCII surface(s) identical ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
