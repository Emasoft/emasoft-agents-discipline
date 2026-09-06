#!/usr/bin/env python3
"""One-shot EXPECT-regex match worker.

Port of regex-worker.mjs. The JS version runs inside a node:worker_threads Worker that
receives exactly one `{source, flags, output}` message and posts exactly one reply; the
budget/timeout/pool logic lives in the caller (gate-check.mjs), not in the worker itself.
This reads one JSON message from stdin and writes one JSON reply to stdout, then exits --
the same one-shot, single-message contract, just over stdio instead of a worker port.
"""

import json
import re
import sys

# JS RegExp flags mapped to Python's re flags for a single .test() call: `g`/`y`/`d` affect
# only stateful/indexed matching, irrelevant here, and `u` is Python's default (Unicode) mode.
_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


def main():
    message = json.loads(sys.stdin.readline())
    source = message["source"]
    flags = message.get("flags") or ""
    output = message["output"]
    try:
        # re.ASCII always: JS's \w, \b and \d are ASCII-only regardless of the `u` flag, while
        # Python's are Unicode by default. Without this, the port would silently match more
        # than the JS oracle it must agree with.
        py_flags = re.ASCII
        for ch in flags:
            py_flags |= _FLAG_MAP.get(ch, 0)
        matched = re.search(source, output, py_flags) is not None
        result = {"matched": matched}
    except re.error as error:
        result = {"error": str(error)}
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
