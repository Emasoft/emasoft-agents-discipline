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

import os
import sys
# BEFORE any local import. A stale .pyc in scripts/lib/__pycache__ once executed while
# inspect.getsource() read the CORRECTED .py: the source looked right, every branch
# condition evaluated true, and the output was still wrong -- two runs of "the same"
# code disagreeing, which is worse than a failure because the natural reaction is to
# distrust the newer measurement.
#
# CORRECTED SCOPE, measured: `sys.dont_write_bytecode` stops this process WRITING a
# .pyc. It does NOT stop Python READING an existing one, and it is NOT inherited by a
# child -- so with only that line, a full run of this file still left three .pyc files
# behind, written by the very subprocesses it spawns. The env var is what reaches the
# children. Neither prevents a stale .pyc that some OTHER tool already wrote from
# being used; nothing in-process can. That residual is why the incident was resolved
# by DELETING __pycache__, and the honest claim is prevention of creation, not of use.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
import atexit
import json
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import time

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
SECTIONS = ["regex_worker", "check_supervisor", "windows_taskkill_path", "process_tree",
            "gates_helpers", "jsapi", "parse_gates", "short_write", "enoent_probe", "dispatch"]
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
# THE TWO FLAG MAPS MUST AGREE. `regex_worker._FLAG_MAP` (used to MATCH) and
# `gates._JS_FLAG_MAP` (used by `parse_regex` to VALIDATE) are separate dicts with identical
# values today, and nothing makes them stay that way. A drift is silent and vacuous in the
# worst direction: an EXPECT validated case-insensitively but matched case-sensitively still
# "works" on every pattern where the flag is irrelevant, so the suite stays green.
#
# ASSERTED, not fixed by importing `gates` into the worker. That import was tried and reverted:
# it makes the per-match subprocess pay gates.py's import on every EXPECT (MEASURED: 16.0ms ->
# 27.2ms), and a coupling is a heavier answer than an equality check. The cost is small against
# the 5000ms startup budget -- the reason to prefer the check is that it states the invariant
# where a reader can see it, instead of implying it through an import.
# PLAIN IMPORTS, with LIB on sys.path -- the idiom this file already uses for `process_tree`.
# An importlib path-load was tried and dropped for two reasons, both measured: `gates.py` does
# `from jsapi import ...`, so a path-load without the sys.path insert raises
# ModuleNotFoundError and aborted the ENTIRE suite (every later section reported as
# never-completed); and once the insert is there, the alias-loaded module is a SEPARATE object
# from the one a later `import gates` produces (`m is gates` -> False), so gates.py's
# module-level code simply ran twice for no benefit.
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import gates as _g  # noqa: E402  # type: ignore[import-not-found]
import regex_worker as _rw  # noqa: E402  # type: ignore[import-not-found]

report(_rw._FLAG_MAP == _g._JS_FLAG_MAP,
       "regex_worker: its flag map equals gates._JS_FLAG_MAP (drift guard)",
       f"worker={_rw._FLAG_MAP} gates={_g._JS_FLAG_MAP}")
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
        # Keyed by (shell, script): both spawn-failure cases run "echo hi", so keying on the
        # script alone silently overwrote the ENOENT entry with the EACCES one. Harmless while
        # only the two /bin/bash entries are read back, and a bug the moment they are not.
        seen[(shell, script)] = (js, py)
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

    (ja, pa), (jb, pb) = seen[("/bin/bash", "echo hi; exit 3")], seen[("/bin/bash", "kill -9 $$")]
    report(ja != jb and pa != pb,
           "check_supervisor: the two CHECKs differ, in BOTH runtimes (vacuity control)",
           f"{pa} vs {pb}")

    # THE KILL PATH. Every case above runs a CHECK that TERMINATES ON ITS OWN, so `both()`
    # reads to EOF and the pumped output arrives no matter how the supervisor buffers. The row
    # named "propagates exit code and pumps stdout" was GREEN throughout a real bug in which
    # the supervisor stranded a hung CHECK's output and never forwarded it (fixed in the commit
    # that adds these rows: `_pump` used BufferedReader.read(65536), which BLOCKS until it has
    # 65536 bytes or EOF, so a CHECK that prints a little and then hangs left those bytes in the
    # supervisor until the per-check timeout SIGKILLed it). A test whose NAME claims the property
    # the bug violated is worse than no test -- it is why nobody looked here.
    #
    # So: print, then hang; read for a bounded window; kill; assert what was pumped BEFORE the
    # kill. This is the only row in the file that observes the supervisor mid-stream.
    kill_path = []
    for argv in (["node", os.path.join(LIB, "check-supervisor.mjs"), "/bin/bash", "echo hi; sleep 30"],
                 [sys.executable, os.path.join(LIB, "check_supervisor.py"), "/bin/bash", "echo hi; sleep 30"]):
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            time.sleep(2.0)                     # long enough for `echo` to have been forwarded
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            # The pipe keeps whatever was written before the writer died; read it after the kill.
            kill_path.append((proc.stdout.read() if proc.stdout else b"").decode("utf-8", "replace").strip())
        finally:
            # Constraint: no process this suite spawns may outlive the case that spawned it.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            if proc.stdout:
                proc.stdout.close()
            proc.wait(timeout=10)
    js_killed, py_killed = kill_path
    report(js_killed == py_killed,
           "check_supervisor: output printed before a mid-stream KILL is pumped — port matches oracle",
           f"js={js_killed!r} py={py_killed!r}")
    # Vacuity control: the row above passes trivially if BOTH strand the output (both ""), which
    # is exactly the pre-fix state. The oracle is the specification, so assert it really pumped.
    report(js_killed == "hi",
           "check_supervisor: the ORACLE really pumped before the kill (vacuity control)",
           f"js={js_killed!r}")
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

