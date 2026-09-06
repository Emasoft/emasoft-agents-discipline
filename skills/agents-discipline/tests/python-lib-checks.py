#!/usr/bin/env python3
"""Differential checks for the ported Python libs that have no consumer yet.

`regex_worker`, `check_supervisor` and the Windows branch of `process_tree` were committed
with nothing driving them: their only verification was that they IMPORT, which is what passed
while process_tree still called `child.exitCode` and `child.kill(sig)` on a subprocess.Popen.

Every case runs BOTH implementations on the same input and compares. The first version of this
file asserted literals instead — `"ended by SIGKILL" in stderr`, `{"matched": False}` — which
is the wrong axis for a port: it establishes that the Python does SOMETHING, not that it
agrees with the JavaScript, and the whole discipline here is to hold the oracle fixed and vary
only the implementation. An assertion written from a belief about the oracle reads as parity
and will be cited as parity; when the oracle changes, nothing notices.

Vacuity controls are kept where "both agree" could be satisfied trivially: two stubs each
answering a constant would agree with each other, so the differing pair must also differ.

Run: python3 tests/python-lib-checks.py   (also runs as the last step of `npm test`)
"""

import json
import os
import subprocess
import sys

# The suite declares `engines: node >=16` and declared no Python floor at all — while
# ledger_check.py already needed 3.11 (datetime.fromisoformat did not accept a colon-less
# `+0000` offset before it). This file shells out rather than importing the checker, so
# without this guard it would PASS on 3.9 while `AD_RUNTIME=python` failed: the check that
# runs by default would be the one that cannot see the floor.
if sys.version_info < (3, 11):
    sys.exit(f"agents-discipline: the Python port needs 3.11+ (fromisoformat offsets), "
             f"found {sys.version.split()[0]}")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "scripts", "lib")
WIN32 = sys.platform == "win32"

failed = 0


def report(ok, name, detail=""):
    global failed
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")
    if not ok:
        failed += 1


TESTS = os.path.dirname(os.path.abspath(__file__))


def both(js_argv, py_argv, stdin=None, js_dir=LIB):
    """Run the oracle and the port on the same input; return (oracle, port) results."""
    out = []
    for argv in (["node", os.path.join(js_dir, js_argv[0])] + js_argv[1:],
                 [sys.executable, os.path.join(LIB, py_argv[0])] + py_argv[1:]):
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=30)
        out.append((p.returncode, p.stdout.strip(), p.stderr.strip()))
    return out


# --- regex_worker: one JSON message in, one reply out -------------------------------------
# The oracle is a node:worker_threads Worker and talks over parentPort, so it cannot be run as
# a subprocess at all — `node regex-worker.mjs` with a line of stdin produces NOTHING. The
# port moved the same one-shot contract onto stdio. regex-worker-drive.mjs bridges that so
# both sides can be driven on the same input; only the REPLY is comparable, which is the
# contract the caller depends on anyway. (The first version of this file asserted literals and
# never noticed the oracle could not be started.)
def regex_case(name, source, output, flags=""):
    msg = json.dumps({"source": source, "output": output, "flags": flags}) + "\n"
    (jc, jo, _), (_, po, pe) = both(["regex-worker-drive.mjs"], ["regex_worker.py"],
                                    stdin=msg, js_dir=TESTS)
    if jc != 0 or not jo:
        report(False, f"regex_worker: {name}", f"oracle did not reply (exit {jc})")
        return None
    try:
        j, p = json.loads(jo), json.loads(po)
    except json.JSONDecodeError:
        report(False, f"regex_worker: {name}", f"unparseable reply js={jo!r} py={po!r} {pe[:80]}")
        return None
    # Parsed, not raw: the two serialisers differ on whitespace (`{"matched":true}` vs
    # `{"matched": true}`) and the caller parses the reply, so byte equality would fail four
    # cases over a space. And on an INVALID pattern only the SHAPE is comparable — the message
    # text is the engine's own ("Invalid regular expression: /(/: Unterminated group" vs
    # "missing ), unterminated subpattern at position 0"), so demanding equality there would
    # be demanding the port reimplement V8's diagnostics. The contract is `error` present.
    ok = sorted(j) == sorted(p) and (("error" in j) or j == p)
    report(ok, f"regex_worker: {name} — port matches oracle",
           "" if ok else f"js={j} py={p} {pe[:80]}")
    return json.dumps(p, sort_keys=True)


