#!/usr/bin/env python3
"""Python mirror of path-api-drive.mjs. See that file for why this pair exists.

DELIBERATELY MIXED. Where a JS-faithful port already exists it is used (`gates._js_join`,
`gates.js_basename`); where none does, the plain `os.path` function is used. That is the whole
measurement: the resulting divergence set is exactly the list of `node:path` functions
`gate-check.mjs` imports that still need a port. A driver that hand-rolled JS semantics inline
would report parity for code that does not exist anywhere the checker can call it.
"""
import sys

# BEFORE any local import -- a stale .pyc validates on (source mtime, source size) alone.
sys.dont_write_bytecode = True

import json  # noqa: E402
import os  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "lib"))

from gates import _js_join, js_basename  # noqa: E402,I001  # type: ignore[import-not-found]

# Kept in the same order and the same values as path-api-drive.mjs's CASES. They are duplicated
# rather than imported across the language boundary; the diff script's vacuity gate checks the
# COUNTS match, so a corpus that drifts on one side is caught rather than silently compared
# element-by-element against a different list.
CASES = {
    "dirname": ["/a/b", "/a/", "a", "/", "", "//", "/a//b//", "/a/b/.", "a/b", "./a"],
    "basename": ["/a/b", "/a/", "a", "/", "", "//", "/a//b//", "a/b/", ".", ".."],
    "isAbsolute": ["/a", "a", "", "./a", "//a", "../a", "/"],
    "join": [["/a", "b"], ["/a", "/b"], ["a", ""], ["", "b"], ["/a", ".."], ["/a", "b/"],
             ["a", "..", "..", "b"], [""], ["/"], ["a/", "/b"]],
    "relative": [["/a/b", "/a/b/c"], ["/a/b", "/a"], ["/a/b", "/a/b"], ["/a/b", "/x"],
                 ["/a/b", "/a/b/../c"], ["/a//b", "/a/b/c"], ["/a/b/", "/a/b/c/"],
                 ["/a/b", "/a/bc"], ["/a", "/a/../a/x"], ["/", "/a"]],
    # See path-api-drive.mjs for why the `//` rows exist and what they cost to omit.
    "resolve": [["/a", "b"], ["/a", "/b"], ["a"], ["/a", ".."], ["/", ".."], ["/a", "b", "../c"],
                ["//a"], ["///a"], ["//"], ["//a/b"], ["//a/../b"]],
}

answers = {
    # NO PORT YET -- plain os.path, so the divergence is measured rather than hidden.
    "dirname": [str(os.path.dirname(p)) for p in CASES["dirname"]],
    # PORTED -- js_basename exists, so this row tests the PORT, not os.path.basename.
    "basename": [str(js_basename(p)) for p in CASES["basename"]],
    "isAbsolute": [os.path.isabs(p) for p in CASES["isAbsolute"]],
    # PORTED -- _js_join was rewritten three times to match node's join.
    "join": [str(_js_join(*parts)) for parts in CASES["join"]],
    # NO PORT YET. resolve() applied first, mirroring gate-check's own call sites.
    "relative": [str(os.path.relpath(os.path.abspath(b), os.path.abspath(a)))
                 for a, b in CASES["relative"]],
    "resolve": [str(os.path.abspath(_js_join(*parts)) if len(parts) > 1 else os.path.abspath(parts[0]))
                for parts in CASES["resolve"]],
    "sep": os.sep,
    "delimiter": os.pathsep,
    # sys.platform embeds the OS major version where process.platform does not (freebsd14 vs
    # freebsd). Stripped here so the two agree everywhere, not just on the three platforms
    # where they coincide by accident -- see the TRDD's approval-digest note.
    "platform": sys.platform.rstrip("0123456789"),
}

json.dump(answers, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