# --- gates_helpers: scope resolution, atomic write, file lock, status log -----------------
# The five helpers lib/dispatch.mjs imports. Whole-effect comparison rather than assertions:
# four of the five are side-effecting, so what each LEAVES BEHIND (file contents, directory
# modes, whether the lock file is gone) is the observable contract, and dumping it lets a
# divergence I did not predict show up as a diff instead of passing unexamined.
#
# Each runtime gets its OWN temp root. Sharing one would make the second run see the first's
# files and diverge on nothing but ordering.
_js_root = tempfile.mkdtemp()
_py_root = tempfile.mkdtemp()
try:
    _js = subprocess.run(["node", os.path.join(TESTS, "gates-helpers-drive.mjs"), _js_root],
                         capture_output=True, text=True, timeout=60)
    _py = subprocess.run([sys.executable, os.path.join(TESTS, "gates_helpers_drive.py"), _py_root],
                         capture_output=True, text=True, timeout=60)
    report(_js.returncode == 0, "gates_helpers: oracle driver ran", _js.stderr.strip()[-300:])
    report(_py.returncode == 0, "gates_helpers: port driver ran", _py.stderr.strip()[-300:])
    _js_rows = json.loads(_js.stdout) if _js.returncode == 0 else []
    _py_rows = json.loads(_py.stdout) if _py.returncode == 0 else []
    _first = next((f"{a} != {b}" for a, b in zip(_js_rows, _py_rows) if a != b), "")
    report(_js_rows == _py_rows and bool(_js_rows),
           "gates_helpers: port matches the oracle on every effect",
           _first[:400] or f"lengths {len(_js_rows)}/{len(_py_rows)}")

    # VACUITY CONTROLS. "The two agree" is trivially true of a comparison that cannot see
    # anything, and this suite has already shipped three harnesses that agreed about nothing
    # (a ps scan matching no line, a sorted(dict) dropping every value, a key stripped to make
    # two shapes line up). So assert that the rows the security guards produce are PRESENT and
    # say what they should -- and that the root guard's positive control still reads.
    _by_name = dict(_js_rows)
    report("outside the allowed root" in str(_by_name.get("read escaped with root")),
           "gates_helpers: the containment guard fired (vacuity control)",
           str(_by_name.get("read escaped with root"))[:200])
    report("ok" in (_by_name.get("read escaped without root") or {}),
           "gates_helpers: the SAME read succeeds without a root (discriminating control)",
           str(_by_name.get("read escaped without root"))[:200])
    report("ok" in (_by_name.get("read contained with root") or {}),
           "gates_helpers: a contained file still reads with a root (positive control)",
           str(_by_name.get("read contained with root"))[:200])
    report(_by_name.get("mode .agents-discipline") == "700"
           and _by_name.get("mode scope dir") == "700",
           "gates_helpers: 0700 reaches the INTERMEDIATE state dir, not just the leaf",
           f"{_by_name.get('mode .agents-discipline')}/{_by_name.get('mode scope dir')}")
    report(all(_by_name.get(k) == [] for k in
               ("lock dir after release", "lock dir after timeout", "lock dir after throw")),
           "gates_helpers: the lock is released on success, timeout AND throw")
finally:
    shutil.rmtree(_js_root, ignore_errors=True)
    shutil.rmtree(_py_root, ignore_errors=True)
completed.append("gates_helpers")

# --- jsapi: the JS built-ins a Python idiom gets wrong -------------------------------------
# Object key enumeration order, localeCompare collation, and Date.parse. The oracle here is the
# JS ENGINE, not a gates.mjs function, so the driver calls the built-ins raw.
_js = subprocess.run(["node", os.path.join(TESTS, "jsapi-drive.mjs")],
                     capture_output=True, text=True, timeout=60)
_py = subprocess.run([sys.executable, os.path.join(TESTS, "jsapi_drive.py")],
                     capture_output=True, text=True, timeout=60)
report(_js.returncode == 0 and _py.returncode == 0, "jsapi: both drivers ran",
       (_js.stderr + _py.stderr).strip()[-300:])
_jr = json.loads(_js.stdout) if _js.returncode == 0 else []
_pr = json.loads(_py.stdout) if _py.returncode == 0 else []

# The DECLARED exception, asserted rather than hidden: Date.parse also accepts formats ECMA-262
# leaves implementation-defined ("Jan 1 2020"), and the port returns None for them. Those rows
# are excluded from the equality check HERE -- one place, where the exception's shape can be
# checked -- instead of being dropped from the corpus, which would make the suite agree by not
# looking.
_DECLARED = "declared non-ISO"
_cmp = [(a, b) for (ka, a), (_kb, b) in zip(_jr, _pr) if _DECLARED not in ka]
_diff = next((f"{a!r} != {b!r}" for a, b in _cmp if a != b), "")
report(_jr and _pr and not _diff and len(_jr) == len(_pr),
       "jsapi: port matches the engine on every non-declared row",
       _diff[:300] or f"{len(_cmp)} compared of {len(_jr)}/{len(_pr)}")
_dec = [(a, b) for (ka, a), (_kb, b) in zip(_jr, _pr) if _DECLARED in ka]
report(bool(_dec) and all(isinstance(a, int) and b is None for a, b in _dec),
       "jsapi: the declared non-ISO exception has the shape it claims", str(_dec)[:200])

# Vacuity controls for the two shims whose whole value is being UNLIKE the obvious Python idiom.
# Without these, a shim that had quietly degraded to `sorted()` or to insertion order would pass
# the equality check the day someone made the drivers agree.
_by = dict(_jr)
report(_by.get("keys mixed") != sorted(_by.get("keys mixed", [])) and
       _by.get("keys mixed") == ["2", "10", "b", "a", "01", "-1"],
       "jsapi: JS key order really is not insertion or sorted order (vacuity control)",
       str(_by.get("keys mixed")))
report(_by.get("localeCompare pairs") != sorted(_by.get("localeCompare pairs", [])),
       "jsapi: localeCompare really differs from code-point order (vacuity control)",
       str(_by.get("localeCompare pairs")))

# A DRIFT GUARD, AND ONLY THAT -- read the next paragraph before citing it for anything more.
# It enforces ONE direction: every shape the docstring enumerates has a corpus row. It CANNOT
# detect the opposite, a corpus row named nowhere, which is the direction an unbounded corpus
# grows in -- and most rows ARE named nowhere. So this check does NOT close the corpus, and the
# commit that introduced it (aef1856) said it did. What it buys is real but small: an enumerated
# shape cannot silently lose its row. No count in this comment on purpose; it would be stale the
# next time a row landed.
_gram = re.search(r"THE GRAMMAR THIS ACCEPTS.*?\n(.*?)\n\s*Adding a shape",
                  pathlib.Path(LIB, "jsapi.py").read_text(encoding="utf-8"), re.S)
# Shape lines ONLY -- indented, starting at a quote. The captured region also holds two PROSE
# lines, and taking every quoted span in it meant one added quotation mark injected a phantom
# shape that no corpus row could ever back. Restricting to the shape-line shape removes that.
_shapes = [s for line in (_gram.group(1).splitlines() if _gram else [])
           if re.match(r'\s+"', line) for s in re.findall(r'"([^"]*)"', line)]
