#!/usr/bin/env python3
"""Drive the PORT's scope/write/lock/status helpers, in the oracle driver's shape.

Pair of tests/gates-helpers-drive.mjs. See that file for why the effects are dumped rather
than asserted.
"""

import errno
import json
import os
import stat as statmod
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from gates import (  # noqa: E402  # type: ignore[import-not-found]
    append_status, read_stable_regular_file, scope_root, status_log_path, validate_scope_id,
    with_file_lock, write_atomic,
)

root = sys.argv[1]
out = []


def redact(value):
    # Both spellings of the root -- see the oracle driver for why the canonical one is needed
    # and why the KEY is redacted as well as the value.
    return str(value).replace(os.path.realpath(root), "<ROOT>").replace(root, "<ROOT>")


def record(name, value):
    out.append([redact(name), value])


def attempt(name, fn):
    try:
        record(name, {"ok": fn()})
    except Exception as error:  # noqa: BLE001 -- the oracle's catch is equally wide
        # An OS-produced error is recorded by errno NAME; an authored one verbatim. The oracle
        # driver carries the full reasoning for that one declared normalization.
        number = getattr(error, "errno", None)
        if number is not None:
            record(name, {"oserror": errno.errorcode.get(number, str(number))})
        else:
            # str(error), not repr: the oracle records `error.message`, no class name.
            record(name, {"error": redact(error)})


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# 1. validate_scope_id
ids = [("api", "api"), ("a", "a"), ("empty", ""), ("dot", "."), ("dotdot", ".."),
       ("lead-dash", "-lead"), ("lead-underscore", "_lead"), ("space", "a b"),
       ("trailing-newline", "api\n"), ("embedded-newline", "api\nx"), ("non-ascii-latin", "café"),
       ("non-ascii-symbol", "ⓐ"), ("len64", "a" * 64), ("len65", "a" * 65),
       ("punctuation", "a.b-c_d"), ("zero-string", "0"), ("slash", "a/b"), ("backslash", "a\\b"),
       ("nul", "a\0b"), ("falsy-number", 0), ("empty-value", None)]
for label, scope_id in ids:
    record("validateScopeId " + label, validate_scope_id(scope_id))
record("validateScopeId label", validate_scope_id("a b", "leaf"))

# 2. scope_root / status_log_path
for r, s in [(root, "api"), ("a", "api"), ("a//", "api"), ("./a", "api"), ("a/b/..", "api")]:
    record("scopeRoot " + r, redact(scope_root(r, s)))
record("statusLogPath scoped", redact(status_log_path(root, "api")))
record("statusLogPath bare", redact(status_log_path(root, None)))

# 3. write_atomic
nested = os.path.join(root, "deep", "nest", "file.txt")


def _write_nested():
    write_atomic(nested, "hello\n")
    return read(nested)


attempt("writeAtomic nested", _write_nested)


def _write_rooted():
    write_atomic(os.path.join(scope_root(root, "api"), "dispatch.json"), '{"schema":1}\n',
                 root=root)
    return read(os.path.join(scope_root(root, "api"), "dispatch.json"))


attempt("writeAtomic rooted", _write_rooted)


def _write_again():
    write_atomic(nested, "again\n")
    return read(nested)


attempt("writeAtomic overwrite", _write_again)

record("mode .agents-discipline",
       oct(statmod.S_IMODE(os.lstat(os.path.join(root, ".agents-discipline")).st_mode))[2:])
record("mode scope dir", oct(statmod.S_IMODE(os.lstat(scope_root(root, "api")).st_mode))[2:])
record("scope dir entries", sorted(os.listdir(scope_root(root, "api"))))

os.symlink(nested, os.path.join(root, "link.txt"))
attempt("writeAtomic onto symlink", lambda: (write_atomic(os.path.join(root, "link.txt"),
                                                          "nope\n"), "wrote")[1])

# 4. The containment guard.
# An ANCESTOR link, not the file -- see the oracle driver for why linking the file certified a
# guard that never ran.
os.makedirs(os.path.join(root, "outside"), exist_ok=True)
with open(os.path.join(root, "outside", "escaped.json"), "w", encoding="utf-8") as handle:
    handle.write('{"schema":1}\n')
