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
    out = {}
    for key in ("mode", "scope", "files", "discoveryErrors", "error", "ambiguous"):
        if key in result:
            out[key] = result[key]
    return out


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
    },
    "envScope": with_env("api", lambda: target()),
    "envScopeOverriddenByExplicit": with_env("api", lambda: target(scope="web")),
    "sameFileIdentity": [
        same_file_identity({"dev": 7, "ino": 11}, {"dev": 7, "ino": 11}),
        same_file_identity({"dev": 7, "ino": 11}, {"dev": 7, "ino": 12}),
        same_file_identity({"dev": 8, "ino": 11}, {"dev": 7, "ino": 11}),
    ],
}, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