# The guard catches PARTIAL extraction, which the truthiness test below cannot: a format change
# that drops most lines leaves a short list that still passes `_shapes and ...`. A marker rename
# yields the empty list and is caught by the truthiness alone.
report(len(_shapes) >= 20, "jsapi: the grammar enumeration extracted whole", str(len(_shapes)))
# ensure_ascii=False, matching jsapi_drive.py:129 EXACTLY. The default escapes non-ASCII where
# JSON.stringify does not, so the reconstructed label missed every non-ASCII row -- and this
# check's own docstring block has a "trimmed first" line, so enumerating U+FEFF would have
# produced a spurious FAIL naming a row whose corpus entry exists. Third occurrence of this trap.
_unbacked = [s for s in _shapes
             if "Number(string) " + json.dumps(s, ensure_ascii=False) not in _by]
report(_shapes and not _unbacked,
       "jsapi: every docstring-enumerated grammar shape has a corpus row", str(_unbacked)[:200])
# THE OTHER DIRECTION, COMPUTED RATHER THAN NARRATED. bd82b3e deleted a hand-written "31 of 65"
# from two files as prose that would rot, and wrote a parenthetical explaining the deletion that
# was LONGER than the computation would have been -- both inputs were already in hand. The
# remedy for a fact that rots is to compute it, not to make it too vague to be wrong. This is a
# RATCHET, not an equality: the gap may shrink, never grow, so a new unnamed row has to be
# argued for in a commit rather than added silently.
_corpus = [json.loads(k[len("Number(string) "):]) for k, _v in _jr
           if k.startswith("Number(string) ")]
# NAMED means named in js_to_number's DOCSTRING -- the enumerated block plus the MEASURED list
# above it. A first version scanned the WHOLE FILE, which is far too permissive: it credited a
# row to "documentation" whenever the string merely collided with a literal in the CODE.
# Measured, 12 rows were named only outside the docstring, and the offenders show what that is
# worth -- "" "0" "nan" "inf" "NaN" "Infinity" are `float("nan")`, `float("inf")` and an
# empty-string comparison, not documentation of anything. Widening the scan is how the count
# fell 31 -> 30; the row it "named" had not been documented, only coincidentally spelled.
_jsapi_src = pathlib.Path(LIB, "jsapi.py").read_text(encoding="utf-8")
_doc = re.search(r'def js_to_number\(value\):\n\s*"""(.*?)"""', _jsapi_src, re.S)
report(bool(_doc), "jsapi: js_to_number's docstring was located", "" if _doc else "regex missed")
_named = set(re.findall(r'"([^"]*)"', _doc.group(1))) if _doc else set()
_unnamed = [s for s in _corpus if s not in _named]
# A RATCHET, and its weakness is worth stating: the bound IS the current value, so the honest
# way past it is to document the row, and the dishonest way is a one-character edit to this
# number. Nothing stops the latter -- I changed it 31 -> 29 -> 30 myself in one session. What it
# buys is that the edit must be MADE and appear in a diff, which an unnamed row otherwise never
# does. It is a speed bump with a paper trail, not a gate.
# THE BOUND ENCODES A DELETION, NOT A DOCUMENTATION GAIN -- and the check cannot tell those
# apart, which is why the detail string prints BOTH numbers. When fd88492 deleted a row the
# count went 31-of-65 to 30-of-64: the deleted row was itself unnamed, so the "improvement" was
# a subtraction. fd88492 was then reverted (the deletion was wrong), and the bound is back at
# 31. Read the pair, never the bound alone.
report(len(_unnamed) <= 31,          # control: 30 FAILs, naming the count in its detail string
       "jsapi: the count of corpus rows named nowhere in the docstring has not grown",
       "%d unnamed of %d rows" % (len(_unnamed), len(_corpus)))
completed.append("jsapi")

# --- the PORT_INCOMPLETE_EXIT sentinel really is disjoint from the oracle's exit codes -------
# gate_check.py stops at target discovery and exits 90 there, so a vector running off the end of
# the port shows as a DIVERGENCE rather than as agreement. That only works while 90 collides with
# nothing gate-check.mjs can exit with, and the justification for it was 16 line numbers written
# into a docstring -- exactly the unchecked prose that got a "31 of 65" count deleted two commits
# earlier. Same rule, so: checked here instead of narrated there.
#
# The oracle is held FIXED by the method, which is a discipline and not a mechanism. This is the
# mechanism.
_gc = pathlib.Path(ROOT, "scripts", "gate-check.mjs").read_text(encoding="utf-8")
_exit_literals = {int(m) for m in re.findall(r"process\.exit\((\d+)\)", _gc)}
_exit_computed = re.findall(r"process\.exit\((?!\d+\))[^)]*\)", _gc)
# Vacuity control: an empty set is disjoint from everything, so the sentinel check alone would
# pass on a file this regex could not read at all.
report(len(_exit_literals) >= 4, "gate-check: exit literals were actually found",
       str(sorted(_exit_literals)))
# A computed code is the real hazard -- it makes the literal set an incomplete enumeration, and
# no static read can then bound what the oracle exits with. lib/check-supervisor.mjs HAS one
# (`process.exitCode = code`), which is why this is scoped to gate-check.mjs: that file is
# reached only by spawn() at :674, as a separate process whose status gate-check reads as data.
report(not _exit_computed, "gate-check: every exit code is a literal, so the set is closed",
       str(_exit_computed)[:200])
# THE DOMAIN HAS TO MATCH THE CLAIM. "90 collides with no oracle exit code" is about the running
# PROGRAM, and `process.exit()` inside an IMPORTED module is the importer's exit too. Checking
# only gate-check.mjs would bound a smaller domain than the sentence covers -- the same
# argument-shape defect this block replaced. So the imports are read as well, and the two
# NON-imported helpers are excluded by name with the reason each is safe.
# NO _spawned TABLE HERE, DELIBERATELY. A draft asserted "check-supervisor.mjs and
# regex-worker.mjs are not also imported" -- which buys nothing: it is already entailed by the
# import closure below containing neither and exiting nowhere, and a hardcoded list of spawned
# helpers is prose stored as a Python literal that a third helper would silently outdate. The
# property is "nothing the oracle IMPORTS can exit"; that is what gets asserted. (They are
# reached by spawn() at :674 and new Worker() at :555 -- a comment holds that fine.)
#
# TO FIXPOINT, not one level. A module gate-check imports may import something that exits, and
# that exit is still gate-check's. One level would have passed today by luck -- the only
# second-level edge is dispatch.mjs -> gates.mjs, which the direct list already covers -- and a
# check that is correct by coincidence stops being correct without anyone editing it.
_imported, _todo = set(), [_gc]
while _todo:
    _txt = _todo.pop()
    for _m in re.finditer(r'from "\.(?:/lib)?/([\w-]+\.mjs)"', _txt):
        _rel = "lib/" + _m.group(1)
        if _rel in _imported:
            continue
        _imported.add(_rel)
        _p = pathlib.Path(ROOT, "scripts", _rel)
        if _p.exists():
            _todo.append(_p.read_text(encoding="utf-8"))
