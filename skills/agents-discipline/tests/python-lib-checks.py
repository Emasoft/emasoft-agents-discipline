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
    # BOTH replies, not just the port's: a vacuity control built on one side only re-creates
    # the one-sidedness this whole file was rewritten to remove.
    return json.dumps(j, sort_keys=True), json.dumps(p, sort_keys=True)


matched = regex_case("a matching EXPECT", "VERIFY_OK", "all good VERIFY_OK")
unmatched = regex_case("a NON-matching EXPECT", "VERIFY_OK", "nothing here")
# The vacuity control: two implementations each hardcoding one reply would agree on every case
# above. Neither can also produce DIFFERENT replies for match and non-match — asserted of BOTH
# sides, because checking only the port would leave a constant-answering ORACLE undetected and
# every "port matches oracle" line above trivially true.
report(matched is not None and unmatched is not None
       and matched[0] != unmatched[0] and matched[1] != unmatched[1],
       "regex_worker: match and non-match differ, in BOTH runtimes (vacuity control)",
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
    # did the same. Exactly one environment must select a path, and four must fail closed —
    # asserted of BOTH lists, since checking only the port leaves a constant-None ORACLE
    # undetected and makes the equality above trivially true.
    report(all(sum(1 for p in paths if p) == 1 and paths[0] for paths in (js_paths, py_paths)),
           "windows_taskkill_path: only the trusted environment selects a path, in BOTH "
           "runtimes (vacuity control)", str(py_paths[0]))

# --- the group kill: the module's whole purpose, and the last case still asserted ----------
# Three real defects had already been found in this exact path (`child.exitCode`,
# `child.kill(sig)`, the send_signal window) and every one of them was invisible to a check
# that drove only one side. Both implementations now spawn their own detached group with a
# backgrounded grandchild, reap it, and report the same five facts.
if WIN32:
    report(True, "process_tree: group kill skipped (POSIX only)")
else:
    py_kill = process_tree._self_check()
    js_kill_out = subprocess.run(["node", os.path.join(TESTS, "process-tree-drive.mjs")],
                                 capture_output=True, text=True, timeout=60)
    if js_kill_out.returncode != 0:
        report(False, "process_tree: oracle could not be driven", js_kill_out.stderr.strip()[:160])
    else:
        js_kill = json.loads(js_kill_out.stdout)
        report(js_kill == py_kill, "process_tree: group kill — port matches oracle",
               "" if js_kill == py_kill else f"js={js_kill} py={py_kill}")
        # Vacuity control: both would agree on {membersBefore: 0, survivors: 0} if neither
        # spawned anything at all, which is also what a broken `ps` parse looks like.
        report(js_kill["membersBefore"] >= 3 and py_kill["membersBefore"] >= 3,
               "process_tree: both saw a live 3-member group before killing it (vacuity control)",
               f"js={js_kill['membersBefore']} py={py_kill['membersBefore']}")

print("all pass" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
