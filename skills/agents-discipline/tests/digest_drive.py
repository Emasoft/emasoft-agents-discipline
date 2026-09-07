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
    "large integer": oracle(maxOutputBytes=9007199254740991),
    "empty strings": oracle(check="", expect="", path=""),
    "platform without version digits": oracle(platform="freebsd"),
}


def _js_numbers(value):
    """Render every FLOAT the way `JSON.stringify` would, before json.dumps sees it.

    `json.dumps` offers no hook for float formatting -- `default=` fires only for types it
    cannot handle at all -- so the conversion has to happen on the value. Two divergences,
    both MEASURED by this differential rather than assumed:

      1500.0        json.dumps writes `1500.0`, JSON.stringify writes `1500`. Caught by the
                    "float timeout" case, which is UNREACHABLE in production (timeoutValue
                    rejects non-integers) and kept precisely so the guard is proven to work.
      float("inf")  json.dumps writes `Infinity`, which is NOT VALID JSON. JSON.stringify
                    writes `null`. Same for NaN. This is worse than a byte difference: the
                    port would emit a document node cannot parse.

    Integral floats go through int(); the rest through `_js_number`, the project's existing
    Number::toString port, rather than a second copy of that logic here -- it already handles
    -0.0, the 1e-6/1e-4 exponential-notation threshold mismatch, and values above 2**53.
    """
    if isinstance(value, dict):
        return {k: _js_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_js_numbers(v) for v in value]
    # bool BEFORE float: bool is a subclass of int in Python, and True would otherwise be
    # rendered as the number 1.
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None                          # JSON.stringify(Infinity) === "null"
        # ONE path, not two. An `if value.is_integer(): return int(value)` fast path sat here
        # and was DELETED after a mutation control could not make it redden: `_js_number(1500.0)`
        # already returns "1500", which re-parses to the int 1500, so the branch was redundant
        # with the line below rather than covering anything. A branch no mutation isolates does
        # not earn its place -- this project's own rule, applied to code written five minutes
        # earlier.
        return json.loads(_js_number(value))     # exact JS spelling, re-parsed as a number
    return value


def js_stringify(value):
    """`JSON.stringify(value)` for the shapes an oracle contains.

    Settings, each matching a documented JSON.stringify behaviour json.dumps gets wrong:
      separators=(",", ":")  -- json.dumps defaults to ", " and ": ".
      ensure_ascii=False     -- json.dumps ESCAPES non-ASCII by default; JSON.stringify never
                                does. This changes the most bytes and is the easiest to miss,
                                because ASCII-only test data hides it completely.
      sort_keys is NOT set   -- insertion order is what JSON.stringify emits, and sorting here
                                would silently reorder the hashed bytes.
      allow_nan=False        -- so a non-finite that somehow escapes _js_numbers RAISES instead
                                of emitting the invalid `Infinity` token. Fail fast; do not
                                write a document the oracle cannot read.
    """
    return json.dumps(_js_numbers(value), separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


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
