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

import atexit
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

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

# A crash anywhere below (a subprocess timeout, a KeyError, a failed assert inside a driver)
# truncates the run: every later section is skipped and NOTHING says so — the reader gets a
# traceback naming one line and no signal that eight other comparisons were never attempted.
# 8eb3710 named that gap in a commit message instead of closing it, which is the "filing is
# not doing" failure: a documented hole is still a hole. atexit rather than wrapping each
# section in try/except, because it needs no re-indentation and it reports the same fact.
SECTIONS = ["regex_worker", "check_supervisor", "windows_taskkill_path", "process_tree"]
completed = []


def _missing_sections():
    return [s for s in SECTIONS if s not in completed]


# Bound BEFORE the decorator runs. Globals resolve at call time so the order is not a bug
# today, but anything raising between the two lines would fire the hook against an unbound
# name and die inside atexit — noise that changes nothing, for one line of ordering.
_checked_completeness = False


@atexit.register
def _report_truncation():
    # CRASH path only. On a normal end the check below has already run and fed `failed`;
    # here the interpreter is already exiting non-zero from the traceback, so this only has
    # to SAY which sections were lost. It cannot influence the exit code: atexit runs after
    # sys.exit has fixed it, which is exactly why it is not the whole guard.
    missing = _missing_sections()
    if missing and not _checked_completeness:
        print(f"FAIL  suite TRUNCATED — these sections never completed: {', '.join(missing)}")


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
completed.append("regex_worker")

# --- check_supervisor: same argv, compare exit code AND the pumped output ------------------
if WIN32:
    report(True, "check_supervisor: skipped (the cases drive /bin/bash)")
else:
    seen = {}
    # The spawn-failure case is the branch that decides whether a BROKEN CHECK reads as a
    # failure or as a crash, and it was never driven. It found a divergence at once: exit 127
    # agreed, but the oracle prints `CHECK spawn failed: spawn <file> ENOENT` (Node's
    # err.message shape) and the port printed `could not start CHECK: [Errno 2] ...`.
    # A second spawn failure with a DIFFERENT errno, because one sample cannot tell a
    # faithful port from a fitted one: `spawn <file> ENOENT` could have been hardcoded and
    # still pass. Measured, both runtimes print `spawn <file> EACCES` here.
    noexec_dir = tempfile.mkdtemp()
    noexec = os.path.join(noexec_dir, "noexec.sh")
    with open(noexec, "w") as fh:
        fh.write("#!/bin/sh\necho hi\n")
    os.chmod(noexec, 0)
    # Removed at the end of the section, because a suite that leaks a temp dir on every run
    # is the same defect this file has now fixed twice (the self-check's stranded sleeps, the
    # oracle driver's missing finally) — and it would be the third, introduced by a case
    # written to prove rigour. chmod 000 does not stop the OWNER unlinking it.
    atexit.register(lambda: shutil.rmtree(noexec_dir, ignore_errors=True))

    for shell, name, script in (
        ("/bin/bash", "propagates exit code and pumps stdout", "echo hi; exit 3"),
        ("/bin/bash", "a signalled CHECK", "kill -9 $$"),
        ("/nonexistent/sh", "an unspawnable shell (ENOENT)", "echo hi"),
        (noexec, "a non-executable shell (EACCES)", "echo hi"),
    ):
        js, py = both(["check-supervisor.mjs", shell, script],
                      ["check_supervisor.py", shell, script])
        report(js == py, f"check_supervisor: {name} — port matches oracle",
               "" if js == py else f"js={js} py={py}")
        seen[script] = (js, py)
        # Agreement alone is not enough for the spawn-failure cases: if chmod 000 did not
        # actually deny (running as root, or a filesystem that ignores the mode), both
        # runtimes would spawn SUCCESSFULLY and agree just as well. Assert the errno the case
        # is named for actually appeared.
        for want_code in (["ENOENT"] if shell == "/nonexistent/sh" else
                          ["EACCES"] if shell == noexec else []):
            report(want_code in js[2] and want_code in py[2],
                   f"check_supervisor: {name} really failed with {want_code} (vacuity control)",
                   js[2][:80])
    # Vacuity control: a supervisor that ignored its CHECK entirely would give identical
    # (0, "", "") on both scripts. These two must differ from each other, in BOTH runtimes —
    # the results are reused from the loop rather than re-run, which also stops the two
    # adjacent controls in this file from checking opposite sides (this one used to sample the
    # oracle while the regex one sampled the port).
    # Argv validation: the supervisor's other refusal, and the last uncovered branch here.
    js_argv, py_argv = both(["check-supervisor.mjs"], ["check_supervisor.py"])
    report(js_argv == py_argv, "check_supervisor: missing argv — port matches oracle",
           "" if js_argv == py_argv else f"js={js_argv} py={py_argv}")
    report(js_argv[0] == 2 and "expected resolved shell" in js_argv[2],
           "check_supervisor: missing argv really refused with exit 2 (vacuity control)",
           f"exit {js_argv[0]}")

    (ja, pa), (jb, pb) = seen["echo hi; exit 3"], seen["kill -9 $$"]
    report(ja != jb and pa != pb,
           "check_supervisor: the two CHECKs differ, in BOTH runtimes (vacuity control)",
           f"{pa} vs {pb}")
