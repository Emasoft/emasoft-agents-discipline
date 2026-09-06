#!/usr/bin/env python3
# Keep a stable process-group leader alive until CHECK stdio closes.
# Zero dependencies. Python 3.11+ (the floor the ported ledger checker sets; see
# tests/python-lib-checks.py, which states and enforces it).

import os
import signal as signal_module
import subprocess
import sys
import threading

def _pump(src, dst):
    # Pipe instead of inheriting descriptors directly. Reading until EOF blocks on any
    # descendant that inherited CHECK's stdout/stderr, keeping this detached supervisor
    # alive as the original process-group identity until every writer has closed.
    for chunk in iter(lambda: src.read(65536), b""):
        dst.write(chunk)
        dst.flush()
    src.close()


def main():
    shell = sys.argv[1] if len(sys.argv) > 1 else None
    command = sys.argv[2] if len(sys.argv) > 2 else None
    if not shell or command is None:
        print("agents-discipline-check-supervisor: expected resolved shell and CHECK command", file=sys.stderr)
        sys.exit(2)

    # windowsHide: true -- hide the console window this leader would otherwise pop on Windows.
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    try:
        child = subprocess.Popen(
            command,
            cwd=os.getcwd(),
            shell=True,
            executable=shell,
            env=os.environ,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
    except OSError as error:
        print(f"agents-discipline-check-supervisor: could not start CHECK: {error}", file=sys.stderr)
        sys.exit(127)

    stdout_thread = threading.Thread(target=_pump, args=(child.stdout, sys.stdout.buffer))
    stderr_thread = threading.Thread(target=_pump, args=(child.stderr, sys.stderr.buffer))
    stdout_thread.start()
    stderr_thread.start()

    returncode = child.wait()
    stdout_thread.join()
    stderr_thread.join()

    if returncode >= 0:
        sys.exit(returncode)

    # A negative returncode means the process was terminated by a signal (POSIX convention);
    # Node instead reports (code=null, signal="SIGxxx") for the same event.
    sig = -returncode
    try:
        sig_name = signal_module.Signals(sig).name
    except ValueError:
        sig_name = "unknown signal"
    print(f"agents-discipline-check-supervisor: CHECK ended by {sig_name}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
