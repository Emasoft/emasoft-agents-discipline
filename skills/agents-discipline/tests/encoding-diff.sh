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
if LC_ALL=C grep -qP '[^\x00-\x7F]' "${BASH_SOURCE[0]}"; then
  echo "DIVERGE  this script's source carries a literal non-ASCII byte; build it from code points" >&2
  LC_ALL=C grep -nP '[^\x00-\x7F]' "${BASH_SOURCE[0]}" >&2
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
_scrub() { sed -e "s|$1|<ROOT>|g" -e 's/"pid": [0-9]*/"pid": <PID>/'; }

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
# gate-lint rather than gate-check: it writes its findings to STDOUT, which is the stream with
# no fallback and therefore the one that fails loudly instead of quietly.
LINT_ORACLE="$HERE/../scripts/gate-lint.mjs"
LINT_PORT="$HERE/../scripts/gate_lint.py"
c3="$WORK/c3"; mkdir -p "$c3"
printf '# Gates\n\n- [ ] G1: x\n  CHECK: true\n  EXPECT: /src/%s/out.txt/\n' "$NON_ASCII" \
  > "$c3/leaf.md"
_hostile() {  # exe script -> stdout + exit + stderr, scrubbed
  PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C "$1" "$2" "$c3/leaf.md" > "$WORK/.ho" 2> "$WORK/.he"
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
o_lint="$(_hostile "$NODE_ABS" "$LINT_ORACLE")"
p_lint="$(_hostile "$PY_ABS" "$LINT_PORT")"
if ! printf '%s' "$o_lint" | grep -q "$NON_ASCII"; then
  printf 'DIVERGE  %-38s oracle lint output lacks the non-ASCII EXPECT; fixture reached nothing\n' \
    "non-UTF-8 stdout"
  fail=$((fail + 1)); FAILED+=("non-UTF-8 stdout")
elif [ "$o_lint" = "$p_lint" ]; then
  printf '  OK      %-38s identical on an ASCII stdout\n' "non-UTF-8 stdout"
  pass=$((pass + 1))
else
  printf 'DIVERGE  %-38s\n' "non-UTF-8 stdout"
  diff <(printf '%s\n' "$o_lint") <(printf '%s\n' "$p_lint")
  fail=$((fail + 1)); FAILED+=("non-UTF-8 stdout")
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
_surrogate_bytes() {  # exe -> hex bytes of the whole corpus
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
    "$PY_ABS" -c '
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("'"$PORT"'")), "lib"))
from jsapi import force_utf8_streams
force_utf8_streams()
rows = ["a" + chr(0xD800) + "b", "a" + chr(0xDFFF) + "b",
        chr(0xDC00) + chr(0xD800), chr(0xD800) + chr(0xD801) + chr(0xD802),
        "x" + chr(0x1F600) + "y", "caf" + chr(0xE9)]
sys.stdout.write("|".join(rows))'
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
