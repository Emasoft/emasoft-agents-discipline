#!/usr/bin/env python3
"""Dump the PORT's parse_gates result as JSON, in the oracle driver's shape.

Pair of tests/parse-gates-drive.mjs. See that file for why the unread fields matter.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from gates import (  # noqa: E402  # type: ignore[import-not-found]
    automatic_evidence_prefix, gate_definition_digest, parse_gates, read_stable_regular_file,
)

doc = parse_gates(read_stable_regular_file(sys.argv[1], label="gate ledger"))

# No key-stripping. An earlier version dropped a `_seen_attrs` set the port had put on every
# gate dict, which made the shapes match while leaving the real difference in place — the
# oracle's gate objects have no such field, and a set is not JSON-encodable, so any consumer
# that serialised a gate would crash. Fixed in parse_gates instead; the driver now compares
# the gates as they actually are, which is the only way it can see that kind of divergence.
gates = doc["gates"]


def _digest_row(gate):
    digest = gate_definition_digest(gate)
    try:
        prefix = None if digest is None else automatic_evidence_prefix(digest)
    except ValueError as error:
        prefix = "threw: " + str(error)
    return {"id": gate["id"], "digest": digest, "prefix": prefix}


digests = [_digest_row(gate) for gate in gates]

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
    "digests": digests,
    # ensure_ascii=False: json.dumps escapes non-ASCII to \uXXXX by default and
    # JSON.stringify does not, so a ledger with a non-Latin title diverged on the SERIALISER
    # rather than on the parse. Measured: "тесты пройдены ✓" came back as те...
    # The four ledgers this landed with are all ASCII, which is why it never surfaced —
    # a harness that is only ever fed the inputs it was written against.
}, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
