#!/bin/bash
# Compare `Number(str)` against jsapi.js_to_number, case by case.
#
# gate-check.mjs coerces argv with Number() before range-checking it (timeoutValue :130,
# jobCount :139), so a port using float() changes WHICH COMMAND LINES ARE ACCEPTED --
# `--timeout 0x10` is 16 seconds to the oracle and a crash to float(). This is the first
# gate_check.py dependency and it had no port at all.
#
# The corpus contains EVERY case js_to_number's docstring names, per the STATE block's
# docstring-cases rule, plus the radix and sign shapes that rule does not reach.
#
# Usage: bash tests/tonumber-diff.sh   (run from skills/agents-discipline)
set -u
export PYTHONDONTWRITEBYTECODE=1

# One case per line, TAB-free, so the two drivers read the same list. Written as a heredoc of
# JSON strings: that is the only spelling that survives a shell, a node arg and a python arg
# without any of the three reinterpreting a backslash or a quote.
cat > /tmp/tonumber-cases.json <<'JSON'
["12", "", " ", "  12  ", "0x10", "0X10", "0b11", "0o17", "0x", "-0x10",
 "1e3", "1E3", "1e", "1e+3", "1e-3", "Infinity", "+Infinity", "-Infinity", "infinity", "inf",
 "12abc", "1_000", "1,000", "1.5", ".5", "5.", "+7", "-7", "--5", "0", "00", "NaN", "abc",
 "\t\n 3 \r", "9007199254740993", "1.7976931348623157e309"]
JSON

node -e '
const cases = require("/tmp/tonumber-cases.json");
// String(Number(x)) rather than the raw number: NaN and Infinity have no JSON spelling, and
// the STRING is what a divergence report has to show anyway.
console.log(JSON.stringify(cases.map((c) => String(Number(c)))));
' > /tmp/tonumber-js.json 2>/tmp/tonumber-js.err; jrc=$?

python3 - > /tmp/tonumber-py.json 2>/tmp/tonumber-py.err <<'PY'; prc=$?
import json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join("scripts", "lib"))
from jsapi import _js_number, js_to_number

cases = json.load(open("/tmp/tonumber-cases.json"))


def one(case):
    """Per-case, so an exception becomes a DIVERGING VALUE rather than a dead driver.

    `Number()` never throws -- it answers NaN. A port that raises has therefore already
    diverged, and the comparison should SAY SO on that row instead of taking the whole run
    down. This repo's own rule is that a crash is not a catch: mutate-probe.sh refuses to
    score one, because a mutant that breaks the harness proved nothing.

    MEASURED: it matters here. Removing the decimal-regex guard makes `float("-0x10")` raise,
    and `-0x10` sorts before `1_000` in the corpus -- so the crash PREEMPTED the divergence
    the later row would have shown, and the control reported no finding at all.
    """
    try:
        return _js_number(js_to_number(case))
    except Exception as error:                       # noqa: BLE001 -- any raise is the finding
        return "RAISED: %s: %s" % (type(error).__name__, error)


# _js_number is Number::toString -- the same String() the oracle applies, so the comparison is
# of the two coercions and NOT of two different number-formatting conventions.
print(json.dumps([one(c) for c in cases]))
PY

if [ $jrc != 0 ] || [ $prc != 0 ]; then
  echo "CRASH   tonumber (js=$jrc py=$prc)"; head -5 /tmp/tonumber-js.err; tail -5 /tmp/tonumber-py.err; exit 1
fi

python3 - <<'PY'
import json, sys

cases = json.load(open("/tmp/tonumber-cases.json"))
js = json.load(open("/tmp/tonumber-js.json"))
py = json.load(open("/tmp/tonumber-py.json"))

if not (len(cases) == len(js) == len(py)):
    print("VACUOUS: lengths differ (%d/%d/%d)" % (len(cases), len(js), len(py))); sys.exit(1)
if len(cases) < 30:
    print("VACUOUS: only %d cases" % len(cases)); sys.exit(1)
# The corpus must DISCRIMINATE, not merely be long: if every case produced the same answer the
# comparison would pass against a constant-returning port. Same gate as lease-diff.sh's.
if len(set(js)) < 5:
    print("VACUOUS: the oracle returns only %d distinct values" % len(set(js))); sys.exit(1)

bad = [(c, a, b) for c, a, b in zip(cases, js, py) if a != b]
for c, a, b in bad:
    print("DIVERGE  %-24r js=%-10s py=%s" % (c, a, b))
if bad:
    print()
    print("--- %d of %d DIVERGE ---" % (len(bad), len(cases))); sys.exit(1)
print("--- %d cases, %d distinct results, identical ---" % (len(cases), len(set(js))))
PY
