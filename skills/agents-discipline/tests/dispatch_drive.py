#!/usr/bin/env python3
"""Drive the PORT's dispatch state machine, in the oracle driver's shape.

Pair of tests/dispatch-drive.mjs. A fixed `now` is threaded through every transition so the two
runtimes produce byte-identical state files; without it every row diverges on the clock.
"""

import sys
# BEFORE any local import. A stale .pyc in scripts/lib/__pycache__ once executed while
# inspect.getsource() read the CORRECTED .py -- so the source looked right, every branch
# condition evaluated true, and the output was still wrong, and two runs of "the same"
# code disagreed. Not writing bytecode for these modules removes the failure mode.
sys.dont_write_bytecode = True
import errno
import json
import re
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from dispatch import (  # noqa: E402  # type: ignore[import-not-found]
    dispatch_state_path, dispatch_status, get_dispatch_wave, update_dispatch,
)

root = sys.argv[1]
out = []


def redact(value):
    return str(value).replace(os.path.realpath(root), "<ROOT>").replace(root, "<ROOT>")


def record(name, value):
    out.append([redact(name), value])


def attempt(name, fn):
    try:
        record(name, {"ok": fn()})
    except Exception as error:  # noqa: BLE001 -- the oracle's catch is equally wide
        number = getattr(error, "errno", None)
        if number is not None:
            record(name, {"oserror": errno.errorcode.get(number, str(number))})
        else:
            record(name, {"error": redact(error)})


def T(n):
    return f"2024-03-0{n}T12:00:00.000Z"


def up(spec):
    return update_dispatch(root, spec)


attempt("open", lambda: up({"scope": "api", "wave": "1", "action": "open", "leaves": ["a", "b"], "now": T(1)}))
attempt("start a", lambda: up({"scope": "api", "wave": "1", "action": "start", "leaf": "a", "handle": "h-a", "now": T(2)}))
attempt("start b", lambda: up({"scope": "api", "wave": "1", "action": "start", "leaf": "b", "handle": "h-b", "now": T(2)}))
attempt("seal", lambda: up({"scope": "api", "wave": "1", "action": "seal", "now": T(3)}))
attempt("return a", lambda: up({"scope": "api", "wave": "1", "action": "return", "leaf": "a", "now": T(4)}))
attempt("return b", lambda: up({"scope": "api", "wave": "1", "action": "return", "leaf": "b", "now": T(5)}))
with open(dispatch_state_path(root, "api"), "r", encoding="utf-8") as handle:
    record("state file", handle.read())

attempt("reopen", lambda: up({"scope": "api", "wave": "1", "action": "open", "leaves": ["a"], "now": T(6)}))
attempt("unknown wave", lambda: up({"scope": "api", "wave": "nope", "action": "seal", "now": T(6)}))
attempt("open no leaves", lambda: up({"scope": "api", "wave": "w2", "action": "open", "leaves": [], "now": T(1)}))
attempt("open dup leaf", lambda: up({"scope": "api", "wave": "w2", "action": "open", "leaves": ["a", "a"], "now": T(1)}))
attempt("bad scope", lambda: up({"scope": "-bad", "wave": "w2", "action": "open", "leaves": ["a"], "now": T(1)}))
attempt("nonstring wave", lambda: up({"scope": "api", "wave": 7, "action": "open", "leaves": ["a"], "now": T(1)}))
attempt("bad action", lambda: up({"scope": "api", "wave": "1", "action": "explode", "now": T(6)}))
attempt("open w2", lambda: up({"scope": "api", "wave": "w2", "action": "open", "leaves": ["x", "y"], "now": T(1)}))
attempt("seal unstarted", lambda: up({"scope": "api", "wave": "w2", "action": "seal", "now": T(2)}))
attempt("return unsealed", lambda: up({"scope": "api", "wave": "w2", "action": "return", "leaf": "x", "now": T(2)}))
attempt("start unknown leaf", lambda: up({"scope": "api", "wave": "w2", "action": "start", "leaf": "zz", "handle": "h", "now": T(2)}))
attempt("start x", lambda: up({"scope": "api", "wave": "w2", "action": "start", "leaf": "x", "handle": "h-x", "now": T(2)}))
attempt("restart x", lambda: up({"scope": "api", "wave": "w2", "action": "start", "leaf": "x", "handle": "h2", "now": T(2)}))
attempt("reuse handle", lambda: up({"scope": "api", "wave": "w2", "action": "start", "leaf": "y", "handle": "h-x", "now": T(2)}))
# 200 emoji is 400 UTF-16 code units, so the oracle rejects it and len() would not.
attempt("emoji handle", lambda: up({"scope": "api", "wave": "w2", "action": "start", "leaf": "y", "handle": "\U0001F600" * 200, "now": T(2)}))
attempt("blank handle", lambda: up({"scope": "api", "wave": "w2", "action": "start", "leaf": "y", "handle": "   ", "now": T(2)}))
attempt("abandon no reason", lambda: up({"scope": "api", "wave": "w2", "action": "abandon", "now": T(3)}))
attempt("abandon", lambda: up({"scope": "api", "wave": "w2", "action": "abandon", "reason": "worker died", "now": T(3)}))
attempt("abandon twice", lambda: up({"scope": "api", "wave": "w2", "action": "abandon", "reason": "again", "now": T(4)}))

