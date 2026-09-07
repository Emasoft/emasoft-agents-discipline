#!/usr/bin/env python3
"""Prove gates.write_all handles a REAL short write, and that one can actually happen.

Runs as its own process because RLIMIT_FSIZE and the SIGXFSZ disposition are process-wide and
the rlimit's HARD half cannot be raised again afterwards.

Why a size limit and not a pipe: a blocking pipe never short-writes (POSIX requires write() to
transfer all nbyte before returning), and my first two attempts at this measured exactly that --
os.write returned the full buffer, a result equally consistent with "the loop is unnecessary".

Three lines, and the CONTROL comes first because it licenses the other two: if a bare os.write
did not short-write here, nothing below is evidence of anything.

  control  -- a bare os.write on a regular file under RLIMIT_FSIZE returns SHORT
  error    -- write_all on that same shape surfaces the failure instead of truncating
  success  -- write_all delivers EVERY byte, across MORE THAN ONE write, when writes short
              but do not fail

The third case is the one the fix exists for, and the first version of this probe did not have
it at all: "does not truncate SILENTLY" is not "does not truncate". Its second version had it
in name only -- see _success_case for the three real descriptors that each turned out to
short-write never, and for why the loop is driven by a stubbed os.write instead.
"""

import errno
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "lib"))
from gates import write_all, write_atomic  # noqa: E402  # type: ignore[import-not-found]

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
        write_all(fd, PAYLOAD)
        print("error: write_all returned without error (WRONG if the control short-wrote)")
    except OSError as error:
        print(f"error: write_all raised {type(error).__name__} errno={error.errno} "
              f"-> the caller SEES the failure")
    finally:
        os.close(fd)


def _success_case():
    """write_all across MULTIPLE short writes that SUCCEED -- the path the fix exists for.

    Driven by a STUBBED os.write, and that is the whole point rather than a shortcut. Measured
    on three real file descriptors, in this order, each attempt discarded when it turned out to
    prove nothing:

      - a blocking PIPE never short-writes: POSIX requires write() to transfer all nbyte.
      - a blocking SOCKET does not either, even with SO_SNDBUF forced to 4096. Instrumented on
        macOS: ONE os.write call returned all 262144 bytes. The socket version of this case sat
        in the suite printing "ALL BYTES" while the loop body ran exactly once -- a row named
        "across multiple short writes" that had never executed a second iteration.
      - a NON-blocking fd shorts, then raises EAGAIN, which write_all does not handle and
        should not: every fd it is given in production is a blocking regular file.

    So no real descriptor can deliver a short-write-then-succeed sequence, and the loop's
    reassembly was untestable through one. The unit under test is the LOOP; os.write is its
    collaborator, and stubbing a collaborator to return the counts the kernel will not produce
    on demand is the same technique this suite already uses to reach process_tree's group-kill
    failure branch. What is asserted is what the loop must guarantee: every byte delivered, in
    order, and MORE THAN ONE call made -- because "all bytes" alone is satisfied by the
    single-write path that was silently being measured before.
    """
    chunks = []
    real_write = os.write

    def short_write(fd, data):
        # 4 KiB at a time regardless of what is offered, which is what a kernel does when its
        # buffer is partly full. Records what it accepted so the caller can prove reassembly.
        view = bytes(data[:4096])
        chunks.append(view)
        return len(view)

    # This rebinds os.write PROCESS-WIDE, not just for gates: `gates.os` IS the global module
    # object (verified: `gates.os is os`), and there is no gates-local alias to patch instead.
    # Two things make that acceptable rather than merely convenient, and both were measured:
    # the window contains exactly one call and no other statement, and print() does NOT route
    # through the Python `os.write` name (checked by counting calls during a print), so the
    # probe's own output is unaffected. The fd is -1 so that if the stub ever failed to install,
    # the real os.write raises EBADF immediately instead of writing the payload somewhere.
    gates_module = sys.modules[write_all.__module__]
    gates_module.os.write = short_write
    try:
        write_all(-1, PAYLOAD)
    finally:
        gates_module.os.write = real_write
        # INSIDE the finally, and a raise rather than an `assert`. Placement: the check exists
        # for a future edit that moves the restore out of this block, and in that world a
        # RAISING write_all would skip both the restore and a check sitting after the
        # try/finally -- so the mutation control only ever exercised one of the two
        # combinations. Raise, because `python -O` and a stray PYTHONOPTIMIZE=1 in a CI image
        # both strip an assert, and a tripwire that vanishes under an env var is not one.
        if os.write is not real_write:
            raise RuntimeError("os.write was not restored")

    delivered = b"".join(chunks)
    ok = delivered == PAYLOAD and len(chunks) > 1
    print(f"success: write_all made {len(chunks)} write(s) delivering {len(delivered)} of "
          f"{len(PAYLOAD)} bytes -> {'ALL BYTES' if ok else 'TRUNCATED'}")