os.symlink(os.path.join(root, "outside"), os.path.join(scope_root(root, "api"), "via-link"))
via = os.path.join(scope_root(root, "api"), "via-link", "escaped.json")
attempt("read escaped without root",
        lambda: read_stable_regular_file(via, label="dispatch state"))
attempt("read escaped with root", lambda: read_stable_regular_file(
    via, root=os.path.join(root, ".agents-discipline"), label="dispatch state"))
attempt("read contained with root", lambda: read_stable_regular_file(
    os.path.join(scope_root(root, "api"), "dispatch.json"),
    root=os.path.join(root, ".agents-discipline"), label="dispatch state"))
os.symlink(os.path.join(root, "outside", "escaped.json"),
           os.path.join(scope_root(root, "api"), "escape.json"))
attempt("read symlinked file", lambda: read_stable_regular_file(
    os.path.join(scope_root(root, "api"), "escape.json"), label="dispatch state"))

# 5. append_status


def _append(scope, line):
    append_status(root, scope, line)
    return read(status_log_path(root, scope))


attempt("appendStatus first", lambda: _append("api", "2020-01-01T00:00:00.000Z started"))
attempt("appendStatus folds newlines", lambda: _append("api", "line one\r\nline two\n\n\nline three"))
attempt("appendStatus bare scope", lambda: _append(None, "no scope"))
os.makedirs(scope_root(root, "linked"), exist_ok=True)
os.symlink(nested, os.path.join(scope_root(root, "linked"), "status.log"))
attempt("appendStatus onto symlink", lambda: append_status(root, "linked", "nope"))

# 6. with_file_lock
locks = os.path.join(root, ".agents-discipline", "locks")
attempt("withFileLock returns", lambda: with_file_lock(root, os.path.join(root, "target"),
                                                       lambda: "value"))
record("lock dir after release", sorted(os.listdir(locks)))
attempt("withFileLock nested times out", lambda: with_file_lock(
    root, os.path.join(root, "target"),
    lambda: with_file_lock(root, os.path.join(os.path.realpath(root), "target"),
                           lambda: "unreachable", timeout_ms=0)))
record("lock dir after timeout", sorted(os.listdir(locks)))


def _boom():
    raise RuntimeError("boom")


attempt("withFileLock releases on throw",
        lambda: with_file_lock(root, os.path.join(root, "target"), _boom))
record("lock dir after throw", sorted(os.listdir(locks)))
attempt("withFileLock missing root", lambda: with_file_lock(
    os.path.join(root, "no-such-root"), os.path.join(root, "target"), lambda: "value"))

# Regressions for the four findings of the b263dad review -- see the oracle driver.
for r in ["a//", "./a", "a/b/..", "a"]:
    record("statusLogPath bare " + r, redact(status_log_path(r, None)))


def _corrupt_locks(text):
    lock_dir = os.path.join(root, ".agents-discipline", "locks")
    for name in os.listdir(lock_dir):
        if name.endswith(".filelock"):
            with open(os.path.join(lock_dir, name), "w", encoding="utf-8") as handle:
                handle.write(text)


def _non_object():
    _corrupt_locks("[]")
    raise RuntimeError("invalid dispatch state: boom")


attempt("withFileLock non-object lock file",
        lambda: with_file_lock(root, os.path.join(root, "corrupt"), _non_object))
attempt("withFileLock unparseable lock file", lambda: with_file_lock(
    root, os.path.join(root, "corrupt2"),
    lambda: (_corrupt_locks("{not json"), "returned anyway")[1]))
with open(os.path.join(root, "blocker"), "w", encoding="utf-8") as handle:
    pass
attempt("writeAtomic through a file",
        lambda: (write_atomic(os.path.join(root, "blocker", "child.txt"), "x"), "wrote")[1])
# ROOTED too -- see the oracle driver; the unrooted row above does not reach the mkdir loop.
with open(os.path.join(scope_root(root, "api"), "blocked"), "w", encoding="utf-8") as handle:
    pass
attempt("writeAtomic rooted through a file",
        lambda: (write_atomic(os.path.join(scope_root(root, "api"), "blocked", "child.json"),
                              "x", root=root), "wrote")[1])

record("final tree", sorted(os.listdir(root)))
record("state tree", sorted(os.listdir(os.path.join(root, ".agents-discipline"))))
record("existsSync temp leak", len([n for n in os.listdir(root) if n.endswith(".tmp")]))
record("sanity", os.path.exists(nested))

json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
