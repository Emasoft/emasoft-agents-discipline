#!/usr/bin/env python3
"""Prove gates._write_all survives a REAL short write, and that one can actually happen.

Runs as its own process because RLIMIT_FSIZE and the SIGXFSZ disposition are process-wide.

Why a size limit and not a pipe: a blocking pipe never short-writes (POSIX requires write() to
transfer all nbyte before returning), and my first two attempts at this measured exactly that --
os.write returned the full buffer, which is equally consistent with "the loop is unnecessary".
A regular file under RLIMIT_FSIZE is the shape that genuinely short-writes, and it is the same
shape as the failure the loop exists for: a state file hitting a disk or quota boundary.

Prints two lines, CONTROL first: the control has to short-write, or the result below proves
nothing at all.
"""
import os, resource, signal, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from gates import _write_all  # noqa: E402  # type: ignore[import-not-found]
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
payload = b"x" * (256 * 1024)
_tmp = tempfile.mkdtemp()

# CONTROL: one bare os.write on a REGULAR file under a size cap.
fd = os.open(os.path.join(_tmp, "control"), os.O_WRONLY | os.O_CREAT, 0o600)
try:
    n = os.write(fd, payload)
    print(f"control: single os.write returned {n} of {len(payload)} -> SHORT: {n < len(payload)}")
except OSError as e:
    print(f"control: os.write raised {type(e).__name__} {e.errno}")
os.close(fd)

# _write_all on the same shape: it must NOT silently stop at the cap.
fd = os.open(os.path.join(_tmp, "loop"), os.O_WRONLY | os.O_CREAT, 0o600)
try:
    _write_all(fd, payload)
    print("_write_all: returned without error (WRONG if the control short-wrote)")
except OSError as e:
    print(f"_write_all: raised {type(e).__name__} errno={e.errno} -> the caller SEES the failure")
os.close(fd)
