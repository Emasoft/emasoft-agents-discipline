"""Shared helpers for the Python ports of the agents-discipline scripts.

Port of the corresponding half of `lib/gates.mjs`. Grows one function at a time as each
script is ported; keeping the same module name means no later rename.
"""

import os
import stat as statmod

DEFAULT_STABLE_FILE_MAX_BYTES = 8 * 1024 * 1024


def _assert_regular_single_link(st, target, label, max_bytes):
    # A FIFO opened O_NONBLOCK returns a descriptor at once and then blocks forever on read,
    # which is the hang this assertion exists to turn into an error. nlink != 1 means the
    # bytes are reachable under another name, so "the file I checked" is not a single thing.
    if not statmod.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise OSError(f"{label} must be one unchanged regular single-link file: {target}")
    if st.st_size > max_bytes:
        raise OSError(f"{label} exceeds {max_bytes} bytes: {target}")


def read_stable_regular_file(path, max_bytes=None, label="file"):
    """Read a regular file under a size cap, refusing symlinks, FIFOs and mid-read changes.

    O_NOFOLLOW refuses a symlinked target outright; O_NONBLOCK keeps the open from blocking
    on a FIFO so the descriptor can be rejected by its type rather than waited on. The
    before/after stat pair makes a file that changes under the read an error instead of a
    silently half-old string.
    """
    target = os.path.abspath(path)
    limit = DEFAULT_STABLE_FILE_MAX_BYTES if max_bytes is None else int(max_bytes)
    if limit < 1:
        raise OSError(f"{label} max_bytes must be a positive integer")

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as err:
        if err.errno == getattr(__import__("errno"), "ELOOP", None):
            raise OSError(f"{label} must be one unchanged regular single-link file: {target}") from err
        raise

    try:
        opened = os.fstat(fd)
        named = os.lstat(target)
        _assert_regular_single_link(opened, target, label, limit)
        # The descriptor and the NAME can already be different files: something may have
        # swapped the path between the open and now. Comparing the fd's identity against the
        # path's is what makes "the file I checked" and "the file at this path" one claim.
        if (opened.st_ino, opened.st_dev) != (named.st_ino, named.st_dev):
            raise OSError(f"{label} changed before it was read: {target}")
        canonical_before = os.path.realpath(target)

        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OSError(f"{label} exceeds {limit} bytes: {target}")

        after = os.fstat(fd)
        _assert_regular_single_link(after, target, label, limit)
        if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (
            opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns
        ):
            raise OSError(f"{label} changed while it was read: {target}")
        if os.path.realpath(target) != canonical_before:
            raise OSError(f"{label} changed canonical location while it was read: {target}")
        # errors="replace", NOT strict. A ledger is prose a human pasted into, so one byte of
        # it is routinely not UTF-8 -- a latin-1 accent, a smart quote out of a word processor.
        # Node's Buffer.toString("utf8") substitutes U+FFFD and carries on; a strict decode
        # raises instead, and the caller then reports "cannot read" (exit 2, "not a ledger")
        # on a file that is visibly a ledger, over a byte that is almost never in the row
        # under test. Measured: `caf\xe9` in a unit name turned a complete ledger into exit 2.
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        os.close(fd)