# Vacuity control, same reason as the exit-literal one: an empty set intersects nothing and
# exits nowhere, so BOTH assertions below would pass on a regex that matched no import at all.
report(len(_imported) >= 3, "gate-check: the import closure was actually walked",
       str(sorted(_imported)))
_imported_exits = {}
for _rel in sorted(_imported):
    _p = pathlib.Path(ROOT, "scripts", _rel)
    _t = _p.read_text(encoding="utf-8") if _p.exists() else ""
    _found = re.findall(r"process\.exit\(([^)]*)\)", _t)
    if _found:
        _imported_exits[_rel] = _found
report(not _imported_exits,
       "gate-check: no imported module exits the process on its own", str(_imported_exits)[:200])
report(90 not in _exit_literals,
       "gate_check.py: the 90 sentinel collides with no oracle exit code",
       str(sorted(_exit_literals)))
completed.append("exit_sentinel")

# --- gate_definition_digest on a LONE SURROGATE: the production function, against node -------
# The port CRASHED here where the oracle returned a digest -- UnicodeEncodeError from sha256's
# strict encode, on the APPROVAL IDENTITY path. Reachable, not theoretical: CPython
# surrogateescape-decodes sys.argv itself (measured: b"\xff" is U+DCFF to CPython and U+FFFD to
# node), and gate_check.py's --cwd carries the result into this payload.
#
# THIS EXISTS BECAUSE digest-diff.sh CANNOT COVER IT. That differential compares two hand-built
# driver reimplementations and never calls gate_definition_digest, so a mutation of gates.py
# reddens nothing there -- measured, after the row was added with a false "mutation-isolated"
# claim. The row is fine for what it proves; this is the check that reaches the shipped code.
_sur = chr(0xD800)
_js_dig = subprocess.run(
    ["node", "-e",
     'import(process.argv[1]).then(m=>process.stdout.write(String(m.gateDefinitionDigest('
     '{check:"echo \\ud800",expect:"ok",cwd:null}))))',
     os.path.join(ROOT, "scripts", "lib", "gates.mjs")],
    capture_output=True, text=True, timeout=60)
# CAUGHT, because the failure mode being checked is a RAISE, not a wrong value. Letting it
# propagate ends the run: measured against the pre-fix code, the suite exited 1 with
# "TRUNCATED -- these sections never completed: parse_gates, short_write, enoent_probe,
# dispatch" and no digest row at all. The truncation guard did its job, but a crash is not a
# catch -- the diagnosis has to name THIS function, not four unrelated sections.
try:
    _py_dig = _g.gate_definition_digest({"check": "echo " + _sur, "expect": "ok", "cwd": None})
except UnicodeEncodeError as exc:
    _py_dig = "RAISED UnicodeEncodeError: %s" % exc
report(_js_dig.returncode == 0 and len(_js_dig.stdout.strip()) == 64,
       "digest: the oracle produced a digest for a lone-surrogate gate",
       (_js_dig.stdout + _js_dig.stderr).strip()[-200:])
report(_js_dig.stdout.strip() == _py_dig,
       "digest: the port matches it instead of raising UnicodeEncodeError",
       "js=%s py=%s" % (_js_dig.stdout.strip()[:16], str(_py_dig)[:16]))
# Control that the check is not vacuous on ORDINARY non-ASCII, where ensure_ascii=False is
# already required and the substitution must NOT fire.
_js_na = subprocess.run(
    ["node", "-e",
     'import(process.argv[1]).then(m=>process.stdout.write(String(m.gateDefinitionDigest('
     '{check:"echo na\\u00efve",expect:"ok",cwd:null}))))',
     os.path.join(ROOT, "scripts", "lib", "gates.mjs")],
    capture_output=True, text=True, timeout=60)
report(_js_na.stdout.strip() == _g.gate_definition_digest(
           {"check": "echo na" + chr(0xEF) + "ve", "expect": "ok", "cwd": None}),
       "digest: ordinary non-ASCII still agrees (the escape did not over-fire)",
       _js_na.stdout.strip()[:32])
# THE SURROGATE IN THE `cwd` SLOT, which is the position the reachability argument actually
# names -- `--cwd` is what carries an argv byte into this payload, and both checks above put the
# surrogate in `check` instead. It passes for a reason (the substitution runs over the whole
# dumped text, not per field, and str(cwd) on a str is identity), but "it passes for a reason"
# is the phrasing this task keeps having to retract. One more object closes it.
_js_cwd = subprocess.run(
    ["node", "-e",
     'import(process.argv[1]).then(m=>process.stdout.write(String(m.gateDefinitionDigest('
     '{check:"echo ok",expect:"ok",cwd:"/tmp/\\udcff"}))))',
     os.path.join(ROOT, "scripts", "lib", "gates.mjs")],
    capture_output=True, text=True, timeout=60)
try:
    _py_cwd = _g.gate_definition_digest(
        {"check": "echo ok", "expect": "ok", "cwd": "/tmp/" + chr(0xDCFF)})
except UnicodeEncodeError as exc:
    _py_cwd = "RAISED UnicodeEncodeError: %s" % exc
report(len(_js_cwd.stdout.strip()) == 64 and _js_cwd.stdout.strip() == _py_cwd,
       "digest: a surrogate in the cwd slot agrees too, not just in check",
       "js=%s py=%s" % (_js_cwd.stdout.strip()[:16], str(_py_cwd)[:16]))
# The LOCK-METADATA write is the same serializer contract at a second site, and it had the same
# crash. Checked as TEXT rather than by writing a lock, because the property is the
# serialization: a resolved path carrying a surrogateescape byte must produce the oracle's bytes
# instead of raising. A failed write here is not a lost record -- gates.py's own comment says
# the release loop's json.load then raises, that arm breaks WITHOUT unlinking, and the lock is
# held until a human removes it.
_lock_payload = {"token": "t", "pid": 1, "target": "/tmp/" + chr(0xDCFF), "at": 1}
_js_lock = subprocess.run(
    ["node", "-e",
     'process.stdout.write(JSON.stringify({token:"t",pid:1,target:"/tmp/\\udcff",at:1}))'],
    capture_output=True, text=True, timeout=60)
