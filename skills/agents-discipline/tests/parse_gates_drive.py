#!/usr/bin/env python3
"""Dump the PORT's parse_gates result as JSON, in the oracle driver's shape.

Pair of tests/parse-gates-drive.mjs. See that file for why the unread fields matter.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from gates import parse_gates, read_stable_regular_file  # noqa: E402  # type: ignore[import-not-found]

doc = parse_gates(read_stable_regular_file(sys.argv[1], label="gate ledger"))

# `_seen_attrs` is the port's own bookkeeping (a set, which JSON cannot encode) and has no
# counterpart in the oracle's gate objects. Dropped here rather than left to diverge: it is
# internal to the parse, and a caller that serialised a gate would otherwise carry it.
gates = [{k: v for k, v in g.items() if k != "_seen_attrs"} for g in doc["gates"]]

json.dump({
    "lines": doc["lines"],
    "eol": doc["eol"],
    "finalNewline": doc["finalNewline"],
    "gates": gates,
    # `.items()`, not the bare dict: the oracle's `abandoned` is a Map of id -> REASON, and
    # `[...map].sort()` yields [id, reason] pairs. `sorted(dict)` yields keys alone, which
    # made the port look like it had dropped every ABANDON reason. The defect was in this
    # driver; the port keeps them. A comparison harness that mis-serialises one side reports
    # a divergence that is its own.
    "abandoned": sorted(doc["abandoned"].items()),
    "owns": doc["owns"],
    "errors": doc["errors"],
    "warnings": doc["warnings"],
}, sys.stdout, indent=2)
sys.stdout.write("\n")
