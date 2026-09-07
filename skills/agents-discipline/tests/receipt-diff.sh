#!/usr/bin/env bash
# Differential for the RECEIPT: the content binding ledger-check writes back into the ledger.
#
# WHY THIS FILE EXISTS, and why no other suite could catch what it catches. `ledger-check.mjs:691`
# computes `text.replace(RECEIPT_RE,"\n").trimEnd() + "\n"` and the port's `body_for_hash =`
# assignment mirrors it. (Cited by NAME. The first version of this header said `ledger_check.py:774`
# and the fix's own comment block then pushed that line to 792 -- a citation invalidated by the
# commit that wrote it. Grep the symbol, not the line: `grep -n body_for_hash`.)
# The port shipped a BARE `.rstrip()` there -- the last bare strip in the file -- and JS
# `trimEnd()` and Python's argument-less `rstrip()` disagree on exactly six code points:
#
#   trimEnd() strips, bare rstrip() does NOT:   U+FEFF
#   bare rstrip() strips, trimEnd() does NOT:   U+001C U+001D U+001E U+001F U+0085
#
# THE DAMAGE IS NOT THE HASH. `body_for_hash` is also what the port WRITES BACK to the user's
# ledger (`fh.write(body_for_hash + stamp + "\n")`, mirroring the oracle's `:738`). So the two
# runtimes did not merely disagree about a digest -- they REWROTE the document differently: python
# deleted a trailing U+001C that node preserved, and preserved a trailing U+FEFF that node deleted.
# A command the user runs to VERIFY a file was editing it, differently per runtime.
#
# The visible symptom was milder and is what led to the discovery: sign under one runtime, re-check
# under the other, and the next run prints `receipt: STALE -- ledger content changed since it was
# last checked` on a file NO HUMAN touched. Not "nobody" -- the checker itself changed it, so the
# message is factually accurate and misattributes only by OMISSION: it names no agent, and the only
# agent the reader knows about is themselves. That line is a bare `console.log`/`print` and never
# reaches the exit code (`process.exit(complete ? 0 : 1)` / `sys.exit(0 if complete else 1)`), so
# it cannot fail a run. The rewrite is the defect; the stamp is the tell.
#
# WHY THIS SUITE IS THE ONE THAT CATCHES IT -- MEASURED BY MUTATION, not argued from coverage.
# Revert the fix (`.rstrip(JS_TRIM)` -> bare `.rstrip()`) and run everything:
#
#   only receipt-diff.sh reds.  The other 13 `*-diff.sh` stay green; `ledger-tests.mjs` passes
#   under BOTH `node` and `AD_RUNTIME=python`; `npm test` exits 0.
#
# That is the claim this file needs, and it is the one worth re-running if anyone doubts the suite
# earns its place. TWO EARLIER VERSIONS OF THIS COMMENT ARGUED IT THE WRONG WAY and both were
# wrong. The first reasoned from two named suites to a universal over all fourteen. The second
# replaced that with an enumeration built on `grep -l ledger.check tests/*-diff.sh` -- which
# measures NAME MENTION, not reachability: it misses a constructed name (`"$base-check.mjs"`), a
# glob over `scripts/*.mjs`, and any transitive call (notably `ledger-tests.mjs`, whose own name
# does not even match that pattern and which drives 11 cases with `rerun: true`). It also matched
# two suites on COMMENT lines. And its dismissal of `encoding-diff.sh` -- "its row is `pending`,
# and the re-run loop only executes `verified` rows" -- is a NON-SEQUITUR that measuring refuted:
# the signing gate is `rerunSkipped`, not the row status, and a `pending`-row ledger DOES get
# signed by both runtimes. A `pending` row means the loop had nothing to run; it never meant the
# run was skipped.
#
# The mutation result subsumes all of it: a suite nobody enumerated either reds or it does not,
# and the run says which. Reachability was never the property in question -- DETECTION was.
# `digest-diff.sh` is a different digest entirely: gate-check's APPROVAL key, not the ledger receipt.
#
# NOTE for anyone re-checking the oracle side of this: `ledger-check.mjs` carries a literal NUL at
# line 58, so `file` calls it binary and a BARE grep on it returns SILENT ZERO. Use `grep -a`.
# Every grep in this suite already does; a source-reading grep must too.
#
# Usage: bash tests/receipt-diff.sh   (run from skills/agents-discipline)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORACLE="$HERE/../scripts/ledger-check.mjs"
PORT="$HERE/../scripts/ledger_check.py"
NODE_ABS="$(command -v node)"
PY_ABS="$(command -v python3)"
[ -x "$NODE_ABS" ] && [ -x "$PY_ABS" ] || { echo "node and python3 are both required" >&2; exit 2; }
export PYTHONDONTWRITEBYTECODE=1

