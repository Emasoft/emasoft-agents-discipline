#!/usr/bin/env bash
# Differential for the argument front end of gate-check.mjs vs scripts/gate_check.py.
#
# BLACK BOX, ON PURPOSE. gate-check.mjs is a top-level script that exports nothing, and the
# method line forbids editing the oracle to make it testable -- so the comparison runs both
# PROGRAMS and compares (exit code, stdout, stderr). That is stronger than a unit differential:
# it exercises the real sink, including the terminalSafe wrapper on console.error and the exact
# two-line failUsage shape.
#
# TWO CONTRACTS, ASSERTED SEPARATELY -- and the reason is a harness bug this file already had.
# The first version classified by exit code alone: "the oracle exits 0 or 2, so it stayed inside
# the argument layer and nothing has side effects". THAT IS FALSE. Exit 2 is documented as
# "usage/parse/infrastructure", so it also covers target discovery finding no ledger -- which
# happens AFTER the argument layer. Twelve vectors that both programs handled CORRECTLY were
# reported as divergences on that reasoning, because the oracle went on to fail at discovery
# while the port stopped at its porting boundary. Reasoning from an exit code back to a code
# path is exactly the vacuity this project keeps producing: the guard was satisfied by
# something other than the property it named.
#
#   reject_case  the argument layer MUST reject this. Asserts both exit 2 with byte-identical
#                stdout and stderr. A port exit of 90 here is a MISSED VALIDATION.
#   accept_case  the argument layer MUST pass this through. Asserts the port reaches 90, and
#                that the oracle's own message is NOT one of the argument layer's -- which is
#                what proves the oracle cleared it too, rather than assuming so.
#
# The accept half is not decoration: without it every vector could be rejected by the port for
# the wrong reason and the suite would still be green.
#
# CRASH GUARD: a Python traceback is reported as CRASH, never as DIVERGE. Every ad-hoc control
# loop written earlier in this port branched on an exit code alone, under which an import error
# reads as a genuine catch -- the defect that once scored a syntax error as nine catches.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORACLE="$HERE/../scripts/gate-check.mjs"
PORT="$HERE/../scripts/gate_check.py"
export PYTHONDONTWRITEBYTECODE=1

# Every message gate-check.mjs:85-179 can emit. Used ONLY by accept_case, to prove the oracle
# left the argument layer rather than inferring it from an exit code.
ARG_LAYER_RE='unknown option |duplicate option | needs a value|pipeline actions are mutually exclusive|--status and --reverify|--status never approves|cannot be combined with|explicit files and --scope|--leaf is only valid|execution options only|needs an integer from|does not exist: |is not a directory: |cannot inspect '

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/empty"
: > "$WORK/afile"

pass=0; fail=0; crash=0
declare -a FAILED=()

# Runs both programs on the given argv. Sets o_code/o_out/o_err and p_code/p_out/p_err.
_both() {
  o_out="$(cd "$WORK/empty" && node "$ORACLE" "$@" 2>"$WORK/o.err")"; o_code=$?
  o_err="$(cat "$WORK/o.err")"
  p_out="$(cd "$WORK/empty" && python3 -B "$PORT" "$@" 2>"$WORK/p.err")"; p_code=$?
  p_err="$(cat "$WORK/p.err")"
}

_crashed() {
  if printf '%s' "$p_err" | grep -q '^Traceback (most recent call last)'; then
    printf '  CRASH    %-42s %s\n' "$1" "$(printf '%s' "$p_err" | tail -1)"
    crash=$((crash + 1)); return 0
  fi
  return 1
}

reject_case() {
  local label="$1"; shift
  local o_out o_err o_code p_out p_err p_code
  _both "$@"
  _crashed "$label" && return
  if [ "$p_code" = 90 ]; then
    printf '  MISSED   %-42s port cleared the argument layer; oracle rejected it\n' "$label"
    printf '    oracle: %s\n' "$(printf '%s' "$o_err" | head -1)"
    fail=$((fail + 1)); FAILED+=("$label"); return
  fi
  if [ "$o_code" = "$p_code" ] && [ "$o_out" = "$p_out" ] && [ "$o_err" = "$p_err" ]; then
    pass=$((pass + 1)); return
  fi
  fail=$((fail + 1)); FAILED+=("$label")
  printf '  DIVERGE  %-42s exit %s/%s\n' "$label" "$o_code" "$p_code"
  [ "$o_out" != "$p_out" ] && { printf '    stdout oracle: %q\n' "$o_out"
                                printf '    stdout port  : %q\n' "$p_out"; }
  [ "$o_err" != "$p_err" ] && { printf '    stderr oracle: %q\n' "$o_err"
                                printf '    stderr port  : %q\n' "$p_err"; }
  return 0
}