completed.append("check_supervisor")

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
     # A file:// URI, JSON-quoted. Two separate defects, and fixing only the first leaves the
     # case broken with a quieter message:
     #   1. The path lands inside a JS string literal, and a Windows path is backslash
     #      escapes. Measured: node reads "C:\Users\test\scripts\lib\x.mjs" as
     #      "C:Users<TAB>estscriptslibx.mjs" — \U, \l, \p lose the backslash, \t is a tab.
     #   2. Even correctly escaped, a bare `C:\...` is not a valid ESM specifier — Node's
     #      loader wants a file:// URL and answers ERR_UNSUPPORTED_ESM_URL_SCHEME.
     # as_uri() answers both: no backslashes, no quotes, percent-encoded, and it is the form
     # Node documents for every platform. Verified to import on POSIX; the Windows half rests
     # on Node's documented specifier rule, not on a run — there is no Windows machine here.
     f'import {{ windowsTaskkillPath }} from '
     f'{json.dumps(pathlib.Path(os.path.join(LIB, "process-tree.mjs")).as_uri())};'
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
completed.append("windows_taskkill_path")

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

    # The FALLBACK arm, reached by injecting a group kill that throws ESRCH. It holds
    # `_child_kill`, which has been fixed three times and had never been executed by any test
    # in either runtime — every defect this module produced lives off the happy path the
    # comparison above walks. It found one immediately: the oracle reads `error.code` (the
    # errno NAME) and the port read `error.strerror`, so the same failure printed
    # "process-group kill failed (ESRCH)" in one runtime and "(No such process)" in the other.
    py_fb = process_tree._self_check(fail_group_kill=True)
    js_fb_out = subprocess.run(
        ["node", os.path.join(TESTS, "process-tree-drive.mjs"), "--fail-group-kill"],
        capture_output=True, text=True, timeout=60)
    if js_fb_out.returncode != 0:
        report(False, "process_tree: oracle fallback could not be driven",
               js_fb_out.stderr.strip()[:160])
    else:
        js_fb = json.loads(js_fb_out.stdout)
        report(js_fb == py_fb, "process_tree: group-kill FALLBACK — port matches oracle",
               "" if js_fb == py_fb else f"js={js_fb} py={py_fb}")
        # Vacuity control: if the injection did not take, this would be the happy path again
        # and would agree just as well. The fallback flag must actually be set.
        # Vacuity control: if the injection did not take this would be the happy path again
        # and would agree just as well. `fallback is True` on BOTH is the load-bearing half
        # (the diagnostic substring is one-sided, and only transitive through the equality
        # above); `survivorsNonZero` on both is what says the fallback did the NARROWER thing
        # it claims — signalled the direct child and left the group alone.
        # `.get`, not `[...]`: if a future edit made both runtimes emit the happy-path shape,
        # the equality above would PASS and this line would raise KeyError — a traceback where
        # a FAIL row belongs, and (until the inline completeness check below) a truncated run.
        report(py_fb["fallback"] is True and js_fb["fallback"] is True
               and py_fb.get("survivorsNonZero") is True
               and js_fb.get("survivorsNonZero") is True
               and "fallback requested" in (py_fb["diagnostic"] or ""),
               "process_tree: the injected failure really reached the fallback (vacuity control)",
               str(py_fb["diagnostic"]))
completed.append("process_tree")

# INLINE, before the exit, so a missing section actually FAILS the run. The atexit hook alone
# was decorative for the case it was written for: a section that returns early WITHOUT raising
# leaves `failed` at 0, `sys.exit(0)` is evaluated first, and the hook then prints "FAIL" onto
# a run that already succeeded. Measured before this fix: exit 0, "all pass", and the FAIL
# line underneath it — npm test green on a truncated suite.
_checked_completeness = True
for _s in _missing_sections():
    report(False, f"suite TRUNCATED — section never completed: {_s}")

print("all pass" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
