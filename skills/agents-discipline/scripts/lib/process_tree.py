"""Best-effort process-tree cleanup shared by the gate runner and tests.
Zero dependencies. Python 3.11+ (the floor the ported ledger checker sets; see
tests/python-lib-checks.py, which states and enforces it).
"""

import ntpath
import os
import re
import signal
import subprocess
import sys
import time

WINDOWS_TASKKILL_TIMEOUT_MS = 1000

_DRIVE_ROOT = re.compile(r"^[A-Za-z]:\\Windows$", re.I)


def _normalize_root(value):
    s = str(value or "").replace("/", "\\")
    return re.sub(r"\\+$", "", s)


def windows_taskkill_path(env=None):
    if env is None:
        env = os.environ
    system_root = _normalize_root(env.get("SystemRoot"))
    windir = _normalize_root(env.get("WINDIR"))
    system_drive = re.sub(r"[\\/]+$", "", str(env.get("SystemDrive") or "")).upper()
    # Require the three standard Windows launcher values to identify the same
    # drive-root directory. A single arbitrary absolute variable must not select
    # an executable, and disagreement fails closed to the ChildProcess handle.
    if (_DRIVE_ROOT.match(system_root) and _DRIVE_ROOT.match(windir)
            and system_root.lower() == windir.lower()
            and system_drive == system_root[:2].upper()):
        return ntpath.join(system_root, "System32", "taskkill.exe")
    # A bare executable name consults cwd/PATH, which are controlled by the
    # CHECK environment. If neither trusted system root exists, skip the helper
    # and use the already-held ChildProcess handle instead.
    return None


def _sync_failure(result):
    if not result:
        return "returned no result"
    if result.get("error"):
        error = result["error"]
        return getattr(error, "code", None) or str(error) or "spawn error"
    if result.get("signal"):
        return "signal " + str(result["signal"])
    if result.get("status") != 0:
        return "exit " + str(result.get("status"))
    return None


def _child_exited(child):
    # `poll()`, not Node's `exitCode`/`signalCode`. A Python child is a subprocess.Popen and
    # has neither attribute, so the transliterated form raised AttributeError against every
    # real child -- and the module still imported cleanly, which is why "it imports" is not a
    # verification. poll() returns None while running and the (possibly negative, meaning
    # signalled) status once reaped, covering both Node fields in one call.
    return child.poll() is not None