try:
    _py_lock = _g._js_json_text(_lock_payload)
    _py_lock_bytes = len(_py_lock.encode("utf-8"))
except UnicodeEncodeError as exc:
    _py_lock, _py_lock_bytes = "RAISED: %s" % exc, -1
report(_js_lock.stdout == _py_lock and _py_lock_bytes == len(_js_lock.stdout.encode("utf-8")),
       "lock metadata: a surrogate in the resolved target serializes, byte for byte",
       "js=%r py=%r" % (_js_lock.stdout[:48], str(_py_lock)[:48]))
completed.append("digest_surrogate")

# --- parse_gates: the whole parse result, field by field -----------------------------------
# The drivers existed and NOTHING RAN THEM. 8424c59 cites "5/5 whole-object diffs identical" as
# its verification, but that corpus lived in ad-hoc temp files and is gone — an unreproducible
# claim, which is the same "documented hole is still a hole" failure this file's own header
# describes. The five ledgers are fixtures now, each aimed at a class that has already produced a
# divergence here: non-ASCII ids and titles, CRLF with duplicate attributes, a missing final
# newline, a regex-reading EXPECT, and a ledger that is all parse errors.
_PORT_FIXTURES = pathlib.Path(TESTS) / "fixtures" / "port"
_fixtures = sorted(_PORT_FIXTURES.glob("*.md"))
# Counted, not globbed-and-hoped: an empty glob would make the loop below pass by running zero
# comparisons — a green section that checked nothing.
report(len(_fixtures) == 9, "parse_gates: the fixture corpus is present", f"{len(_fixtures)} found")
# ...but the COUNT alone is not enough, and measured: five ZERO-BYTE .md files satisfy it and
# then parse identically on both sides (`errors: ["ledger contains zero live gates"]`), so all
# six rows go green while nothing is tested. That is the same defect as the empty glob with one
# more step. So assert the corpus still exhibits the CLASSES the fixtures exist for, read from
# the ORACLE's own output rather than from what I believe I wrote into the files.
_oracle_docs = {}
for _fixture in _fixtures:
    _probe = subprocess.run(["node", os.path.join(TESTS, "parse-gates-drive.mjs"), str(_fixture)],
                            capture_output=True, text=True, timeout=30)
    if _probe.returncode == 0:
        _oracle_docs[_fixture.name] = json.loads(_probe.stdout)
_all = list(_oracle_docs.values())
# The probe loop stores a doc ONLY on returncode == 0, so a fixture that crashes the oracle
# silently drops out of _all and weakens every `any(...)` predicate below -- each would then be
# asking a smaller corpus and could go green by not looking. Nothing reported that until now.
report(len(_oracle_docs) == len(_fixtures),
       "parse_gates: the oracle parsed EVERY fixture (none silently dropped)",
       f"{len(_oracle_docs)} of {len(_fixtures)}")
if len(_oracle_docs) != len(_fixtures):
    # FAIL FAST rather than continue. Every class predicate below is an `any(...)` over this
    # corpus, so a short corpus does not make them fail -- it makes them ask a smaller question
    # and print GREEN. One red line above a screen of greens is the shape a skimmer misreads,
    # and the greens are the ones they trust. There is no useful answer to be had from a corpus
    # that is missing a fixture, so stop.
    print(f"{failed} FAILED")
    sys.exit(1)
_jstrim = _oracle_docs.get("js-trim.md", {})


def _gates_with_digests(doc):
    """Pair each gate with ITS digest, POSITIONALLY.

    Not a lookup by id: gate ids are not unique in this corpus -- crlf-duplicates.md and
    owns-duplicates.md exist precisely to carry duplicates -- so a first-match-by-id join
    returns the wrong row whenever two gates share an id. Concretely, put a digest-None gate
    (non-ASCII CHECK, no EXPECT) AFTER a digest-set gate with the same id and the id join hands
    the digest-None gate the other one's digest, re-opening the hole the digest join was added
    to close. Both drivers build `digests` by mapping over `gates` in order, so index
    correspondence is exact and needs no key at all.
    """
    return list(zip(doc.get("gates", []), doc.get("digests", [])))


# The pairing assertion is here, in an EXPLICIT loop, and NOT inside the helper. It was inside,
# which made it a side effect of a call made from a generator expression under `any(...)` -- and
# `any` SHORT-CIRCUITS. Measured with the exact comprehension shape: with the matching fixture
# sorted LAST the assertion runs for 9 of 9 fixtures, with it sorted FIRST it runs for 1 of 9.
# It reported 9 today only because unicode-ids.md happens to sort last; renaming that file, or
# adding non-ASCII to an earlier one, would have silently dropped the check to a single fixture
# with no failure anywhere. zip() truncates to the shorter side, so an unpaired dump would then
# quietly shrink every predicate built on it instead of failing.
for _name, _doc in sorted(_oracle_docs.items()):
    report(len(_doc.get("gates", [])) == len(_doc.get("digests", [])),
           f"parse_gates: one digest per gate in {_name}",
           f"{len(_doc.get('gates', []))} gates, {len(_doc.get('digests', []))} digests")