accept_case() {
  local label="$1"; shift
  local o_out o_err o_code p_out p_err p_code
  _both "$@"
  _crashed "$label" && return
  if printf '%s' "$o_err" | grep -qE "$ARG_LAYER_RE"; then
    printf '  REJECTED %-42s oracle rejected it in the argument layer\n' "$label"
    printf '    oracle: %s\n' "$(printf '%s' "$o_err" | head -1)"
    fail=$((fail + 1)); FAILED+=("$label"); return
  fi
  if [ "$p_code" = 90 ]; then pass=$((pass + 1)); return; fi
  printf '  DIVERGE  %-42s port did not clear the argument layer (exit %s)\n' "$label" "$p_code"
  printf '    port  : %s\n' "$(printf '%s' "$p_err" | head -1)"
  fail=$((fail + 1)); FAILED+=("$label")
  return 0
}

# --help writes HELP straight to stdout, bypassing terminalSafe. The usage line names the
# running program in both (gate_lint.py:39 vs gate-lint.mjs:26 set that convention for this
# port), so the FIRST LINE is a deliberate difference and every other byte must match.
help_case() {
  local label="$1"; shift
  local o_out o_err o_code p_out p_err p_code
  _both "$@"
  _crashed "$label" && return
  local o_rest p_rest
  o_rest="$(printf '%s' "$o_out" | tail -n +2)"
  p_rest="$(printf '%s' "$p_out" | tail -n +2)"
  if [ "$o_code" = "$p_code" ] && [ "$o_rest" = "$p_rest" ] && [ "$o_err" = "$p_err" ] \
     && [ "$(printf '%s' "$o_out" | head -1)" = "usage: gate-check.mjs [options] [file ...]" ] \
     && [ "$(printf '%s' "$p_out" | head -1)" = "usage: gate_check.py [options] [file ...]" ]; then
    pass=$((pass + 1)); return
  fi
  printf '  DIVERGE  %-42s help body differs (exit %s/%s)\n' "$label" "$o_code" "$p_code"
  diff <(printf '%s\n' "$o_rest") <(printf '%s\n' "$p_rest") | head -10
  fail=$((fail + 1)); FAILED+=("$label")
  return 0
}

echo "gate-check argument front end -- oracle vs port"
echo "-- REJECT: the argument layer must refuse these, identically --"

help_case   "help long"                    --help
help_case   "help short"                   -h
help_case   "both help spellings"          --help -h
# NOT a help_case: parseArgs scans the WHOLE argv before main() ever tests opt.help, so an
# unknown option anywhere makes it return an error and --help never prints. Filed here as a
# reject to record that ordering, which is the opposite of what the name suggested when this
# row was written as a help_case and reported a false divergence.
reject_case "unknown option beats --help"  --help --bogus

reject_case "unknown long"                 --bogus
reject_case "unknown short"                -x
reject_case "unknown with ="               --bogus=1
reject_case "duplicate flag"               --status --status
reject_case "duplicate value opt"          --scope a --scope b
reject_case "duplicate mixed forms"        --scope=a --scope b
reject_case "value missing at end"         --scope
reject_case "value empty via ="            --scope=
reject_case "value empty via next"         --scope ""
reject_case "two pipeline actions"         --claim --release --scope s
reject_case "three pipeline actions"       --claim --release --list-scopes --scope s
reject_case "action plus --log"            --claim --log t --scope s
reject_case "status + reverify"            --status --reverify
reject_case "status + approve"             --status --approve
reject_case "action + run mode"            --claim --status --scope s
reject_case "action + files"               --claim f.md
reject_case "files + scope"                --scope s f.md
reject_case "leaf without claim/release"   --leaf x
reject_case "exec opt with action"         --claim --timeout 5 --scope s
reject_case "exec opt with status"         --status --jobs 2
reject_case "exec opt --shell with status" --status --shell /bin/sh
reject_case "exec opt --cwd with status"   --status --cwd .