# 2b. KEY ORDER -- a LETTER wave inserted BEFORE a DIGIT one; see the oracle driver for why the
# first scope cannot discriminate.
attempt("ord zz", lambda: up({"scope": "ord", "wave": "zz", "action": "open", "leaves": ["a"], "now": T(1)}))
attempt("ord 7", lambda: up({"scope": "ord", "wave": "7", "action": "open", "leaves": ["a"], "now": T(1)}))
with open(dispatch_state_path(root, "ord"), "r", encoding="utf-8") as handle:
    record("ord state file", handle.read())
record("ord status", dispatch_status(root, "ord"))

# 2c. The DEFAULT clock -- see the oracle driver; only the SHAPE is comparable.
def _default_clock():
    result = up({"scope": "clock", "wave": "c1", "action": "open", "leaves": ["a"]})
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
                             result["wave"]["openedAt"]))


attempt("default clock", _default_clock)

# 2d. The logWarning branch: a committed transition whose audit append FAILS.
os.makedirs(os.path.dirname(dispatch_state_path(root, "warn")), exist_ok=True)
os.symlink("/nonexistent-target",
           dispatch_state_path(root, "warn").replace("dispatch.json", "status.log"))


def _log_warning():
    # The full redacted TEXT -- see the oracle driver for why this is comparable at all.
    result = up({"scope": "warn", "wave": "w1", "action": "open", "leaves": ["a"], "now": T(1)})
    return {"state": result["wave"]["state"], "warning": redact(result["logWarning"])}


attempt("logWarning", _log_warning)

record("status", dispatch_status(root, "api"))
record("status no scope", dispatch_status(root, ""))
attempt("getDispatchWave", lambda: get_dispatch_wave(root, "api", "1"))
attempt("getDispatchWave unknown", lambda: get_dispatch_wave(root, "api", "ghost"))

bad = [
    ("not an object", None),
    ("wrong schema", {"schema": 2, "waves": {}}),
    ("waves is an array", {"schema": 1, "waves": []}),
    ("bad wave shape", {"schema": 1, "waves": {"w": {"state": "open"}}}),
    ("bad state name", {"schema": 1, "waves": {"w": {"state": "nope", "leaves": ["a"], "openedAt": T(1), "started": {}, "returned": {}}}}),
    ("seal metadata on open", {"schema": 1, "waves": {"w": {"state": "open", "leaves": ["a"], "openedAt": T(1), "sealedAt": T(2), "started": {}, "returned": {}}}}),
    ("return before start", {"schema": 1, "waves": {"w": {"state": "sealed", "leaves": ["a"], "openedAt": T(1), "sealedAt": T(2), "started": {"a": {"handle": "h", "at": T(3)}}, "returned": {"a": {"at": T(1)}}}}}),
    ("start before open", {"schema": 1, "waves": {"w": {"state": "open", "leaves": ["a"], "openedAt": T(5), "started": {"a": {"handle": "h", "at": T(1)}}, "returned": {}}}}),
    ("bad timestamp", {"schema": 1, "waves": {"w": {"state": "open", "leaves": ["a"], "openedAt": "not a date", "started": {}, "returned": {}}}}),
    # TWO problems, a DIGIT key alongside a letter key: which error fires first depends on JS
    # enumeration order, which puts "1" before "w" however they were inserted.
    ("two problems, digit key first", {"schema": 1, "waves": {"w": {"state": "nope", "leaves": ["a"], "openedAt": T(1), "started": {}, "returned": {}}, "1": {"state": "open", "leaves": [], "openedAt": T(1), "started": {}, "returned": {}}}}),
]
for name, value in bad:
    path = dispatch_state_path(root, "bad")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + chr(10))
    record("validateState " + name, dispatch_status(root, "bad"))

json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write(chr(10))