# Each predicate must be satisfiable ONLY by a fixture that exercises the class in its name.
# Three of these were not, and they had the exact defect they were written to catch:
#   - "no final newline" was `finalNewline is False`, which a ZERO-BYTE file satisfies — so it
#     was the one assertion that survived the empty-corpus vacuity control, and it survived for
#     the degenerate reason the control exists to reject. Now it must also carry a gate.
#   - "a checked box" was `any(d["gates"])`, i.e. any gate at all. An all-`- [ ]` corpus passed.
#   - "a regex-reading EXPECT warning" was `any(d["warnings"])`, i.e. any warning of any kind —
#     in the very class where a fixture had already lied to me once.
_classes = {
    "a CRLF ledger": any(d["eol"] == "\r\n" and d["gates"] for d in _all),
    "a ledger with no final newline": any(d["finalNewline"] is False and d["gates"] for d in _all),
    "a non-ASCII gate id": any(not g["id"].isascii() for d in _all for g in d["gates"]),
    "an ABANDON reason": any(any(reason for _id, reason in d["abandoned"]) for d in _all),
    "a regex-reading EXPECT warning":
        any("read as a regular expression" in w for d in _all for w in d["warnings"]),
    # ACCUMULATED errors on a ledger that HAS gates, so the unconditional "ledger contains zero
    # live gates" error cannot be one of them. The previous spelling filtered that message by
    # SUBSTRING, which fails OPEN: reword the error in parse_gates and the filter stops matching,
    # the unconditional error counts again, and the predicate silently reverts to the weaker form
    # — no failure, just less strictness, which is the worst direction for an assertion whose
    # whole job is being strict. Requiring gates gets the same meaning with no coupling to text.
    "accumulated parse errors": any(len(d["errors"]) > 1 and d["gates"] for d in _all),
    # The non-ASCII TITLE, asserted separately from the id. 8424c59's fixture comment claims
    # "non-ASCII ids and titles" and only the id was ever checked -- and the title is where a
    # real divergence lived: the ensure_ascii=False serializer bug was found on
    # "тесты пройдены ✓", not on an id. Editing that heading to ASCII went unnoticed.
    # The TITLE specifically (line 0), not "any non-ASCII line": d["lines"] includes the GATE
    # line, so `gäte-ü` satisfied the previous spelling on its own and ASCII-ising the heading
    # still went unnoticed — the very thing the class was added for. My control mutated the
    # title AND the id together, so the id class firing is all it actually demonstrated.
    "a non-ASCII title": any(not d["lines"][0].isascii() for d in _all if d["lines"]),
    "a checked box": any(g.get("checked") for d in _all for g in d["gates"]),
    # Non-ASCII in the three fields the DIGEST is computed over, which is a different class from
    # the id and the title above and is not implied by either. gate_definition_digest serializes
    # with ensure_ascii=False; json.dumps defaults to True, and that mutation produced ZERO
    # divergences across all eight fixtures because every CHECK/EXPECT/CWD in the corpus was
    # pure ASCII -- the non-ASCII lived only in ids and titles, which the digest never reads.
    # So the guard was untested while two neighbouring unicode classes were green. Without this
    # predicate, ASCII-ising one fixture line silently restores that hole.
    # A value padded with U+FEFF -- stripped by `.trim()`, kept by `str.strip()`. Both halves are
    # load-bearing: the padding is IN the file, and the ORACLE's parse does not carry it. My first
    # spelling was `any("﻿" not in check ...)`, which every ASCII fixture satisfies on its own --
    # vacuous, in the same way three earlier predicates in this dict were.
    #
    # This guards the CORPUS, not the port: _oracle_docs is Node's output, so the second half
    # holds no matter what the port does. Catching a port regression is the differential's job,
    # and it does -- measured, reverting js_trim to str.strip() makes js-trim.md diverge.
    # THE WITNESSES MUST EXIST. `all()` over an empty sequence is True, so the previous spelling
    # passed when js-trim.md parsed to NOTHING -- gates [], owns [], abandoned [] -- which is
    # verbatim the regression the commit before this one repaired (an indented OWNS and a
    # misspelled ABANDON parsed to nothing and the fixture still looked fine). A predicate that
    # survives its own subject vanishing asserts nothing. The three non-empty checks below are
    # exactly the three lists that came back empty then.
    "a value padded with JS-only whitespace": (
        "﻿" in (_PORT_FIXTURES / "js-trim.md").read_text(encoding="utf-8")
        and bool(_jstrim.get("gates")) and bool(_jstrim.get("owns"))
        and bool(_jstrim.get("abandoned"))
        and all("﻿" not in str(g.get(_f) or "")
                for g in _jstrim["gates"] for _f in ("check", "expect", "title"))
    ),
    # Joined to the DIGEST, not merely to a gate. gate_definition_digest returns None unless
    # both check and expect are non-empty strings, and several fixtures carry digest-None gates
    # (errors.md, attr-association.md). Without the join, moving the non-ASCII onto one of those
    # keeps this predicate green while ensure_ascii=False goes back to being untested -- the
    # exact vacuity it was added to close, re-opened one fixture edit later.
    "non-ASCII in a digested field": any(
        isinstance(g.get(_f), str) and not g[_f].isascii() and _row["digest"] is not None
        for d in _all for g, _row in _gates_with_digests(d) for _f in ("check", "expect", "cwd")
    ),
    # The shapes the seventh review round named as structurally invisible: a fence closed by a
    # SHORTER run, a mismatched fence character, a backtick in the info string, an attribute
    # after a blank line, an unindented attribute, and duplicate OWNS entries. Every one AGREED
    # between the runtimes when measured -- these assertions keep the shapes in the corpus so
    # that stays a measured fact rather than a remembered one.
    # The OUTCOME, not the presence of a backtick run. `any("```" in line ...)` passed on any
    # fixture that merely CONTAINED a fence, so stripping the sneaky gates out of fences.md would
    # have kept it green while the coverage silently vanished — the same defect as "a checked
    # box" meaning "any gate at all". This pins what the state machine must DO: a gate-shaped
    # line inside a fence is not a gate, and a real one after it still is.
    # PRESENT in the source and ABSENT from the gates. The previous spelling asked only that no
    # gate be NAMED sneaky1/sneaky2, which every fixture without one satisfies -- absence of
    # evidence standing in for evidence of absence, the same defect as "a checked box" meaning
    # "any gate at all". Now deleting the sneaky lines fails it, renaming them fails it, and
    # letting them escape their fence fails it.
    "a fence that swallows gate-shaped lines": any(
        all(any("- [ ] " + name + ":" in line for line in d["lines"])
            for name in ("sneaky1", "sneaky2", "sneaky4"))
        and not any(g["id"] in ("sneaky1", "sneaky2", "sneaky4") for g in d["gates"])
        and any(g["id"] == "real" for g in d["gates"])
        for d in _all),
    "a duplicated OWNS entry": any(len(d["owns"]) != len(set(d["owns"])) for d in _all),
}
for _name, _present in sorted(_classes.items()):
    report(_present, f"parse_gates: the corpus still contains {_name}")
for _fixture in _fixtures:
    _js = subprocess.run(["node", os.path.join(TESTS, "parse-gates-drive.mjs"), str(_fixture)],
                         capture_output=True, text=True, timeout=30)
    _py = subprocess.run([sys.executable, os.path.join(TESTS, "parse_gates_drive.py"),
                          str(_fixture)], capture_output=True, text=True, timeout=30)
    _ok = _js.returncode == 0 and _py.returncode == 0 and _js.stdout == _py.stdout
    _detail = ""
    if not _ok:
        # The DIFF first, and stderr only when a process actually failed. With `or` the other way
        # round, any stderr at all — a Node experimental warning, a DeprecationWarning — won
        # over the line comparison even when both exited 0 and the failure was purely a stdout
        # difference, so the diff was never computed and the row reported the wrong cause.
        if _js.returncode != 0 or _py.returncode != 0:
            _detail = (_js.stderr or _py.stderr or "").strip()[-200:]
        else:
            _detail = next(
                (f"{a} | {b}" for a, b in zip(_js.stdout.splitlines(), _py.stdout.splitlines())
                 if a != b), "output length differs")
    report(_ok, f"parse_gates: {_fixture.name} — whole parse result identical", _detail)
