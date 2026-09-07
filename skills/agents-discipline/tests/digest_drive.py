#!/usr/bin/env python3
"""Python mirror of digest-drive.mjs. See that file for what this pair does and does not test.

THE POINT OF THIS FILE is that a naive `json.dumps(obj)` is WRONG here in at least two ways,
and both are silent: it writes `", "` / `": "` where `JSON.stringify` writes `,` / `:`, and it
escapes non-ASCII where `JSON.stringify` emits it raw. Either changes every byte of the digest
and therefore every approval token. The serializer below is written to match the oracle, and
the differential is what proves it does.

Non-printing characters are built with chr(), never written literally -- same reason as the
oracle driver: a writing layer already turned escape sequences into a raw U+2028 once.
"""
import sys

# BEFORE any local import -- a stale .pyc validates on (source mtime, source size) alone.
sys.dont_write_bytecode = True

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402

# Mirrors gates.py's _LONE_SURROGATE_RE. Deliberately duplicated rather than imported: this
# driver's whole job is to reproduce JSON.stringify INDEPENDENTLY of the code under test, so
# importing the production helper would make the differential compare gates.py with itself.
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "lib"))

from jsapi import _js_number  # noqa: E402,I001  # type: ignore[import-not-found]


def oracle(**over):
    """The twelve fields in gate-check.mjs:340-356 order.

    A Python dict preserves insertion order and `json.dumps` emits it, which is what makes the
    key ORDER hazard a non-issue HERE -- but only because these literals are written in the
    oracle's order. A port that builds this dict from anything unordered (a set, **kwargs
    merged differently, a comprehension over sorted keys) reorders the hashed bytes.
    """
    value = {
        "schema": 1,
        "check": "echo ok",
        "expect": "ok",
        "cwd": "/repo/packages/api",
        "shell": "/bin/bash",
        "timeoutMs": 120000,
        "maxOutputBytes": 1048576,
        "regexTimeoutMs": 250,
        "regexStartupTimeoutMs": 5000,
        "maxRegexWorkers": 4,
        "platform": "darwin",
        "path": "/usr/bin:/bin",
    }
    value.update(over)
    return value


# Same cases, same order, same values as digest-drive.mjs's CASES.
CASES = {
    "CONTROL baseline": oracle(),
    "non-ascii check": oracle(check="echo na" + chr(0xEF) + "ve"),
    # chr(0x1F600) directly -- Python strings are code points, where JS needed a surrogate PAIR
    # (0xd83d, 0xde00) for the same character. The two spellings must produce the SAME bytes.
    "emoji beyond the BMP": oracle(check="echo " + chr(0x1F600)),
    # See digest-drive.mjs for what this row closes: the port CRASHED here where the oracle
    # returned a digest. chr(0xD800) is a lone HIGH surrogate -- in the .mjs it is CH(0xd800),
    # one UTF-16 code unit; here it is one code point. The two spellings denote the same
    # abstract string, which is the only thing that matters, because both drivers hand it to
    # the SAME serialization contract and compare DIGESTS, not source bytes.
    "lone surrogate": oracle(check="echo " + chr(0xD800)),
    # See digest-drive.mjs: the "absent path" row that stood here was WRONG and is gone.
    # gate-check.mjs:325 is `String(process.env.PATH || "")`, so an unset PATH becomes the empty
    # STRING and the key is never dropped -- the row tested a shape the oracle cannot emit.
    "empty path (unset PATH becomes the empty string, not null)": oracle(path=""),
    "null path (serializer control -- null is not empty-string)": oracle(path=None),
    # See digest-drive.mjs: `shell` carries the SAME `opt.status ? null : ...` ternary
    # (gate-check.mjs:324), and the corpus had the same gap -- every row "/bin/bash", none null.
    "null shell (serializer control -- same ternary as path)": oracle(shell=None),
    "both ambient fields null (serializer control)": oracle(shell=None, path=None),
    "control chars": oracle(check="a" + chr(1) + "b" + chr(31) + "c"),
    "tab and newline": oracle(check="a" + chr(9) + "b" + chr(10) + "c"),
    "quote and backslash": oracle(check='a"b\\c'),
    "line separator U+2028": oracle(expect="a" + chr(0x2028) + "b"),
    "paragraph separator U+2029": oracle(expect="a" + chr(0x2029) + "b"),
    "float timeout (unreachable today)": oracle(timeoutMs=1500.0),
    # See digest-drive.mjs: json.dumps would write the invalid token `Infinity` here.
    "infinity becomes null": oracle(timeoutMs=float("inf")),
    "nan becomes null": oracle(timeoutMs=float("nan")),
    "negative zero": oracle(timeoutMs=-0.0),
    # See digest-drive.mjs: the non-integral floats are the gap that let a real defect through.
    "float exponent spelling": oracle(timeoutMs=1e-7),
    "float at the notation threshold": oracle(timeoutMs=0.000001),
    "float non-integral": oracle(timeoutMs=1.5),
    "large integer": oracle(maxOutputBytes=9007199254740991),
    "empty strings": oracle(check="", expect="", path=""),
    "platform without version digits": oracle(platform="freebsd"),
}