def terminate_process_tree(child, options=None):
    options = options or {}
    platform = options.get("platform") or ("win32" if sys.platform == "win32" else sys.platform)
    spawn_sync_impl = options.get("spawnSyncImpl") or _default_spawn_sync
    kill_group = options.get("killGroup") or os.kill
    pid = getattr(child, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return {"ok": False, "fallback": False, "diagnostic": "child PID is unavailable"}

    if platform != "win32":
        # The gate runner launches a detached Python supervisor as the group leader
        # and keeps it alive until the shell and inherited stdout/stderr close. If
        # we have already observed that supervisor exit, the numeric PGID no
        # longer carries identity and may have been reused; never signal it.
        if _child_exited(child):
            return {"ok": True, "fallback": False, "diagnostic": "process supervisor already exited"}
        try:
            kill_group(-pid, signal.SIGKILL)
            return {"ok": True, "fallback": False, "diagnostic": None}
        except OSError as error:
            if _child_exited(child):
                return {
                    "ok": True,
                    "fallback": True,
                    "diagnostic": "process-group kill failed (" + (error.strerror or str(error)) +
                        "); supervisor already exited",
                }
            try:
                requested = _child_kill(child, signal.SIGKILL)
                if requested is False:
                    return {
                        "ok": False,
                        "fallback": True,
                        "diagnostic": "process-group kill failed (" + (error.strerror or str(error)) +
                            "); child fallback returned false",
                    }
                return {
                    "ok": True,
                    "fallback": True,
                    "diagnostic": "process-group kill failed (" + (error.strerror or str(error)) +
                        "); child fallback requested",
                }
            except OSError as fallback_error:
                return {
                    "ok": False,
                    "fallback": True,
                    "diagnostic": "process-group kill failed (" + (error.strerror or str(error)) +
                        "); child fallback failed (" + (fallback_error.strerror or str(fallback_error)) + ")",
                }

    # taskkill addresses the stored leader PID, unlike a POSIX process-group
    # request. Do not target it after the process reports exit because the PID may have
    # been reused while an inherited pipe remains open.
    if _child_exited(child):
        return {"ok": True, "fallback": False, "diagnostic": "child already exited"}

    command = windows_taskkill_path(options.get("env") or os.environ)
    try:
        requested_timeout = float(options.get("taskkillTimeoutMs"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        requested_timeout = float("nan")  # `x == x` below is the NaN test
    taskkill_timeout_ms = (
        int(requested_timeout) if requested_timeout == requested_timeout and requested_timeout > 0
        else WINDOWS_TASKKILL_TIMEOUT_MS
    )
    result = None
    failure = None
    if not command:
        failure = "trusted system taskkill path unavailable"
    else:
        try:
            result = spawn_sync_impl(
                command, ["/pid", str(pid), "/f", "/t"],
                # Cleanup runs inside the gate timeout path. A broken helper must not
                # replace a bounded CHECK with an unbounded synchronous wait.
                timeout_ms=taskkill_timeout_ms,
            )
            failure = _sync_failure(result)
        except OSError as error:
            failure = error.strerror or str(error) or "spawn threw"

    if not failure:
        return {"ok": True, "fallback": False, "diagnostic": None, "command": command}

    if _child_exited(child):
        return {
            "ok": True,
            "fallback": True,
            "command": command,
            "diagnostic": "taskkill failed (" + failure + "); child already exited",
        }

    try:
        requested = _child_kill(child, signal.SIGKILL)
        if requested is False:
            return {
                "ok": False,
                "fallback": True,
                "command": command,
                "diagnostic": "taskkill failed (" + failure + "); child fallback returned false",
            }
        return {
            "ok": True,
            "fallback": True,
            "command": command,
            "diagnostic": "taskkill failed (" + failure + "); child fallback requested",
        }
    except OSError as error:
        return {
            "ok": False,
            "fallback": True,
            "command": command,
            "diagnostic": "taskkill failed (" + failure + "); child fallback failed (" +
                (error.strerror or str(error)) + ")",
        }


def _child_kill(child, sig):
    # An OSError from here propagates to the caller's `except OSError`, which reports "child
    # fallback failed" — where the oracle's kill() returns false for both ESRCH and EPERM and
    # so reports "returned false". Diagnostic-only (`ok` is False either way), and ESRCH is
    # unreachable for a Popen-owned child anyway: send_signal polls first, and a zombie is
    # still signalable, so os.kill cannot answer ESRCH here.
    # NOT REACHABLE PARITY, and chasing it further would be pretending: Node's `kill()` asks
    # "have I NOTICED the exit yet?" and poll() asks "has it exited?". Between the OS reaping
    # a process and libuv delivering the exit event, the oracle signals a zombie, succeeds and
    # returns true where this returns False. That flips the caller to {"ok": False} -- a false
    # alarm about cleanup, never a false pass, so the residual divergence errs safely.
    # Node's `child.kill(sig)` takes a signal and RETURNS a boolean; Popen.kill() takes no
    # argument and Popen.send_signal() raises ProcessLookupError when the child is already
    # gone. Translating the raise into the oracle's `false` keeps the caller's three-way
    # result (requested / returned false / threw) meaning the same thing in both runtimes.
    # The poll() guard is NOT redundant with the caller's: measured, `send_signal` on an
    # already-REAPED Popen returns silently (Popen suppresses it) instead of raising, so
    # without this the fallback reports "requested" for a signal nobody received, where the
    # oracle's `child.kill()` returns false. The caller checks exit before entering, but the
    # child can die in the window between that check and this call -- which is precisely the
    # case the fallback exists for.
    # Decided AFTER the call, on the poll send_signal already did. A pre-call `poll()` guard
    # only NARROWS the window, it cannot close it: CPython's send_signal polls internally and
    # returns silently when the child has exited, so a child dying between the guard and the
    # call is reported as "requested" for a signal nobody received. Measured: returncode None
    # before the call, 7 after, guard-form returns True. That window is exactly the one the
    # fallback path exists for, since the caller already checked exit before entering.
    child.send_signal(sig)
    return child.returncode is None


def _self_check():
    """Kill a REAL detached group with a backgrounded grandchild, then prove it is gone.

    The point of this module is reaping descendants, and "the module imports" cannot see a
    single thing about that -- it is what passed while `child.exitCode` and `child.kill(sig)`,
    neither of which exists on a Popen, were still in the code.
    """
    if sys.platform == "win32":
        print("self-check skipped: POSIX group kill only")
        return
    child = subprocess.Popen(
        # BOTH sleeps backgrounded, bash blocking in `wait`: three processes exist by
        # construction. `sleep 47 & sleep 47` leaves the last command exposed to bash's
        # fork-suppression optimisation, which can exec it and yield two members on some
        # versions -- an assertion betting on shell internals rather than on this module.
        ["/bin/bash", "-c", "sleep 47 & sleep 47 & wait"],
        start_new_session=True,  # its own process group, so -pid addresses the whole tree
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(child.pid)

    # ps snapshot FIRST, then read it: a live `pgrep -f`/`ps | grep` matches the very shell
    # running the pipeline, because that shell carries the pattern in its own argv.
    # `.split()`, never `.split(" ")`: ps RIGHT-ALIGNS the pgid column, so with a wider pgid
    # anywhere on the machine every line begins with a space and `split(" ")[0]` is `''`.
    # That made the survivor filter match nothing and the assertion below unfireable -- a
    # check that passes because it cannot see, which is the failure it exists to catch.
    def members():
        snapshot = subprocess.run(
            ["ps", "-eo", "pgid,command"], capture_output=True, text=True, timeout=10
        ).stdout
        out = []
        for line in snapshot.splitlines():
            head = line.split()
            if head and head[0].isdigit() and int(head[0]) == pgid:
                out.append(line.strip())
        return out

    # POSITIVE CONTROL, and it is the whole reason this check is worth anything: prove the
    # detector CAN see the group while it is alive. Without it, "no survivors" afterwards is
    # equally consistent with a perfect kill and with a parser that never matches.
    # Polled, because Popen returns as soon as the fork succeeds: the first snapshot caught
    # bash plus ONE sleep, the other not yet spawned. The control fired on that -- an
    # assertion that failed for the right reason before it ever passed.
    try:
        _run_self_check(child, pgid, members)
    finally:
        # A leak-detection check that LEAKS on failure is the irony this guard exists to
        # avoid: every assertion below fires while two `sleep 47` processes are alive, and an
        # unguarded raise would strand them for 47 seconds each time the check is run.
        # Belt-and-braces, because the success path has already reaped the group: killpg on an
        # empty group raises ProcessLookupError, which is the expected case here, not an error.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _run_self_check(child, pgid, members):
    alive = []
    for _ in range(40):
        alive = members()
        if len(alive) >= 3:
            break
        time.sleep(0.05)
    assert len(alive) >= 3, f"expected the bash and its two sleeps in group {pgid}, saw {alive}"

    result = terminate_process_tree(child)
    assert result["ok"], result
    child.wait(timeout=5)
    # Polled, not one snapshot: `wait` reaps only the DIRECT child, so a backgrounded sleep is
    # reparented to init and lingers as a <defunct> entry still carrying the pgid for a moment
    # after the kill. A single snapshot in that window reads it as a survivor and fails a
    # correct kill. (`members()` matches on pgid alone, so a recycled pgid inside the window
    # would read as a survivor too -- vanishingly unlikely, and named rather than guarded.)
    survivors = []
    for _ in range(40):
        survivors = members()
        if not survivors:
            break
        time.sleep(0.05)
    assert not survivors, f"group {pgid} survived the kill: {survivors}"
    print(f"self-check ok: group {pgid} had {len(alive)} members, all reaped "
          "(including the backgrounded grandchild)")


def _default_spawn_sync(command, args, timeout_ms=None):
    proc = subprocess.run(
        [command] + args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=(timeout_ms / 1000 if timeout_ms else None),
    )
    return {"status": proc.returncode}


if __name__ == "__main__":
    _self_check()
