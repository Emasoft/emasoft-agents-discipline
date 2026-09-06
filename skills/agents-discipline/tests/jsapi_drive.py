#!/usr/bin/env python3
"""Drive scripts/lib/jsapi.py on the oracle driver's corpus. Pair of tests/jsapi-drive.mjs."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from jsapi import (  # noqa: E402  # type: ignore[import-not-found]
    js_json_object, js_object_key_order, locale_compare_key, parse_date,
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

spec = [
    "2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00Z", "2020-01-01T00:00:00", "2020-01-01",
    "2020-01-01T24:00:00Z", "2020-01-01T24:00:00.000Z", "2020-01-01T24:00:01Z",
    "2020-01-01T24:01:00Z", "2020-01-01T25:00:00Z", "2020-01-01T00:60:00Z", "2020-01-01T00:00:60Z",
    "2020-01-01T00:00:00+02:00", "2020-01-01T00:00:00+0200", "2020-01-01T00:00:00-05:30",
    "2020-01-01T00:00:00+24:00", "2020-01-01T00:00:00+02:60",
    "2020-13-01T00:00:00Z", "2020-00-01T00:00:00Z", "2020-02-30T00:00:00Z", "2020-02-29T00:00:00Z",
    "2021-02-29T00:00:00Z", "2020-01-32T00:00:00Z", "2020-01-00T00:00:00Z",
    "Jan 1 2020", "2020", "2020-01", "2020-01-01T00:00:00.1234567Z", "2020-01-01T00:00:00.1Z",
    "+002020-01-01T00:00:00Z", "-002020-01-01T00:00:00Z", "2020-01-01t00:00:00z",
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

json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