def _js_value(value):
    """One value, spelled exactly as `JSON.stringify` spells it.

    NUMBERS ARE EMITTED AS TEXT AND NEVER ROUND-TRIPPED, which is the whole reason this
    function exists instead of a `json.dumps` call with clever arguments. The previous version
    did `json.loads(_js_number(value))` -- producing the correct JS spelling and then throwing
    it away, because `json.dumps` re-renders the resulting float with Python's repr. MEASURED,
    and it is wrong for exactly the cases `_js_number` was written to fix:

        1e-7       JS `1e-7`      round-tripped `1e-07`   (Python zero-pads the exponent)
        0.000001   JS `0.000001`  round-tripped `1e-06`   (the two switch to exponential
                                                           notation at DIFFERENT thresholds)

    The corpus could not catch it: its only float case was `1500.0`, which is INTEGRAL, so
    `_js_number` returned "1500" and the round trip happened to survive. A control proved the
    line did something; it did not prove the line was right. `_js_number`'s output is now the
    output.

    SCOPE, stated rather than implied: this handles the shapes `oracle()` actually contains --
    a flat dict of str/int/float. It is NOT a general JSON.stringify: no `undefined`/function
    dropping, no `toJSON()`, no nesting. `oracle()` (gate-check.mjs:340-356) returns twelve
    flat str/int fields, so those paths are unreachable there; anything else raises rather
    than guessing.
    """
    # None -> null. The docstring above said oracle() "returns twelve flat str/int fields", and
    # that was FALSE: gate-check.mjs:325 is `opt.status ? null : String(process.env.PATH || "")`,
    # so `path` is genuinely null under --status. This function used to RAISE on it
    # ("oracle field of unsupported type 'NoneType'"), which is the right default for an
    # unmodelled type and the wrong answer for one the oracle actually emits. Found by adding
    # the row, not by reading the docstring.
    if value is None:
        return "null"
    # bool BEFORE int: bool subclasses int in Python, so True would render as 1.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # json.dumps is CORRECT for strings -- the corpus proves it on control characters,
        # quote/backslash, U+2028/U+2029, non-ASCII and an astral character. ensure_ascii=False
        # because JSON.stringify never escapes non-ASCII and json.dumps escapes it by default.
        #
        # EXCEPT for a LONE SURROGATE, and this driver had the SAME defect as the production
        # code it exists to check: ensure_ascii=False leaves it raw, and the encode at the
        # bottom of this file then raises UnicodeEncodeError where the oracle returns a digest.
        # Adding the row made the driver crash, which is how the second copy of the bug was
        # found -- the fix in gates.py alone would have looked complete.
        # JSON.stringify escapes it to the six ASCII characters \\ud800, so this does too.
        return _LONE_SURROGATE_RE.sub(lambda m: "\\u%04x" % ord(m.group(0)),
                                      json.dumps(value, ensure_ascii=False))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "null"                        # JSON.stringify(Infinity) === "null"
        return _js_number(value)                 # verbatim; see the docstring
    raise TypeError("oracle field of unsupported type %r: %r" % (type(value).__name__, value))


def js_stringify(value):
    """`JSON.stringify(obj)` for a FLAT oracle object.

    Built by hand rather than delegated to `json.dumps` because there is no hook for float
    formatting and the digest must be byte-exact. Key ORDER is insertion order, which is what
    JSON.stringify emits -- nothing here sorts, and sorting would silently reorder the hashed
    bytes.
    """
    if not isinstance(value, dict):
        raise TypeError("js_stringify expects the flat oracle object, got %r" % type(value).__name__)
    body = ",".join(json.dumps(k, ensure_ascii=False) + ":" + _js_value(v) for k, v in value.items())
    return "{" + body + "}"


answers = {}
for name, value in CASES.items():
    text = js_stringify(value)
    answers[name] = {
        "json": text,
        # utf-8, matching the oracle's `.update(json, "utf8")`.
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }

json.dump(answers, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
