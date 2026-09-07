#!/usr/bin/env python3
"""Port of `lib/regex-worker.mjs`: test ONE regex against ONE output, disposably.

A SUBPROCESS, where the oracle uses a worker THREAD, and that is forced rather than
chosen. The whole point of the worker is that the parent can abandon a catastrophically
backtracking match after `REGEX_TIMEOUT_MS`. Node can `worker.terminate()` a thread;
CPython cannot interrupt a thread stuck inside `re` at all -- the C matcher never
returns to the interpreter, so it checks no signal and releases no GIL. A thread here
would hang the checker exactly as the oracle's design exists to prevent. The parent
kills a process instead.

PROTOCOL (stdin/stdout, one JSON object each way) -- the shapes match the oracle's
`postMessage` payloads exactly, because `safeRegexMatch` branches on them:
    in :  {"source": str, "flags": str, "output": str}
    out:  {"matched": bool}   on success
          {"error": str}      on a bad pattern
Anything else the parent must treat as a crash, which is what the oracle does with a
worker that exits without posting a message.
"""
import sys

# BEFORE any local import -- a stale .pyc validates on (source mtime, source size) alone,
# and a same-size mutation inside one mtime second leaves the OLD bytecode executing.
sys.dont_write_bytecode = True

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# IMPORTED, not re-declared. `parse_regex` VALIDATES an EXPECT with this map and this
# worker MATCHES with it; a second copy lets the two drift, and the drift is silent and
# vacuous -- a pattern validated case-insensitively would be matched case-sensitively,
# so every EXPECT would still "work" while quietly testing something else.
from gates import _JS_FLAG_MAP  # noqa: E402,I001  # type: ignore[import-not-found]


def main():
    request = json.loads(sys.stdin.read())
    flags = re.ASCII  # matches parse_regex: JS \d\w\b are ASCII-only, Python's are not
    for flag_char in request.get("flags", ""):
        flags |= _JS_FLAG_MAP.get(flag_char, 0)
    try:
        # `search`, not `match`: JS `RegExp.test` is unanchored, and `re.match` anchors at
        # the start. That difference is invisible on a pattern beginning with `.*` and
        # wrong on every other one.
        matched = re.compile(request["source"], flags).search(request["output"]) is not None
        reply = {"matched": matched}
    except re.error as error:
        reply = {"error": str(error)}
    # COMPACT separators, matching `JSON.stringify`. Python's default `json.dump` writes
    # `{"matched": true}` and JS writes `{"matched":true}`; the parent parses either, so this
    # is not a behavioural fix -- it is so the differential can SEE the semantic rows. With
    # the default separators every one of the 21 corpus rows reported DIVERGE on whitespace
    # and the six real engine differences were invisible in the noise.
    json.dump(reply, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
