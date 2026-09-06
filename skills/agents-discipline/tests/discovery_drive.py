#!/usr/bin/env python3
"""Python mirror of discovery-drive.mjs. See that file for why each row is here."""
import sys

# BEFORE any local import. A stale .pyc in scripts/lib/__pycache__ is validated on
# (source mtime, source size) ALONE, so a same-size rewrite inside one mtime second -- exactly
# what a mutation probe does -- runs the OLD bytecode while the .py on disk reads correct.
sys.dont_write_bytecode = True

import json  # noqa: E402
import os  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "lib"))

from gates import legacy_files, list_scopes, resolve_target, same_file_identity, scope_files, stat_current_named_file, _js_join  # noqa: E402,I001  # type: ignore[import-not-found]

root = sys.argv[1]
os.environ.pop("AGENTS_DISCIPLINE_SCOPE", None)


def target(**options):
    result = resolve_target({"root": root, **options})
    # Fixed key order, and absent keys DROPPED -- JSON.stringify omits undefined, so a key the
    # oracle never set must not appear here as null.
    expected = ("mode", "scope", "files", "discoveryErrors", "error", "ambiguous")
    # Both drivers PROJECT onto this fixed list, so a MISSING key is caught (the mjs emits it,
    # this omits it) but an EXTRA or MISNAMED one is invisible -- a port returning "ambigous"
    # would diff as nothing at all. The projection is still right (JSON.stringify drops
    # undefined, so absent must mean absent), so the check belongs here rather than in the
    # shape: assert the port returns nothing OUTSIDE the set the oracle can produce.
    unexpected = set(result) - set(expected)
    assert not unexpected, "resolve_target returned unexpected key(s): " + repr(sorted(unexpected))
    return {key: result[key] for key in expected if key in result}


_stat_dir = os.path.join(root, "stat-probe")
_stat_file = os.path.join(_stat_dir, "regular")
_stat_symlink = os.path.join(_stat_dir, "link")
_stat_hardlink = os.path.join(_stat_dir, "hard")
_stat_fifo = os.path.join(_stat_dir, "fifo")
_stat_missing = os.path.join(_stat_dir, "absent")


def _join_row(parts):
    try:
        return _js_join(*parts)
    except Exception as error:            # node throws TypeError for join(); mirror the name
        return "THREW:" + type(error).__name__


def _stat_row(path, options):
    try:
        return "ok:" + str(stat_current_named_file(path, options).st_size)
    except OSError as error:
        return str(error).replace(root, "<R>")


def with_env(value, run):
    os.environ["AGENTS_DISCIPLINE_SCOPE"] = value
    try:
        return run()
    finally:
        os.environ.pop("AGENTS_DISCIPLINE_SCOPE", None)


json.dump({
    "listScopes": list_scopes(root),
    "scopeFiles": [scope_files(root, s) for s in ["api", "keep", "missing", "web"]],
    "legacyFiles": legacy_files(root),
    "targets": {
        "bare": target(),
        "explicit": target(files=["GATES.md", "gates/extra.md"]),
        "explicitAbsolute": target(files=[root + "/GATES.md"]),
        "scopeApi": target(scope="api"),
        "scopeMissing": target(scope="definitely-absent"),
        "scopeInvalid": target(scope="-bad"),
        "scopeDotDot": target(scope=".."),
        "scopeEmptyString": target(scope=""),
        # Non-canonical roots -- see discovery-drive.mjs for the measured divergence.
        "rootDoubleSep": target(root=root + "//"),
        "rootDotSegment": target(root=root + "/./"),
        "rootTrailingSep": target(root=root + "/"),
    },
    "legacyFilesNonCanonical": [legacy_files(r) for r in [root + "//", root + "/./", root + "/"]],
    "scopeFilesNonCanonical": [scope_files(r, "api") for r in [root + "//", root + "/./"]],
    "envScope": with_env("api", lambda: target()),
    "envScopeOverriddenByExplicit": with_env("api", lambda: target(scope="web")),
    "envScopeWithEmptyExplicit": with_env("api", lambda: target(scope="")),
    "jsJoinCorpus": [["a", "b"], ["a//", "b"], ["a/.", "b"], ["a", "b/"], ["/a", "/b"], ["a", "/b"], ["a", ""], ["", ""], ["//"], ["///"], ["///", "a"], ["/", "x.md"], ["/", "."], [".."], ["../a", "b"], ["a", "..", "b"], ["a", "..", "..", "b"], ["/a/", "/b/", "/c/"], ["a/", "/b/"], ["", "a"], ["a", ".", "b"], [".", ""], ["/", ""], ["a/b/", "c"], [], ["é", "à"], ["😀", "a"], ["xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "b"]],
    "jsJoin": [_join_row(parts) for parts in [["a", "b"], ["a//", "b"], ["a/.", "b"], ["a", "b/"], ["/a", "/b"], ["a", "/b"], ["a", ""], ["", ""], ["//"], ["///"], ["///", "a"], ["/", "x.md"], ["/", "."], [".."], ["../a", "b"], ["a", "..", "b"], ["a", "..", "..", "b"], ["/a/", "/b/", "/c/"], ["a/", "/b/"], ["", "a"], ["a", ".", "b"], [".", ""], ["/", ""], ["a/b/", "c"], [], ["é", "à"], ["😀", "a"], ["xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "b"]]],
    # Mirrors discovery-drive.mjs row for row; the mjs runs FIRST and builds the probe tree.
    "statCurrentNamedFile": [_stat_row(path, opts) for path, opts in [
        (_stat_file, None), (_stat_file, {}), (_stat_file, {"label": "ledger"}),
        (_stat_file, {"maxBytes": 10}), (_stat_file, {"maxBytes": 1}),
        (_stat_file, {"maxBytes": 0}), (_stat_file, {"maxBytes": 2.5}),
        (_stat_file, {"maxBytes": -1}), (_stat_file, {"maxBytes": None}),
        (_stat_file, {"maxBytes": "10"}),
        # "1" against a 2-byte file: the ONLY row that RENDERS the size message from a
        # STRING maxBytes. Without it float("1") -> 1.0 printed "exceeds 1.0 bytes" where
        # the oracle says "1 bytes", and every other string row stayed under its cap.
        # ORDER MATTERS -- the two drivers compare POSITIONALLY, and inserting this row after
        # a different neighbour on each side misaligned every row below it.
        (_stat_file, {"maxBytes": "1"}), (_stat_file, {"maxBytes": "ten"}),
        (_stat_file, {"maxBytes": True}), (_stat_file, {"label": ""}),
        (_stat_file, {"label": 0}), (_stat_file, {"label": 7}),
        (_stat_file, {"openFlags": 0}),
        (_stat_symlink, None), (_stat_hardlink, None), (_stat_dir, None),
        (_stat_fifo, {"maxBytes": 100}), (_stat_missing, None),
    ]],
    "sameFileIdentity": [
        same_file_identity({"dev": 7, "ino": 11}, {"dev": 7, "ino": 11}),
        same_file_identity({"dev": 7, "ino": 11}, {"dev": 7, "ino": 12}),
        same_file_identity({"dev": 8, "ino": 11}, {"dev": 7, "ino": 11}),
        same_file_identity(os.lstat(root), os.lstat(root)),
        same_file_identity(os.lstat(root), os.lstat(os.path.dirname(root))),
    ],
}, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
