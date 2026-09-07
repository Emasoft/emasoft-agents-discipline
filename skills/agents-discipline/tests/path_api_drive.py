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

from gates import (  # noqa: E402,I001  # type: ignore[import-not-found]
    _js_join, js_basename, js_dirname, js_relative, js_resolve,
)

# Kept in the same order and the same values as path-api-drive.mjs's CASES. They are duplicated
# rather than imported across the language boundary; the diff script's vacuity gate checks the
# COUNTS match, so a corpus that drifts on one side is caught rather than silently compared
# element-by-element against a different list.
CASES = {
    # See path-api-drive.mjs: the `//a` rows exist because a mutation control found them missing.
    "dirname": ["/a/b", "/a/", "a", "/", "", "//", "//a", "//a/b", "/a//b//", "/a/b/.", "a/b",
                "./a"],
    "basename": ["/a/b", "/a/", "a", "/", "", "//", "/a//b//", "a/b/", ".", ".."],
    "isAbsolute": ["/a", "a", "", "./a", "//a", "../a", "/"],
    "join": [["/a", "b"], ["/a", "/b"], ["a", ""], ["", "b"], ["/a", ".."], ["/a", "b/"],
             ["a", "..", "..", "b"], [""], ["/"], ["a/", "/b"]],
    "relative": [["/a/b", "/a/b/c"], ["/a/b", "/a"], ["/a/b", "/a/b"], ["/a/b", "/x"],
                 ["/a/b", "/a/b/../c"], ["/a//b", "/a/b/c"], ["/a/b/", "/a/b/c/"],
                 ["/a/b", "/a/bc"], ["/a", "/a/../a/x"], ["/", "/a"],
                 # See path-api-drive.mjs: two js_relative branches had no row reaching them.
                 ["/a", "/"], ["/a/bc", "/a/b"]],
    # See path-api-drive.mjs for why the `//` rows exist and what they cost to omit.
    "resolve": [["/a", "b"], ["/a", "/b"], ["a"], ["/a", ".."], ["/", ".."], ["/a", "b", "../c"],
                ["//a"], ["///a"], ["//"], ["//a/b"], ["//a/../b"],
                ["/a", "./b"], ["/a/./b"], ["a/./b"], ["/a/b/."], ["."], ["./."]],
}

answers = {
    # EVERY row now drives a PORT, not a stdlib stand-in. That changes what this file measures,
    # and the change is the point. While `resolve` had no port, its row compared node against
    # `abspath(_js_join(...))` -- an expression written FOR the test, so its divergences were
    # partly artifacts of that composition rather than facts about either runtime. A row whose
    # result is decided by how the harness was built measures the harness.
    #
    # The stand-ins were defensible for `dirname` and `relative` and NOT for `resolve`, and the
    # line is worth stating: `os.path.dirname`/`relpath` are the same-named, same-arity,
    # same-purpose functions a porter reaches for without pausing, so comparing them predicted
    # a mistake a real port would make. `os.path` has no `resolve` at all -- nobody can reach
    # for the wrong function by name, so that row measured one arbitrary guess.
    "dirname": [str(js_dirname(p)) for p in CASES["dirname"]],
    "basename": [str(js_basename(p)) for p in CASES["basename"]],
    # isAbsolute is the ONE row still comparing stdlib to node: os.path.isabs is a genuinely
    # different function that happens to agree, which is a finding rather than a regression
    # check. The ported rows around it can only ever confirm their own ports.
    "isAbsolute": [os.path.isabs(p) for p in CASES["isAbsolute"]],
    "join": [str(_js_join(*parts)) for parts in CASES["join"]],
    "relative": [str(js_relative(a, b)) for a, b in CASES["relative"]],
    "resolve": [str(js_resolve(*parts)) for parts in CASES["resolve"]],
    "sep": os.sep,
    "delimiter": os.pathsep,
    # sys.platform embeds the OS major version where process.platform does not (freebsd14 vs
    # freebsd). Stripped here so the two agree everywhere, not just on the three platforms
    # where they coincide by accident -- see the TRDD's approval-digest note.
    "platform": sys.platform.rstrip("0123456789"),
}

json.dump(answers, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
