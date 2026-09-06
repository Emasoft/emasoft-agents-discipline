#!/usr/bin/env python3
"""Build the filesystem shapes resolve_target / list_scopes / *_files must agree on.

A string corpus cannot reach these: every answer depends on lstat, symlink-ness, readdir
ORDER and errno. The tree is built from code points (never typed) and printed as one root
path on stdout, so both drivers run against the SAME absolute paths and their JSON compares
without any rewriting.

Usage: python3 tests/build-discovery-tree.py <variant> [parent-dir]
"""
import os
import stat
import sys
import tempfile

AD = ".agents-discipline"


def mk(path):
    os.makedirs(path, exist_ok=True)
    return path


def link(source, name):
    """Idempotent symlink. Rebuilding a variant into a parent that already holds it raised
    FileExistsError, and the shell caught it the worst possible way: the traceback killed the
    script BEFORE it printed the root, so `R=$(build ...)` was the EMPTY STRING and both
    drivers then ran against root "" -- agreeing perfectly on the answer for nothing at all.
    A runner that only compares the two outputs cannot see that; see discovery-diff.sh, which
    now refuses an empty root."""
    if os.path.islink(name) or os.path.exists(name):
        os.remove(name)
    os.symlink(source, name)


def touch(path, text="- [ ] g1: t\n"):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def scope(root, name, *, gates=()):
    base = mk(os.path.join(root, AD, name))
    touch(os.path.join(base, "GATES.md"))
    if gates:
        directory = mk(os.path.join(base, "gates"))
        for entry in gates:
            touch(os.path.join(directory, entry))
    return base


def build(variant, root):
    if variant == "one-scope":
        # The ASTRAL row, and the whole reason this variant carries a gates/ directory.
        # markdownDiscovery sorts filenames with NO id filter, and JS Array.sort() orders by
        # UTF-16 CODE UNITS where Python's sorted() orders by CODE POINTS. U+1F600 is the
        # surrogate pair D83D DE00, so it sorts BEFORE U+FFFD in JS and AFTER it in Python.
        # list_scopes cannot show this -- validate_scope_id rejects both names -- so if this
        # file's corpus were scopes only, the divergence would ship.
        scope(root, "api", gates=[chr(0x1F600) + ".md", chr(0xFFFD) + ".md", "a.md", "B.md",
                                  "_.md", "a-b.md", "a.MD", "not-markdown.txt"])
    elif variant == "two-scopes":
        scope(root, "api")
        scope(root, "web")
    elif variant == "no-scopes":
        touch(os.path.join(root, "GATES.md"))
        mk(os.path.join(root, "gates"))
        touch(os.path.join(root, "gates", "extra.md"))
    elif variant == "hostile-entries":
        # Everything list_scopes must EXCLUDE, beside one it must keep.
        scope(root, "keep")
        state = os.path.join(root, AD)
        mk(os.path.join(state, "locks"))                    # excluded by name
        touch(os.path.join(state, "afile"))                 # not a directory
        mk(os.path.join(state, "-bad-id"))                  # fails validate_scope_id
        mk(os.path.join(state, "x" * 65))                   # 65 chars: one over the bound
        mk(os.path.join(state, "."))                        # already exists; a no-op
        outside = mk(os.path.join(root, "outside-target"))
        link(outside, os.path.join(state, "linkdir"))        # symlink to a real directory
        link(os.path.join(state, "keep"), os.path.join(state, "linkscope"))
        fifo = os.path.join(state, "afifo")                  # neither file nor directory
        if not os.path.exists(fifo):
            os.mkfifo(fifo)
    elif variant == "scope-is-a-file":
        # namedEntry(scopePath) is TRUE while list_scopes excluded it, so resolve_target must
        # take the discovery branch and report the real-directory error -- NOT "no such scope".
        mk(os.path.join(root, AD))
        touch(os.path.join(root, AD, "api"), "not a directory\n")
    elif variant == "state-is-a-symlink":
        # The second half of that same guard: .agents-discipline itself is a link, so
        # _real_directory_inside(state) is false while _named_entry(state) is true.
        elsewhere = mk(os.path.join(root, "elsewhere"))
        mk(os.path.join(elsewhere, "api"))
        link(elsewhere, os.path.join(root, AD))
    elif variant == "dangling-state-link":
        # .agents-discipline is a symlink to NOTHING. This is the only shape that separates
        # existsSync (FOLLOWS the link => false) from an lstat-based presence test (=> true),
        # and it exists because a mutation of exists -> lexists reddened NOTHING against the
        # other six variants: state-is-a-symlink points at a real directory, where both
        # answer true. A variant added because a mutation escaped, not because it looked tidy.
        link(os.path.join(root, "no-such-target"), os.path.join(root, AD))
    elif variant == "empty":
        pass
    else:
        raise SystemExit("unknown variant: " + variant)


if __name__ == "__main__":
    parent = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="ad-discovery-")
    target = mk(os.path.join(parent, sys.argv[1]))
    build(sys.argv[1], target)
    # World-writable bits would change nothing here, but a mode assertion elsewhere might read
    # them; keep the tree boring so the only variable is shape.
    os.chmod(target, stat.S_IRWXU)
    print(target)
