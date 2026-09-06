#!/usr/bin/env python3
"""Prove gates._write_all handles a REAL short write, and that one can actually happen.

Runs as its own process because RLIMIT_FSIZE and the SIGXFSZ disposition are process-wide and
the rlimit's HARD half cannot be raised again afterwards.

Why a size limit and not a pipe: a blocking pipe never short-writes (POSIX requires write() to
transfer all nbyte before returning), and my first two attempts at this measured exactly that --
os.write returned the full buffer, a result equally consistent with "the loop is unnecessary".

Three lines, and the CONTROL comes first because it licenses the other two: if a bare os.write
did not short-write here, nothing below is evidence of anything.

  control  -- a bare os.write on a regular file under RLIMIT_FSIZE returns SHORT
  error    -- _write_all on that same shape surfaces the failure instead of truncating
  success  -- _write_all delivers EVERY byte when the writes short but do not fail

The third case is the one the fix exists for, and the first version of this probe did not have
it: demonstrating "does not truncate SILENTLY" is not the same as demonstrating "does not
truncate". A stream socket is used for it because, unlike a pipe, a blocking SOCK_STREAM send
may return fewer bytes than asked once its send buffer fills.
"""

import os
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from gates import _write_all  # noqa: E402  # type: ignore[import-not-found]

PAYLOAD = b"x" * (256 * 1024)
CAP = 65536


def _size_capped_cases(tmp):
    """The two RLIMIT_FSIZE cases. Unix only -- `resource` and SIGXFSZ do not exist on Windows."""
    import resource
    import signal

    # Without this the process is KILLED by SIGXFSZ at the cap instead of getting a short count.
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (CAP, CAP))

    fd = os.open(os.path.join(tmp, "control"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        written = os.write(fd, PAYLOAD)
        print(f"control: single os.write returned {written} of {len(PAYLOAD)} "
              f"-> SHORT: {written < len(PAYLOAD)}")
    except OSError as error:
        print(f"control: os.write raised {type(error).__name__} {error.errno}")
    finally:
        os.close(fd)

    # A DIFFERENT file, so this starts from zero as the control did rather than from the cap.
    fd = os.open(os.path.join(tmp, "loop"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        _write_all(fd, PAYLOAD)
        print("error: _write_all returned without error (WRONG if the control short-wrote)")
    except OSError as error:
        print(f"error: _write_all raised {type(error).__name__} errno={error.errno} "
              f"-> the caller SEES the failure")
    finally:
        os.close(fd)


def _success_case():
    """_write_all across MULTIPLE successful short writes -- the path the fix exists for.

    The SETUP is guarded separately from the measurement. A platform that cannot provide the
    mechanism (no socketpair, no settable SO_SNDBUF, no os.write on a socket fd) must report
    SKIPPED; a _write_all that fails to deliver must report TRUNCATED. Wrapping both in one
    try would let a real regression print the same word as an unavailable primitive -- and the
    previous version of this file guarded win32 by branching around the rlimit cases while
    still calling THIS one, so if socketpair or SO_SNDBUF behaves differently there, the guard
    I added to fix a Windows failure would itself have failed on Windows. I have no Windows
    machine to test on, so the honest fix is to make the outcome legible rather than to assume.
    """
    try:
        left, right = socket.socketpair()
    except (AttributeError, OSError) as error:
        print(f"success: SKIPPED (socketpair unavailable: {type(error).__name__})")
        return
    try:
        # A small send buffer makes the writes short; a reader that keeps draining makes each
        # subsequent one succeed, so the loop must run more than once to finish.
        try:
            left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        except OSError as error:
            print(f"success: SKIPPED (SO_SNDBUF not settable: {type(error).__name__})")
            return
        received = bytearray()

        def drain():
            while len(received) < len(PAYLOAD):
                chunk = right.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            _write_all(left.fileno(), PAYLOAD)
        except OSError as error:
            # os.write on a SOCKET fd is a POSIX affordance; on Windows a socket is a handle,
            # not a CRT file descriptor. Reported as SKIPPED rather than TRUNCATED so an
            # unavailable primitive never reads as the loop losing bytes.
            print(f"success: SKIPPED (os.write on a socket fd: {type(error).__name__} "
                  f"{getattr(error, 'errno', None)})")
            return
        # daemon=True above so a reader still blocked in recv cannot keep the interpreter alive
        # past this join -- the suite runs this probe as a subprocess and waits on it.
        reader.join(timeout=30)
        ok = bytes(received) == PAYLOAD
        print(f"success: _write_all delivered {len(received)} of {len(PAYLOAD)} bytes "
              f"-> {'ALL BYTES' if ok else 'TRUNCATED'}")
    finally:
        left.close()
        right.close()


def main():
    if sys.platform == "win32":
        # Reported, not silently skipped: a probe that prints nothing on one platform is
        # indistinguishable from one that was never wired up. The suite's other Windows-aware
        # section (windows_taskkill_path) is the precedent; this one lacked the guard entirely
        # and would have failed three rows on every Windows run from the commit that added it.
        print("control: SKIPPED on win32 (no resource module, no SIGXFSZ)")
        print("error: SKIPPED on win32")
        _success_case()
        return
    tmp = tempfile.mkdtemp()
    try:
        # The success case runs FIRST: the rlimit below lowers the HARD limit too, which cannot
        # be undone, and it would otherwise cap the socket case as well.
        _success_case()
        _size_capped_cases(tmp)
    finally:
        # The one probe here that deliberately writes files up to a size cap is the worst to
        # leave lying around; without this it left ~128 KiB behind on every `npm test`, forever.
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # Guarded: setrlimit lowers the HARD limit irreversibly and SIGXFSZ is set to ignore, both
    # of which were module-level side effects before, so importing this from anything else
    # would have permanently capped that process's file size.
    main()
