#!/usr/bin/env python3
"""Dump the PORT's parse_gates result as JSON, in the oracle driver's shape.

Pair of tests/parse-gates-drive.mjs. See that file for why the unread fields matter.
"""

import sys
# BEFORE any local import. A stale .pyc in scripts/lib/__pycache__ once executed while
# inspect.getsource() read the CORRECTED .py -- so the source looked right, every branch
# condition evaluated true, and the output was still wrong, and two runs of "the same"
# code disagreed. Not writing bytecode for these modules removes the failure mode.
sys.dont_write_bytecode = True
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from gates import (  # noqa: E402  # type: ignore[import-not-found]
    automatic_evidence_prefix, classify_gate_evidence, gate_definition_digest, gate_state,
    parse_gates, read_stable_regular_file, tail,
)

doc = parse_gates(read_stable_regular_file(sys.argv[1], label="gate ledger"))

# No key-stripping. An earlier version dropped a `_seen_attrs` set the port had put on every
# gate dict, which made the shapes match while leaving the real difference in place — the
# oracle's gate objects have no such field, and a set is not JSON-encodable, so any consumer
# that serialised a gate would crash. Fixed in parse_gates instead; the driver now compares
# the gates as they actually are, which is the only way it can see that kind of divergence.
# Mirror of the oracle driver's synthetic corpus. Built from chr() code points, never typed:
# these are exactly the invisible characters that decide the verdicts below, and a corpus that
# loses them asserts nothing. See tests/parse-gates-drive.mjs for what each row is for.
_RUNNABLE = {"id": "s", "checked": True, "check": "echo ok", "expect": "ok", "cwd": None}
_OK_PREFIX = automatic_evidence_prefix(gate_definition_digest(_RUNNABLE))
_BODY = " exit=0; EXPECT=matched; output-sha256=" + "a" * 64 + "; output-bytes=0; shell="
_SYNTHETIC = []
for _name, _evidence in [
    ["absent", None], ["blank", ""], ["pending", "pending"], ["PENDING upper", "PENDING"],
    ["pending trailing newline", "pending\n"],
    ["pending leading space", " pending"],
    ["falsy zero", 0], ["falsy false", False],
    ["current", _OK_PREFIX + _BODY + "x"],
    ["shell=LF", _OK_PREFIX + _BODY + "\n"],
    ["shell=CR", _OK_PREFIX + _BODY + "\r"],
    ["shell=LS U+2028", _OK_PREFIX + _BODY + chr(0x2028)],
    ["shell=PS U+2029", _OK_PREFIX + _BODY + chr(0x2029)],
    ["shell=astral", _OK_PREFIX + _BODY + chr(0x1F600)],
    ["bytes over cap", _OK_PREFIX + _BODY.replace("output-bytes=0", "output-bytes=1048577") + "x"],
    ["bytes at cap", _OK_PREFIX + _BODY.replace("output-bytes=0", "output-bytes=1048576") + "x"],
    ["wrong digest", automatic_evidence_prefix("b" * 64) + _BODY + "x"],
    ["stale automatic", "automatic-evidence=v1; something else"],
    ["stale exit0", "exit=0; shell=sh"],
    ["human", "I ran it and it worked"],
    ["long astral over cap", _OK_PREFIX + _BODY + "x" + chr(0x1F600) * 451],
    ["long ascii over cap", _OK_PREFIX + _BODY + "x" * 900],
    ["coerce NaN", float("nan")], ["coerce empty object", {}], ["coerce empty array", []],
    ["coerce array of one", [1]], ["coerce true", True], ["coerce float", 1.0],
    ["coerce zero", 0], ["coerce false", False], ["coerce str zero", "0"],
    ["coerce list pending", ["pending"]],
    ["coerce list stale", ["automatic-evidence=v1; x"]],
    ["coerce list nested", [["a"], None, 2]],
]:
    _gate = dict(_RUNNABLE, evidence=_evidence)
    _SYNTHETIC.append([_name, {
        "evidence": classify_gate_evidence(_gate),
        "state": gate_state(_gate, {}),
        "stateAbandoned": gate_state(_gate, {"s": "why"}),
        "unchecked": gate_state(dict(_gate, checked=False), {}),
        "nonRunnable": gate_state(dict(_gate, check=""), {}),
    }])
for _name, _value in [
    ["empty", ""], ["blank lines", "\n\n  \n"], ["one line", "hello"],
    ["three lines", "a\nb\nc"], ["crlf", "a\r\nb\r\nc"],
    ["trailing blank", "a\nb\n\n"], ["padded", "  a  \n  b  "],
    ["over 240", "x" * 300],
    ["astral over 240", chr(0x1F600) * 200],
    ["u2028 inside", "a" + chr(0x2028) + "b"],
    ["nel inside", "a" + chr(0x85) + "b"],
    ["vertical tab", "a" + chr(0x0B) + "b"],
    ["bom padded line", chr(0xFEFF) + "a" + chr(0xFEFF) + "\nb"],
    ["nel padded line", "a" + chr(0x85) + "\nb"],
]:
    _SYNTHETIC.append(["tail " + _name, tail(_value)])
_SYNTHETIC.append(["state no id", gate_state(
    {"checked": True, "check": "echo ok", "expect": "ok", "cwd": None,
     "evidence": "pending"}, {})])

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
    "verdicts": [{"id": g["id"], "evidence": classify_gate_evidence(g),
                  "state": gate_state(g, doc["abandoned"])} for g in gates],
    "synthetic": _SYNTHETIC,
    # ensure_ascii=False: json.dumps escapes non-ASCII to \uXXXX by default and
    # JSON.stringify does not, so a ledger with a non-Latin title diverged on the SERIALISER
    # rather than on the parse. Measured: "тесты пройдены ✓" came back as те...
    # The four ledgers this landed with are all ASCII, which is why it never surfaced —
    # a harness that is only ever fed the inputs it was written against.
}, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
