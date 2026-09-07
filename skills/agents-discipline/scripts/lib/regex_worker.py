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


def _translate(source, has_m):
    """Rewrite a JS RegExp source into the closest `re`-compatible source.

    A single left-to-right scan, tracking only "inside a character class or not" (JS/Python
    agree on class boundaries except for the one case handled below). Every substitution here
    was picked by running the SAME pattern through `node` and `python3` and comparing -- see
    the TRDD and tests/regex-worker-diff.sh's corpus, not by reasoning about either engine from
    memory. Constructs this function does not recognize are passed through unchanged, which is
    correct for the constructs both engines already agree on (e.g. lookbehind, `\\d`, `\\w`).
    """
    out = []
    i, n = 0, len(source)
    in_class = False
    while i < n:
        ch = source[i]
        if ch == "\\" and i + 1 < n:
            nxt = source[i + 1]
            # JS (no `u` flag; the corpus never pairs \A/\Z with `u`) treats `\A`/`\Z` as
            # Annex-B "IdentityEscape": literal characters, not anchors. Python's `re` gives
            # `\A`/`\Z` the opposite, special meaning (absolute start/end), so passing them
            # through would anchor a pattern JS never anchors. True inside a class too --
            # measured: `[\A]` matches literal "A" in both engines.
            if nxt in ("A", "Z"):
                out.append(nxt)
            else:
                out.append(ch)
                out.append(nxt)
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            out.append(ch)
            i += 1
            continue
        if ch == "[":
            # JS closes a character class on the very FIRST unescaped `]`, even as the class's
            # first character -- unlike the POSIX/Python convention where a leading `]` is a
            # literal. `[]` is therefore a valid, EMPTY class (matches nothing) and `[^]` its
            # negation (matches anything); `re` has no empty-class syntax, so translate to the
            # nearest equivalent: `(?!)` always fails (empty class never matches), `[\s\S]`
            # always succeeds on one character (negated-empty matches any char, newline
            # included). Measured against node: `a[]b` -> no match; `[^]` -> matches any char.
            j = i + 1
            negated = j < n and source[j] == "^"
            if negated:
                j += 1
            if j < n and source[j] == "]":
                out.append(r"[\s\S]" if negated else "(?!)")
                i = j + 1
                continue
            out.append(ch)
            in_class = True
            i += 1
            continue
        if ch == "(" and source[i : i + 4] == "(?P<":
            # `(?P<name>...)` is Python-only named-group syntax; JS parses `(?P` as an
            # unrecognized group modifier and THROWS ("Invalid group"). Passing it to `re`
            # untranslated would make the port MATCH where the oracle errors -- the exact
            # silent-passthrough this port must never do. Fail the same way the oracle does.
            raise re.error("invalid group name syntax for JS RegExp: (?P<...>")
        if ch == "(" and source[i : i + 3] == "(?<" and source[i + 3 : i + 4] not in ("=", "!"):
            # JS named group `(?<name>...)` -> Python `(?P<name>...)`. Excludes lookbehind
            # `(?<=` / `(?<!`, which is NOT a named group and must pass through untouched
            # (measured: the lookbehind row already agrees with the oracle).
            out.append("(?P<")
            i += 3
            continue
        if ch == "$" and not has_m:
            # Without the `m` flag, JS `$` matches ONLY the absolute end of input. Python's
            # `$` without MULTILINE also matches just before a single trailing "\n" -- measured
            # divergence: `/ok$/.test("ok\n")` is false in JS, true in `re.search`. Python's
            # `\Z` has no such exception, so it is the faithful translation. With `m` present
            # both engines already agree (measured trap row `b$` + `m`), so leave `$` alone.
            out.append(r"\Z")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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
        py_source = _translate(source, "m" in flags)
        matched = re.search(py_source, output, py_flags) is not None
        result = {"matched": matched}
    except re.error as error:
        result = {"error": str(error)}
    # A BARE json.dumps, and unlike the two sites in gates.py that were just fixed, this one is
    # correct. Those wrote bytes an oracle-written file is compared against, or text a user
    # reads; this is a PRIVATE protocol between this worker and gate_check.py's json.loads
    # (:988), which decodes any \u escape straight back to the same str. Nothing compares these
    # bytes to regex-worker.mjs's, and nothing could usefully: the two engines' re.error /
    # SyntaxError messages differ by construction, which regex-worker-diff.sh already treats as
    # a known gap rather than a divergence. Left as-is so the next scan for bare json.dumps has
    # this answer rather than re-deriving it.
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