completed.append("parse_gates")

# --- short write: gates._write_all -----------------------------------------------------------
# No oracle row is possible: Node's writeFileSync(fd, ...) loops internally, so there is nothing
# to compare against -- the port had to GROW the loop. Verified against its own control instead,
# in a subprocess because RLIMIT_FSIZE is process-wide.
# TimeoutExpired caught, not propagated: a regressed `written <= 0` guard makes _write_all spin
# forever, and MEASURED — removing that guard hangs the probe. Unwrapped, the timeout would take
# down the whole run with a traceback instead of failing one row, so the one defect the guard
# exists to prevent would also destroy the report that names it.
try:
    _sw = subprocess.run([sys.executable, os.path.join(TESTS, "short_write_probe.py")],
                         capture_output=True, text=True, timeout=30)
    _sw_stdout, _sw_rc, _sw_stderr = _sw.stdout, _sw.returncode, _sw.stderr
except subprocess.TimeoutExpired:
    _sw_stdout, _sw_rc, _sw_stderr = "", 1, "probe HUNG (an unbounded write loop?)"
# Keyed by PREFIX, not by position. The probe's three lines were reordered (the socket case
# has to run BEFORE the rlimit lowers the hard limit), and the positional lookups silently
# attached each assertion to the wrong line — three FAILs that were entirely the harness's.
_lines = {line.split(":", 1)[0]: line for line in _sw_stdout.strip().splitlines()}
report(_sw_rc == 0 and set(_lines) == {"control", "error", "success", "atomic", "noprogress"},
       "short_write: probe ran", (_sw_stderr or _sw_stdout).strip()[-200:])
# The CONTROL first, and it gates the claim: if a bare os.write did not short-write, the second
# line is consistent with the loop being unnecessary and must not be read as verification.
report("SHORT: True" in _lines.get("control", "") or "SKIPPED" in _lines.get("control", ""),
       "short_write: a bare os.write really does short-write here (control)",
       _lines.get("control", "MISSING"))
report("raised OSError" in _lines.get("error", "") or "SKIPPED" in _lines.get("error", ""),
       "short_write: _write_all surfaces the failure instead of truncating",
       _lines.get("error", "MISSING"))
# The SUCCESS path, which the first version of this probe did not have: proving the loop does not
# truncate SILENTLY is not the same as proving it does not truncate. This one runs on every
# platform, including Windows, where the two rlimit rows report SKIPPED.
# SKIPPED accepted only for an unavailable PRIMITIVE, never for TRUNCATED: the probe words
# those differently on purpose, so a platform that cannot run the case and a loop that loses
# bytes can never print the same thing. Forced all three skip paths and confirmed the wording.
# The property the whole fix exists for, and the one nothing tested until now: a write that
# FAILS mid-way must leave the pre-existing target byte-identical, unlink its temp, and never
# reach os.replace. Mutation-controlled both ways — swallowing the write error yields
# target_unchanged=False, i.e. the truncated file really does get renamed into place.
report("INTACT" in _lines.get("atomic", ""),
       "short_write: a failed write_atomic leaves the target intact and no temp behind",
       _lines.get("atomic", "MISSING"))
# The `written <= 0` guard from 0fd2909, which nothing had executed. Removing it HANGS the probe.
report("BOUNDED" in _lines.get("noprogress", ""),
       "short_write: a 0-return raises instead of spinning forever",
       _lines.get("noprogress", "MISSING"))
report("ALL BYTES" in _lines.get("success", "") or "SKIPPED" in _lines.get("success", ""),
       "short_write: _write_all delivers every byte across multiple short writes",
       _lines.get("success", "MISSING"))
completed.append("short_write")

# --- enoent_probe: the arm that distinguishes a MISSING file from a SWAPPED one -------------
# Port-only, and stub-driven because it is a race: os.open must report ENOENT while the name
# EXISTS. No oracle comparison is possible without the same stub, so what is asserted is the
# DISTINCTION the oracle draws — and it is load-bearing, not cosmetic: dispatch's readState
# returns emptyState() on ENOENT, so a port that re-raised the raw error would hand back an
# empty wave set on exactly the interleaving the oracle refuses.
_probe_src = r"""
import errno, os, sys
sys.path.insert(0, %r)
import gates
target = sys.argv[1]
real_open = os.open
def enoent_once(path, flags, *a, **k):
    os.open = real_open                      # only the FIRST open lies
    raise OSError(errno.ENOENT, "No such file or directory", path)
os.open = enoent_once
try:
    gates.read_stable_regular_file(target, label="dispatch state")
    print("NO ERROR")
except OSError as error:
    print(f"{errno.errorcode.get(error.errno, 'NONE')}|{error}")
finally:
    os.open = real_open
""" % (LIB,)
_ep_dir = tempfile.mkdtemp()
try:
    _present = os.path.join(_ep_dir, "present.json")
    with open(_present, "w", encoding="utf-8") as _h:
        _h.write('{"schema":1}\n')
    _ep = subprocess.run([sys.executable, "-c", _probe_src, _present],
                         capture_output=True, text=True, timeout=30)
    _out = _ep.stdout.strip()
    report("appeared after its open" in _out and _out.startswith("NONE|"),
           "enoent_probe: a name that reappears is an ERROR, not an absence", _out[:160])
    # DISCRIMINATING CONTROL: a genuinely missing file must still raise a real ENOENT, or the
    # arm above would just be swallowing every ENOENT and dispatch could never see an absence.
    _ep2 = subprocess.run([sys.executable, "-c", _probe_src, os.path.join(_ep_dir, "gone.json")],
                          capture_output=True, text=True, timeout=30)
    _out2 = _ep2.stdout.strip()
    report(_out2.startswith("ENOENT|"),
           "enoent_probe: a genuinely missing file still raises ENOENT (control)", _out2[:160])
finally:
    shutil.rmtree(_ep_dir, ignore_errors=True)
completed.append("enoent_probe")

