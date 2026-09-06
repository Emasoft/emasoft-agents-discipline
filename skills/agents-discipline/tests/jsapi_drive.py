#!/usr/bin/env python3
"""Drive scripts/lib/jsapi.py on the oracle driver's corpus. Pair of tests/jsapi-drive.mjs."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from jsapi import (  # noqa: E402  # type: ignore[import-not-found]
    js_json_object, js_length, js_object_key_order, js_slice, js_trim, locale_compare_key,
    parse_date,
)

out = []

objects = [
    ("mixed", ["b", "10", "a", "2", "01", "-1"]),
    ("all-numeric", ["3", "1", "20", "0"]),
    ("all-string", ["w2", "w1", "alpha"]),
    ("boundary", ["4294967294", "4294967295", "4294967296", "0"]),
    ("idlike", ["1", "1.0", "1e1", "+1", " 1"]),
]
for name, keys in objects:
    obj = {k: 1 for k in keys}
    out.append(["keys " + name, js_object_key_order(list(obj))])
    # separators match JSON.stringify's compact form; the point of the row is KEY ORDER.
    out.append(["stringify " + name,
                json.dumps(js_json_object(obj), separators=(",", ":"), ensure_ascii=False)])

alphabet = list("-._0123456789abzABZ")
ids = []
for a in alphabet:
    ids.append(a)
    for b in alphabet:
        ids.append(a + b)
out.append(["localeCompare", sorted(ids, key=locale_compare_key)])
out.append(["localeCompare pairs",
            sorted(["a", "A", "aB", "Ab", "aa", "aA", "Aa", "AA", "a1", "A1", "1a"],
                   key=locale_compare_key)])
STRINGS = ["", "a", "aé", "😀", "😀😀", "a😀b", "\U0001F600\U0001F601\U0001F602", "ⓐ", "😀" * 200]
out.append(["js_length", [js_length(s) for s in STRINGS]])
for a, b in [(0, 1), (0, 2), (0, 3), (1, 3), (0, 500), (-3, None), (3, 1), (0, 0), (-99, 99)]:
    # Code POINTS of the result, not the string: a slice can end on a lone surrogate, which
    # json.dump cannot serialise on either side. The code points compare exactly and survive.
    out.append([f"js_slice {a},{b if b is not None else 'undefined'}",
                [[ord(c) for c in js_slice(s, a, b)] for s in STRINGS]])
out.append(["localeCompare punctuation",
            sorted(["a-B", "aB", "a.b", "ab", "a-b", "a_B", "w1.retry-2", "w1.retry-1"],
                   key=locale_compare_key)])
out.append(["localeCompare triples",
            sorted(["aAb", "AaB", "aab", "AAB", "aAB", "Aab", "abA", "aBa"],
                   key=locale_compare_key)])
out.append(["stringify nested in array",
            json.dumps(js_json_object({"waves": [{"b": 1, "10": 2, "a": 3}], "meta": {"z": 1, "2": 2}}),
                       separators=(",", ":"), ensure_ascii=False)])

spec = [
    "2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00Z", "2020-01-01T00:00:00", "2020-01-01",
    "2020-01-01T24:00:00Z", "2020-01-01T24:00:00.000Z", "2020-01-01T24:00:01Z",
    "2020-01-01T24:01:00Z", "2020-01-01T25:00:00Z", "2020-01-01T00:60:00Z", "2020-01-01T00:00:60Z",
    "2020-01-01T00:00:00+02:00", "2020-01-01T00:00:00+0200", "2020-01-01T00:00:00-05:30",
    "2020-01-01T00:00:00+24:00", "2020-01-01T00:00:00+02:60",
    "2020-13-01T00:00:00Z", "2020-00-01T00:00:00Z", "2020-02-30T00:00:00Z", "2020-02-29T00:00:00Z",
    "2021-02-29T00:00:00Z", "2020-01-32T00:00:00Z", "2020-01-00T00:00:00Z",
    "Jan 1 2020", "2020", "2020-01", "2020-01-01T00:00:00.1234567Z", "2020-01-01T00:00:00.1Z",
    "+002020-01-01T00:00:00Z", "-002020-01-01T00:00:00Z", "-000000-01-01T00:00:00Z", "+000000-01-01T00:00:00Z", "2020-01-01t00:00:00z",
    "2020-01-01T00:00:00.000z", "2020-01-01 00:00:00", "2020-01-01T00:00", "2020-01-01T00",
    "", " ", "not a date", "2020-01-01T00:00:00.000Z ", "1970-01-01T00:00:00Z",
    "1969-12-31T23:59:59Z", "275760-09-13T00:00:00Z", "2020-1-1T00:00:00Z",
]
# See the oracle driver: these two stay IN the corpus and are recorded with both answers, so the
# declared divergence is visible in the compared output instead of absent from it.
DECLARED_NON_ISO = {"Jan 1 2020", "2020-01-01 00:00:00"}
for value in spec:
    parsed = parse_date(value)
    if value in DECLARED_NON_ISO:
        # Each driver reports its OWN answer under the marked key. Writing the other runtime's
        # number in here would be fabricating a measurement -- and it is local-time dependent,
        # so it would also be machine-specific. python-lib-checks excludes exactly these keys
        # from the equality check and asserts the exception's SHAPE instead: oracle a number,
        # port None. The divergence is therefore stated once, in the place that can verify it.
        out.append(["Date.parse (declared non-ISO) " + json.dumps(value, ensure_ascii=False),
                    parsed])
    else:
        out.append(["Date.parse " + json.dumps(value, ensure_ascii=False), parsed])

# trim(): the same probe as the oracle's. js_trim, NOT str.strip() -- str.strip() answers True
# for the five separators the oracle leaves alone and False for U+FEFF, so a port built on it
# diverges on 6 of these 24 rows. That was a shipped defect, not a hypothetical: an abandon
# reason of one U+FEFF exited 2 in the oracle and 0 in the port.
for _cp in (
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0, 0x1680, 0x2000, 0x200A, 0x2028, 0x2029,
    0x202F, 0x205F, 0x3000, 0xFEFF, 0x1C, 0x1D, 0x1E, 0x1F, 0x180E, 0x200B, 0x61,
):
    _c = chr(_cp)
    out.append(["trim U+" + format(_cp, "04x"), js_trim(_c + "a" + _c) == "a"])

json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
