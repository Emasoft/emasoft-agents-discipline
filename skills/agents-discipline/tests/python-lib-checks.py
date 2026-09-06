#!/usr/bin/env python3
"""Behavioural checks for the ported Python libs that have no consumer yet.

`regex_worker`, `check_supervisor` and the Windows branch of `process_tree` were committed
with nothing driving them: their only verification was that they IMPORT, which is what passed
while process_tree still called `child.exitCode` and `child.kill(sig)` on a subprocess.Popen.
An unverified module reads as a verified one once it is committed under "ported", so each gets
the cheapest check that could actually fail.

Every case here is paired with the control that makes it non-vacuous — a NON-matching regex, a
non-zero exit, an untrusted env — because a stub that always answers "yes" passes the happy
half of all three.

Run: python3 tests/python-lib-checks.py   (also runs as the last step of `npm test`)
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "scripts", "lib")
sys.path.insert(0, LIB)

import process_tree  # noqa: E402  # type: ignore[import-not-found]

failed = 0


def report(ok, name, detail=""):
    global failed
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")
    if not ok:
        failed += 1


def regex_worker_reply(source, output, flags=""):
    proc = subprocess.run(
        [sys.executable, os.path.join(LIB, "regex_worker.py")],
        input=json.dumps({"source": source, "output": output, "flags": flags}) + "\n",
        capture_output=True, text=True, timeout=20,
    )
    return json.loads(proc.stdout)


# regex_worker: one message in, one reply out. The NON-matching case is the control -- a stub
# replying {"matched": true} to everything passes the first assertion alone.
report(regex_worker_reply("VERIFY_OK", "all good VERIFY_OK") == {"matched": True},
       "regex_worker: a matching EXPECT reports matched")
report(regex_worker_reply("VERIFY_OK", "nothing here") == {"matched": False},
       "regex_worker: a NON-matching EXPECT reports not-matched")
report("error" in regex_worker_reply("(", "x"),
       "regex_worker: an invalid pattern reports an error rather than crashing")
# \d must stay ASCII, as it is in JavaScript: the whole reason the port carries re.ASCII.
report(regex_worker_reply(r"^\d+$", "١٢٣") == {"matched": False},
       "regex_worker: \\d stays ASCII, as JavaScript has it")

# check_supervisor: the exit code alone is the vacuous half -- a supervisor that never pumped
# stdout would still propagate it. Assert the OUTPUT came through as well.
sup = subprocess.run(
    [sys.executable, os.path.join(LIB, "check_supervisor.py"), "/bin/bash", "echo hi; exit 3"],
    capture_output=True, text=True, timeout=20,
)
report(sup.returncode == 3, "check_supervisor: propagates the CHECK exit code", f"got {sup.returncode}")
report(sup.stdout.strip() == "hi", "check_supervisor: pumps CHECK stdout", repr(sup.stdout))

sup_sig = subprocess.run(
    [sys.executable, os.path.join(LIB, "check_supervisor.py"), "/bin/bash", "kill -9 $$"],
    capture_output=True, text=True, timeout=20,
)
report(sup_sig.returncode == 1 and "ended by SIGKILL" in sup_sig.stderr,
       "check_supervisor: a signalled CHECK exits 1 and names the signal",
       f"exit {sup_sig.returncode}")

# windows_taskkill_path is pure string logic over an env dict, so it is testable on POSIX --
# and it is the fail-closed decision that keeps an ARBITRARY executable from being selected,
# which makes it the most worth testing of anything in the Windows branch.
report(process_tree.windows_taskkill_path(
    {"SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows", "SystemDrive": "C:"}
) == "C:\\Windows\\System32\\taskkill.exe", "windows_taskkill_path: agreeing trusted roots select System32")
for bad, why in (
    ({"SystemRoot": "/tmp/evil", "WINDIR": "/tmp/evil", "SystemDrive": "C:"}, "a non-Windows root"),
    ({"SystemRoot": "C:\\Windows", "WINDIR": "D:\\Windows", "SystemDrive": "C:"}, "disagreeing roots"),
    ({"SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows", "SystemDrive": "D:"}, "a mismatched drive"),
    ({}, "an empty environment"),
):
    report(process_tree.windows_taskkill_path(bad) is None,
           f"windows_taskkill_path: fails closed on {why}")

# The POSIX group kill, driven for real. Prints its own line and raises on failure.
process_tree._self_check()

print("all pass" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
