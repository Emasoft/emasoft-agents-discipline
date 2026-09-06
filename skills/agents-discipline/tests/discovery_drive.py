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

from gates import legacy_files, list_scopes, resolve_target, same_file_identity, scope_files  # noqa: E402,I001  # type: ignore[import-not-found]

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
    "sameFileIdentity": [
        same_file_identity({"dev": 7, "ino": 11}, {"dev": 7, "ino": 11}),
        same_file_identity({"dev": 7, "ino": 11}, {"dev": 7, "ino": 12}),
        same_file_identity({"dev": 8, "ino": 11}, {"dev": 7, "ino": 11}),
        same_file_identity(os.lstat(root), os.lstat(root)),
        same_file_identity(os.lstat(root), os.lstat(os.path.dirname(root))),
    ],
}, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