# EXPLICIT `unset`, not merely "we do not set it". This suite is the ONLY one that must reach the
# signing path, and that path is gated on the re-run not being skipped. Inheriting the variable
# from a parent shell (npm test, CI, an interactive export) would leave both runtimes writing NO
# receipt -- and two absent receipts compare equal, so every case would pass while proving nothing.
# That is the exact vacuity shape this repo has now shipped twice.
unset AGENTS_DISCIPLINE_SKIP_RERUN

WORK="$(mktemp -d)"
[ -n "$WORK" ] && [ -d "$WORK" ] || { echo "mktemp -d failed; refusing to build fixtures" >&2; exit 2; }
trap 'rm -rf "$WORK"' EXIT
# A real git repo: ledger-check walks up for `.git` to resolve cited artifacts, and the not-found
# fallback is a different code path from the one users hit.
git -C "$WORK" init -q . || { echo "git init failed" >&2; exit 2; }
mkdir -p "$WORK/app" && printf 'x\n' > "$WORK/app/stats.py"

pass=0; fail=0
declare -a FAILED=()

# Writes a ledger whose LAST line carries $2 (a printf byte escape) at the position $3 asks for:
#   tail -- immediately before the terminating newline, i.e. inside the trailing whitespace run
#   mid  -- in the middle of the prose, followed by letters, i.e. OUTSIDE the trailing run
#
# The acceptance is the non-runnable words `tests pass`: this suite deliberately runs WITHOUT
# SKIP_RERUN, so a fixture carrying a code span would have its contents EXECUTED. `_assert_inert`
# below enforces that rather than trusting this comment to survive the next edit.
_write() {
  local dest="$1" ws="$2" where="$3" pad
  pad="$(printf '%b' "$ws")"
  {
    printf '# Delegation plan\n'
    printf 'Units: 1\n\n'
    printf '| # | Unit | Files (mine) | Worker | Acceptance | Status |\n'
    printf '|---|------|--------------|--------|------------|--------|\n'
    printf '| 1 | stats | app/stats.py | worker-1 | tests pass | verified |\n'
    printf '\n## Evidence\n\n'
    # shellcheck disable=SC2016  # the backticks are a MARKDOWN code span in the fixture's own
    # text, not command substitution -- single quotes are what keeps them literal.
    if [ "$where" = mid ]; then
      printf '**Unit 1 —** ran the%ssuite by hand; see `app/stats.py`.\n' "$pad"
    else
      printf '**Unit 1 —** ran the suite by hand; see `app/stats.py`.%s\n' "$pad"
    fi
  } > "$dest"
}

# The fixture must contain NO code span outside the one citation, or the re-run loop executes it.
# `app/stats.py` is a bare path, not a command, so it is a citation and never runs -- but a future
# edit that adds a runnable span would silently turn this suite into a shell executor in CI.
_assert_inert() {
  local f="$1" spans
  # shellcheck disable=SC2016  # matching LITERAL backticks in the fixture; single quotes required.
  spans="$(grep -ao '`[^`]*`' "$f" | grep -av '^`app/stats.py`$' || true)"
  if [ -n "$spans" ]; then
    printf 'DIVERGE  fixture grew a code span the re-run loop would EXECUTE: %s\n' "$spans"
    exit 1
  fi
}

# Strip the stamp so two signings are comparable: the stamp embeds a wall-clock timestamp, which
# differs between two runs of the SAME runtime and would swamp the comparison.
_unstamped() { grep -av 'agents-discipline-check:' "$1"; }
_digest()    { grep -ao 'sha256:[0-9a-f]*' "$1" | tail -1 | cut -d: -f2; }