# The numeric validators, at exactly the points Number() and float() part company.
reject_case "timeout underscore is NaN"    --timeout 1_000
reject_case "timeout fractional"           --timeout 1.5
reject_case "timeout zero"                 --timeout 0
reject_case "timeout negative"             --timeout -1
reject_case "timeout above range"          --timeout 86401
reject_case "timeout non-numeric"          --timeout abc
reject_case "timeout Infinity"             --timeout Infinity
# The three Unicode/underscore rows below each isolate a DIFFERENT hole, confirmed by mutation:
#   "١٢"     _JS_DECIMAL_RE using `\d` (Python matches every Unicode decimal digit; JS is
#            ASCII-only in every mode) -- this row alone reddens when [0-9] reverts to \d
#   "0x١٢"   int(digits, radix) accepting Unicode digits, which no try/except can catch
#            because int() does not consider them an error
#   "0x1_0"  int(digits, radix) accepting the PEP 515 underscore -- a SEPARATE acceptance from
#            the one above, so removing the guard for only one of them still reddens the other
reject_case "timeout unicode digits"       --timeout "١٢"
reject_case "timeout radix unicode digits" --timeout "0x١٢"
reject_case "timeout radix underscore"     --timeout "0x1_0"
reject_case "jobs zero"                    --jobs 0
reject_case "jobs above range"             --jobs 65
reject_case "jobs non-numeric"             --jobs abc

# The rejection message routes the value through JSON.stringify, so quoting is contractual.
reject_case "bad timeout with a quote"     --timeout 'a"b'
reject_case "bad timeout with a backslash" --timeout 'a\b'
reject_case "bad timeout non-ascii"        --timeout 'é'
reject_case "bad timeout with a newline"   --timeout "$(printf 'a\nb')"
reject_case "bad timeout with a tab"       --timeout "$(printf 'a\tb')"

# terminalSafe wraps console.error, so it applies to every message above.
reject_case "unknown opt with escape"      "$(printf -- '--a\033[31mb')"
reject_case "unknown opt with a bidi mark" "$(printf -- '--a‮b')"
reject_case "unknown opt with a C0 byte"   "$(printf -- '--a\001b')"

# asDirectory, and the ORDER of --root against --timeout: both exit 2, so only the message
# distinguishes them, and reordering the two calls would silently change which one a user sees.
reject_case "root missing"                 --root "$WORK/nope"
reject_case "root is a file"               --root "$WORK/afile"
reject_case "root checked before timeout"  --root "$WORK/nope" --timeout abc
reject_case "cwd missing"                  --cwd nope --root "$WORK/empty"
reject_case "cwd is a file"                --cwd "$WORK/afile" --root "$WORK/empty"
# ENOTDIR -- a FILE used as a path component. This is the ONLY row that reaches asDirectory's
# generic catch arm, where the port RECONSTRUCTS node's `CODE: description, stat 'path'` message
# byte by byte because CPython's OSError string shares almost nothing with it. Without this row
# that reconstruction is asserted by a docstring and tested by nothing: "root missing" is
# ENOENT (the special-cased arm) and "root is a file" never throws at all, since statSync
# succeeds on a file and it is isDirectory() that returns false.
reject_case "root under a file (ENOTDIR)"  --root "$WORK/afile/sub"

echo "-- ACCEPT: the argument layer must pass these through --"

# Number() accepts shapes float() rejects and vice versa; each of these must be ACCEPTED.
accept_case "timeout hex"                  --timeout 0x10 --root "$WORK/empty"
accept_case "timeout float-integral"       --timeout 5.0 --root "$WORK/empty"
accept_case "timeout exponent"             --timeout 1e3 --root "$WORK/empty"
accept_case "timeout at range top"         --timeout 86400 --root "$WORK/empty"
accept_case "timeout at range bottom"      --timeout 1 --root "$WORK/empty"
accept_case "timeout whitespace-padded"    --timeout "  12  " --root "$WORK/empty"
accept_case "jobs hex"                     --jobs 0x4 --root "$WORK/empty"
accept_case "jobs at range top"            --jobs 64 --root "$WORK/empty"
# `--` and a value that looks like a flag: both are accepted by parse_args, and the failure
# comes later. The oracle proves that by naming a FILE or a SCOPE in its message, not an option.
accept_case "-- makes a dashed file"       -- --status
accept_case "-- twice"                     -- -- --status
accept_case "value that looks like a flag" --scope --status
accept_case "= inside the value"           --log=a=b --scope s
accept_case "plain file argument"          somefile.md
accept_case "no arguments at all"          --root "$WORK/empty"

printf '\n  %d pass  %d fail  %d crash\n' "$pass" "$fail" "$crash"
if [ "$crash" -gt 0 ] || [ "$fail" -gt 0 ]; then
  [ "${#FAILED[@]}" -gt 0 ] && printf '  failed: %s\n' "${FAILED[*]}"
  exit 1
fi
echo "  argument front end agrees: rejects identically, and accepts the same vectors."