matched = regex_case("a matching EXPECT", "VERIFY_OK", "all good VERIFY_OK")
unmatched = regex_case("a NON-matching EXPECT", "VERIFY_OK", "nothing here")
# The vacuity control: two implementations each hardcoding one reply would agree on every case
# above. They cannot also produce DIFFERENT replies for match and non-match.
report(matched is not None and matched != unmatched,
       "regex_worker: match and non-match give different replies (vacuity control)",
       f"{matched} vs {unmatched}")
regex_case("an invalid pattern", "(", "x")
# \d must stay ASCII: JS's is [0-9] with or without the `u` flag, Python's is Unicode by
# default. Compared rather than asserted, so a change on either side is caught.
regex_case("an ASCII-only \\d against Arabic-Indic digits", r"^\d+$", "١٢٣")

# --- check_supervisor: same argv, compare exit code AND the pumped output ------------------
if WIN32:
    report(True, "check_supervisor: skipped (the cases drive /bin/bash)")
else:
    for name, script in (
        ("propagates exit code and pumps stdout", "echo hi; exit 3"),
        ("a signalled CHECK", "kill -9 $$"),
    ):
        js, py = both(["check-supervisor.mjs", "/bin/bash", script],
                      ["check_supervisor.py", "/bin/bash", script])
        report(js == py, f"check_supervisor: {name} — port matches oracle",
               "" if js == py else f"js={js} py={py}")
    # Vacuity control: a supervisor that ignored its CHECK entirely would give identical
    # (0, "", "") on both scripts. These two must differ from each other.
    a, _ = both(["check-supervisor.mjs", "/bin/bash", "echo hi; exit 3"],
                ["check_supervisor.py", "/bin/bash", "echo hi; exit 3"])
    b, _ = both(["check-supervisor.mjs", "/bin/bash", "kill -9 $$"],
                ["check_supervisor.py", "/bin/bash", "kill -9 $$"])
    report(a != b, "check_supervisor: the two CHECKs give different results (vacuity control)",
           f"{a} vs {b}")

# --- windows_taskkill_path: pure string logic, so it is testable on POSIX ------------------
# It is the fail-closed decision that keeps an ARBITRARY executable from being selected, which
# makes it the most worth testing of anything in the Windows branch. Compared against the
# oracle's own function rather than against my reading of it.
ENVS = [
    {"SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows", "SystemDrive": "C:"},
    {"SystemRoot": "/tmp/evil", "WINDIR": "/tmp/evil", "SystemDrive": "C:"},
    {"SystemRoot": "C:\\Windows", "WINDIR": "D:\\Windows", "SystemDrive": "C:"},
    {"SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows", "SystemDrive": "D:"},
    {},
]
js_out = subprocess.run(
    ["node", "--input-type=module", "-e",
     f'import {{ windowsTaskkillPath }} from "{os.path.join(LIB, "process-tree.mjs")}";'
     f'console.log(JSON.stringify({json.dumps(ENVS)}.map((e) => windowsTaskkillPath(e) ?? null)));'],
    capture_output=True, text=True, timeout=30,
)
sys.path.insert(0, LIB)
import process_tree  # noqa: E402  # type: ignore[import-not-found]

if js_out.returncode != 0:
    report(False, "windows_taskkill_path: oracle could not be driven", js_out.stderr.strip()[:120])
else:
    js_paths = json.loads(js_out.stdout)
    py_paths = [process_tree.windows_taskkill_path(e) for e in ENVS]
    report(js_paths == py_paths, "windows_taskkill_path: port matches oracle on 5 environments",
           "" if js_paths == py_paths else f"js={js_paths} py={py_paths}")
    # Vacuity control: a function that returned None for everything would match a port that
    # did the same. Exactly one environment must select a path, and four must fail closed.
    report(sum(1 for p in py_paths if p) == 1 and py_paths[0],
           "windows_taskkill_path: only the trusted environment selects a path (vacuity control)",
           str(py_paths[0]))

# The POSIX group kill, driven for real. Prints its own line and raises on failure.
process_tree._self_check()

print("all pass" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