# One case. Signs a fresh copy with EACH runtime, then compares what they wrote.
#
# THE ASSERTION IS BYTE EQUALITY OF THE TWO SIGNED FILES, stamp removed -- deliberately not a grep
# for the word STALE. STALE is output wording three layers downstream of the property (digest ->
# PRIOR_RECEIPT match -> branch -> printed string): reword the message and a grep for it passes
# silently, and being a NEGATIVE grep it also passes when the command printed nothing at all. That
# is the proxy-assertion mistake this repo removed from whitespace-diff.sh in bb9042a, and it must
# not come back one file over. Byte equality asserts the write path, the hash input and RECEIPT_RE
# in a single comparison, and it is the property that actually matters: both runtimes must leave
# the user's ledger in the same state.
_case() {
  local name="$1" ws="$2" where="$3" n p nd pd
  n="$WORK/node-$(printf '%s' "$name" | tr -c 'A-Za-z0-9' '-').md"
  p="$WORK/py-$(printf '%s' "$name" | tr -c 'A-Za-z0-9' '-').md"
  _write "$n" "$ws" "$where"
  _write "$p" "$ws" "$where"
  _assert_inert "$n"
  # The two inputs must be identical BEFORE signing, or the comparison after it means nothing.
  if ! cmp -s "$n" "$p"; then
    printf 'DIVERGE  %-40s the two fixtures differ before signing; harness is broken\n' "$name"
    exit 1
  fi

  "$NODE_ABS" "$ORACLE" "$n" >/dev/null 2>&1
  "$PY_ABS" "$PORT" "$p" >/dev/null 2>&1

  # A receipt must actually have been WRITTEN, and be well formed. Without this the case passes on
  # two unsigned files: absent == absent. This is the control that fires if the signing path was
  # never reached -- a fixture that failed earlier, a read-only file (the oracle swallows that
  # write error by design), or SKIP_RERUN leaking back in.
  nd="$(_digest "$n")"; pd="$(_digest "$p")"
  if ! printf '%s' "$nd" | grep -qE '^[0-9a-f]{16}$' || ! printf '%s' "$pd" | grep -qE '^[0-9a-f]{16}$'; then
    printf 'DIVERGE  %-40s no well-formed receipt written; the signing path was never reached\n' "$name"
    printf '    node digest: [%s]   py digest: [%s]\n' "$nd" "$pd"
    exit 1
  fi

  if diff -a <(_unstamped "$n") <(_unstamped "$p") >/dev/null 2>&1 && [ "$nd" = "$pd" ]; then
    printf '  OK      %-40s both runtimes left the ledger in the same state\n' "$name"
    pass=$((pass + 1))
  else
    printf 'DIVERGE  %-40s the runtimes rewrote the ledger differently\n' "$name"
    printf '    digests : node=%s py=%s\n' "$nd" "$pd"
    printf '    content : %s\n' "$(diff -a <(_unstamped "$n") <(_unstamped "$p") 2>&1 | tr '\n' '|')"
    fail=$((fail + 1)); FAILED+=("$name")
  fi
}

# Six code points, each in the trailing whitespace run. Written as EXPLICIT UTF-8 BYTES for the
# same reason whitespace-diff.sh does: a literal would have to survive this file, git, and every
# editor in between.
#   U+FEFF -> ef bb bf     U+001C..U+001F -> 1c..1f     U+0085 -> c2 85
#
# NOTE the pad is followed by the line's terminating `\n`, which is whitespace to BOTH languages.
# That is not an oversight to be corrected by appending a space: a text file always ends in a
# newline, so `<divergent><newline>` IS the real-world shape, and the divergent byte is reachable
# by one strip loop and a wall to the other exactly as it would be mid-run.
_case "tail U+FEFF (BOM)"              '\xef\xbb\xbf' tail
_case "tail U+001C (file separator)"   '\x1c'         tail
_case "tail U+001D (group separator)"  '\x1d'         tail
_case "tail U+001E (record separator)" '\x1e'         tail
_case "tail U+001F (unit separator)"   '\x1f'         tail
_case "tail U+0085 (NEL)"              '\xc2\x85'     tail

# BOTH ORDERS of a divergent pair in one trailing run. Neither strip loop can pass these by
# accident: the outcome is decided by the RIGHTMOST divergent character, so an implementation that
# iterated the wrong set, or stripped the union of both sets, satisfies every single-character case
# above and fails here.
_case "tail U+FEFF then U+001C"        '\xef\xbb\xbf\x1c' tail
_case "tail U+001C then U+FEFF"        '\x1c\xef\xbb\xbf' tail

# NEGATIVE CONTROLS -- these must be IDENTICAL, and they are what stops the fix from over-reaching.
# A divergent byte MID-PROSE is followed by letters, so it is outside the trailing whitespace run
# and no correct implementation may treat it differently. If a "fix" ever strips these characters
# document-wide rather than at the tail, every case above still passes and these two red.
_case "mid-prose U+001C (must not differ)" '\x1c'         mid
_case "mid-prose U+FEFF (must not differ)" '\xef\xbb\xbf' mid

# And a trailing run of ONLY shared whitespace: no divergent byte anywhere, so the two runtimes
# must agree. Combined with the well-formed-digest assertion above, this is the case that proves
# the harness reaches the signing path at all -- a green suite whose every case skipped the
# receipt would fail here rather than reporting coverage it does not have.
_case "tail shared whitespace only"    '\x20\x09'     tail
_case "no pad at all (control)"        ''             tail

echo
if [ "$fail" = 0 ]; then
  # "apart from the stamp line" is not hedging -- `_unstamped` drops EVERY line matching
  # `agents-discipline-check:`, so the stamp's wording, prefix, placement and COUNT are all
  # unasserted; the only compared part of the receipt is its 16 hex digits. A reader of a suite
  # named receipt-diff.sh would otherwise assume the receipt line itself was compared.
  echo "--- $pass receipt case(s): ledgers byte-identical apart from the timestamped stamp line ---"
  exit 0
fi
printf -- '--- %s DIVERGENCE(S): %s ---\n' "$fail" "${FAILED[*]}"
exit 1