# --- dispatch: the wave state machine, end to end ------------------------------------------
# The whole lifecycle plus every refusal, driven with a FIXED `now` so the two runtimes produce
# byte-identical state files. Each runtime gets its own root.
_dj, _dp = tempfile.mkdtemp(), tempfile.mkdtemp()
try:
    _o = subprocess.run(["node", os.path.join(TESTS, "dispatch-drive.mjs"), _dj],
                        capture_output=True, text=True, timeout=60)
    _p = subprocess.run([sys.executable, os.path.join(TESTS, "dispatch_drive.py"), _dp],
                        capture_output=True, text=True, timeout=60)
    report(_o.returncode == 0 and _p.returncode == 0, "dispatch: both drivers ran",
           (_o.stderr + _p.stderr).strip()[-250:])
    _or = json.loads(_o.stdout) if _o.returncode == 0 else []
    _pr = json.loads(_p.stdout) if _p.returncode == 0 else []
    _first = next((f"{k}: {a!r} != {b!r}" for (k, a), (_k, b) in zip(_or, _pr) if a != b), "")
    report(bool(_or) and _or == _pr, "dispatch: port matches the oracle on every effect",
           _first[:400] or f"lengths {len(_or)}/{len(_pr)}")

    # VACUITY CONTROLS, one per JS-semantics mechanism, each mutation-verified: reverting
    # js_length, js_entries or js_json_object reddens exactly one of these rows.
    _by = dict(_or)
    report("at most 256 characters" in str(_by.get("emoji handle")),
           "dispatch: a 400-code-unit handle is REJECTED (the UTF-16 bound, not len())",
           str(_by.get("emoji handle"))[:150])
    report("wave 1 has an invalid shape" in str(_by.get("validateState two problems, digit key first")),
           "dispatch: the DIGIT key is validated first (JS enumeration order)",
           str(_by.get("validateState two problems, digit key first"))[:150])
    _ord = str(_by.get("ord state file", ""))
    report(_ord.find('"7"') < _ord.find('"zz"') and '"7"' in _ord,
           "dispatch: the serialized file hoists the digit wave above the letter one",
           _ord[:90])
finally:
    shutil.rmtree(_dj, ignore_errors=True)
    shutil.rmtree(_dp, ignore_errors=True)
    # The `default clock` row above pins the SHAPE, which is real coverage (a wrong format fails
    # it) but NOT the bug it was added for. MEASURED: the buggy _iso_now -- seconds from a UTC
    # clock, milliseconds from a SECOND, LOCAL-time call -- produces a stamp matching the same
    # regex, so that row would have passed on it. The defect is a RACE (the two calls can
    # straddle a second), and a race is not observable from one sample.
    #
    # What IS deterministic is the call COUNT. One clock read, or the stamp is assembled from
    # two instants. This is port-only: the oracle's toISOString has nothing to compare against.
    _clock_probe = r"""
import datetime, sys
sys.path.insert(0, %r)
import dispatch
calls = []
real = datetime.datetime.now
class Counting(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        calls.append(tz)
        return real(tz)
dispatch.datetime.datetime = Counting
stamp = dispatch._iso_now()
dispatch.datetime.datetime = datetime.datetime
print(f"{len(calls)}|{stamp}|{calls}")
""" % (LIB,)
    _cp = subprocess.run([sys.executable, "-c", _clock_probe], capture_output=True, text=True,
                         timeout=30)
    _count = _cp.stdout.split("|")[0] if _cp.stdout else ""
    report(_count == "1", "dispatch: _iso_now reads the clock EXACTLY once (not shape — count)",
           (_cp.stdout.strip() or _cp.stderr.strip())[:200])
completed.append("dispatch")

# automatic_evidence_prefix's REJECT path. Every fixture reaches this function with a digest
# that is already 64 lowercase hex, so the validation never fires: mutating _DIGEST_HEX_RE to
# `.*` produced ZERO divergences across all nine fixtures, and the "threw:" branch in both
# parse-gates drivers is unreachable. The guard was therefore untested wherever it was tested
# from. These call it directly.
#
# The trailing-newline row is the one that matters most: Python's `$` matches BEFORE a final
# newline and JS's does not, so a `$`-spelled pattern would ACCEPT "…<64 hex>\n" that the oracle
# REJECTS -- and the accepted value then goes straight into the evidence prefix. That is the
# `\Z` this port spells everywhere, asserted here rather than assumed.
if True:
    sys.path.insert(0, LIB)
    import gates as _g  # noqa: E402  # type: ignore[import-not-found]
    _hex = "a" * 64
    # The first two labels say "via the falsy coalesce" because that is what they test: the guard
    # is `"" if not definition_digest else str(...)`, so None and "" BOTH reach the pattern as ""
    # and neither asks whether it is 64 lowercase hex. Six of these eight rows exercise the
    # pattern proper; calling the first two "rejects None"/"rejects empty" read as if the pattern
    # had refused them.
    for _bad, _why in (
        ("", "empty via the falsy coalesce"), (None, "None via the falsy coalesce"),
        ("A" * 64, "uppercase"), ("a" * 63, "63 chars"),
        ("a" * 65, "65 chars"), (_hex + "\n", "trailing newline (the \\Z case)"),
        ("g" * 64, "non-hex letter"), (" " + _hex, "leading space"),
    ):
        try:
            _g.automatic_evidence_prefix(_bad)
            report(False, f"automatic_evidence_prefix rejects {_why}", "it RETURNED instead")
        except ValueError:
            report(True, f"automatic_evidence_prefix rejects {_why}")
        except Exception as _exc:  # noqa: BLE001
            # NOT decoration. Measured: mutating _DIGEST_HEX_RE to `.*` makes the None row fall
            # through to `"..." + None`, which raises TypeError -- uncaught, that aborted the
            # WHOLE suite with a traceback after 3 lines instead of reporting 8 failures. So the
            # block did react to the mutation, but by dying in a way that hid the other 7 rows,
            # and I had claimed it "catches .*" without ever running it under .*.
            report(False, f"automatic_evidence_prefix rejects {_why}",
                   f"raised {type(_exc).__name__}, not ValueError: {_exc}")
    # GUARDED, for the same reason the reject rows are. Unwrapped, inverting the guard to
    # `if _DIGEST_HEX_RE.match(...)` makes all 8 reject rows report PASS -- for the wrong reason,
    # since they now raise for the opposite cause -- and then this line raises an uncaught
    # ValueError, aborting the run with a traceback UNDER eight greens. That is the identical
    # shape as the TypeError crash the previous commit repaired; the hardening stopped one line
    # short of the control that would have made the inversion legible.
    try:
        report(_g.automatic_evidence_prefix(_hex) ==
               "automatic-evidence=v1; definition-sha256=" + _hex + ";",
               "automatic_evidence_prefix accepts a real digest (positive control)")
    except Exception as _exc:  # noqa: BLE001
        report(False, "automatic_evidence_prefix accepts a real digest (positive control)",
               f"raised {type(_exc).__name__} on a valid digest: {_exc}")

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
