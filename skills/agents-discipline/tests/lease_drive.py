#!/usr/bin/env python3
"""Python mirror of lease-drive.mjs. See that file for why each step is here."""
import sys

# BEFORE any local import -- a stale .pyc validates on (source mtime, source size) alone.
sys.dont_write_bytecode = True

import json  # noqa: E402
import os  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "lib"))

from gates import claim_leases, read_leases, release_leases, sha256  # noqa: E402,I001  # type: ignore[import-not-found]

root = sys.argv[1]
os.makedirs(root, exist_ok=True)
locks = os.path.join(root, ".agents-discipline", "locks")


def scrub(value):
    """Replace the pid and the root, keeping the SHAPE comparable."""
    if isinstance(value, dict):
        return {k: ("<PID>" if k == "pid" else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return value.replace(root, "<R>")
    return value


steps = []


def step(label, run):
    try:
        outcome = scrub(run())
    except Exception as error:
        outcome = "THREW: " + str(error).replace(root, "<R>")
    try:
        files = sorted(os.listdir(locks))
    except OSError:
        files = ["(no lock dir)"]
    steps.append({"label": label, "outcome": outcome, "files": files,
                  "leases": scrub(read_leases(root))})


def claim(scope, leaf, globs):
    return lambda: claim_leases(root, {"scope": scope, "leaf": leaf, "globs": globs})


def release(scope, leaf):
    return lambda: release_leases(root, {"scope": scope, "leaf": leaf})


step("read on a virgin root", lambda: read_leases(root))
step("claim api/leaf-a src/a/**", claim("api", "leaf-a", ["src/a/**"]))
step("claim api/leaf-b src/b/** (disjoint)", claim("api", "leaf-b", ["src/b/**"]))
step("claim api/leaf-c src/a/deep/** (overlaps a)", claim("api", "leaf-c", ["src/a/deep/**"]))
step("claim api/leaf-d src/** (overlaps everything)", claim("api", "leaf-d", ["src/**"]))
step("claim web/leaf-a src/a/** (other scope, same glob)", claim("web", "leaf-a", ["src/a/**"]))
step("re-claim api/leaf-a (identity conflict)", claim("api", "leaf-a", ["totally/other/**"]))
step("claim with a bad scope id", claim("-bad", "leaf-x", ["src/x/**"]))
step("claim with a bad leaf id", claim("api", "-bad", ["src/x/**"]))
step("claim with no globs", claim("api", "leaf-x", []))
step("claim with an absolute glob", claim("api", "leaf-x", ["/abs/**"]))
step("claim with a traversal glob", claim("api", "leaf-x", ["../escape/**"]))
step("claim with a placeholder glob", claim("api", "leaf-x", ["<repository-relative globs>"]))
step("release api/leaf-b", release("api", "leaf-b"))
step("release api/leaf-b again (idempotent)", release("api", "leaf-b"))
step("claim api/leaf-c now that b is gone", claim("api", "leaf-c", ["src/b/**"]))
step("release whole scope api", release("api", None))
step("release a scope that holds nothing", release("nothing", None))

os.makedirs(locks, exist_ok=True)
with open(os.path.join(locks, "not-a-digest.lease"), "w", encoding="utf-8") as handle:
    handle.write("{}\n")
step("claim beside a misnamed lease", claim("api", "leaf-z", ["src/z/**"]))
with open(os.path.join(locks, "not-a-digest.lease"), "w", encoding="utf-8") as handle:
    handle.write("{ this is not json\n")
step("claim beside an unparseable lease", claim("api", "leaf-z", ["src/z/**"]))

# See lease-drive.mjs for why these two exist: the tampered files above fail the SHAPE test
# first, so they never isolate the filename-identity or glob-normalization checks.
def _lease_for(scope, leaf, globs):
    return json.dumps({"scope": scope, "leaf": leaf, "globs": globs, "pid": 1}, indent=2) + "\n"


def _write(name, text):
    with open(os.path.join(locks, name), "w", encoding="utf-8") as handle:
        handle.write(text)


_write("0000000000000000000000ff.lease", _lease_for("api", "leaf-a", ["src/q/**"]))
step("claim beside a valid record under a WRONG filename", claim("api", "leaf-q", ["src/q/**"]))
os.unlink(os.path.join(locks, "0000000000000000000000ff.lease"))

_digest = sha256("api" + "::" + "leaf-r")[:24] + ".lease"
_write(_digest, _lease_for("api", "leaf-r", ["./src/r/**"]))
step("claim beside a record with a NON-CANONICAL glob", claim("api", "leaf-s", ["src/r/**"]))
os.unlink(os.path.join(locks, _digest))

json.dump(steps, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