def _atomic_case(tmp):
    """The property the whole write_all fix exists for: a FAILED write leaves the target intact.

    Everything else here tests write_all in isolation. The claim that motivated it is about
    write_atomic -- "a truncated dispatch.json fsync'd and renamed into position" -- and nothing
    exercised write_atomic under a failure DURING the write. The gates_helpers driver has six
    write_atomic rows and every one is a SETUP failure (symlink, a file where a directory must
    be); none reaches the raise path, so the blast radius the fix was written for was unasserted.

    Three things must hold after the failure, and each is a separate way to lose data:
      - the pre-existing target is byte-identical (os.replace never ran);
      - no .tmp file survives (the finally unlinked it -- untested on the raise path);
      - the caller sees the error rather than a silent partial success.
    """
    target = os.path.join(tmp, "state.json")
    before = b'{"schema":1,"waves":{}}\n'
    with open(target, "wb") as handle:
        handle.write(before)

    calls = []
    real_write = os.write

    def fail_on_second(fd, data):
        calls.append(len(data))
        if len(calls) >= 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, bytes(data[:4096]))

    gates_module = sys.modules[write_all.__module__]
    gates_module.os.write = fail_on_second
    raised = None
    try:
        write_atomic(target, "x" * (64 * 1024))
    except OSError as error:
        raised = error
    finally:
        gates_module.os.write = real_write
        if os.write is not real_write:
            raise RuntimeError("os.write was not restored")

    with open(target, "rb") as handle:
        after = handle.read()
    leftovers = [n for n in os.listdir(tmp) if n.endswith(".tmp")]
    ok = raised is not None and after == before and not leftovers
    print(f"atomic: mid-write failure after {len(calls)} write(s) -> raised="
          f"{raised is not None} target_unchanged={after == before} "
          f"tmp_left={len(leftovers)} -> {'INTACT' if ok else 'DAMAGED'}")


def _no_progress_case():
    """The `written <= 0` guard added in 0fd2909, which nothing has ever executed.

    It exists to satisfy the plan's "no infinite loops" constraint, and a guard nobody has run
    is exactly what 3ed63fc turned out to be. The stub returns 0 forever, so an unguarded loop
    hangs here rather than failing -- which is why this case asserts a raise, not a value.
    """
    real_write = os.write
    gates_module = sys.modules[write_all.__module__]
    gates_module.os.write = lambda fd, data: 0
    raised = None
    try:
        write_all(-1, b"x" * 4096)
    except OSError as error:
        raised = error
    finally:
        gates_module.os.write = real_write
        if os.write is not real_write:
            raise RuntimeError("os.write was not restored")
    ok = raised is not None and "no progress" in str(raised)
    print(f"noprogress: a 0-return raised={raised is not None} "
          f"({raised}) -> {'BOUNDED' if ok else 'UNBOUNDED'}")


def main():
    if sys.platform == "win32":
        # Reported, not silently skipped: a probe that prints nothing on one platform is
        # indistinguishable from one that was never wired up. Only the rlimit cases are Unix
        # bound; the success case is pure Python over a stub now, so it runs everywhere -- which
        # also retires the question of whether os.write works on a Winsock handle, since the
        # probe no longer asks.
        print("control: SKIPPED on win32 (no resource module, no SIGXFSZ)")
        print("error: SKIPPED on win32")
        _success_case()
        _no_progress_case()
        _atomic_case(tempfile.mkdtemp())
        return
    tmp = tempfile.mkdtemp()
    try:
        # The success case runs FIRST. It no longer needs to (it is pure Python over a stub),
        # but the rlimit below lowers the HARD limit irreversibly, so keeping the unconstrained
        # case ahead of it stays correct if either ever touches a real descriptor again.
        _success_case()
        _no_progress_case()
        _atomic_case(tmp)
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
