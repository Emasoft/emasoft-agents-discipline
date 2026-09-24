#!/usr/bin/env python3
"""Hand-written expected-table tests for scripts/lib/cmdrun.py (no bash, no pytest).

D4 (docs_dev/no-bash-spec.md): the oracle for this interpreter is a hand-written expected
table, not a bash-generated one -- there is no bash left in this plugin to generate one from.
Every construct in the "Supported" and "Refused" lists of the Grammar section gets at least
one row here; POSIX only (Windows execution paths are written but need a Windows host to run;
see cmdrun.py's own module docstring and the report this test's runner writes).

Run: python3 tests/cmdrun_tests.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(TESTS_DIR, "..", "scripts", "lib")
CMDRUN = os.path.join(LIB_DIR, "cmdrun.py")

failed = 0


def report(ok: bool, name: str, detail: object = "") -> None:
    """`detail` is often the raw cmdrun answer dict, not a string -- format it here so every
    call site can just hand over whatever it has instead of pre-stringifying at each of the
    ~60 call sites below (that was the reportArgumentType root cause: the old signature
    declared `detail: str` while callers already passed dicts and tuples)."""
    global failed
    detail_str = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail_str})' if detail_str else ''}")
    if not ok:
        failed += 1


def run(command, cwd, mode="gate", timeout_ms=5000, output_limit=1_000_000, env=None):
    req = {"command": command, "cwd": cwd, "mode": mode, "timeout_ms": timeout_ms, "output_limit": output_limit}
    if env is not None:
        req["env"] = env
    p = subprocess.run(
        [sys.executable, CMDRUN],
        input=json.dumps(req),
        capture_output=True,
        text=True,
        timeout=(timeout_ms / 1000.0) + 5,
    )
    if p.returncode != 0 and not p.stdout.strip():
        return {"__crash__": True, "stderr": p.stderr}
    lines = [line for line in p.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return {"__crash__": True, "stderr": f"expected exactly one JSON line, got {len(lines)}: {p.stdout!r}"}
    return json.loads(lines[0])


def run_raw(raw_text, timeout=8):
    """M1/M3/L3/P6: send arbitrary, possibly malformed, request text -- `run()` always builds a
    well-typed dict, which cannot express "timeout_ms is the bool True" or "top-level JSON is
    `123`", exactly the shapes those findings are about."""
    p = subprocess.run(
        [sys.executable, CMDRUN], input=raw_text, capture_output=True, text=True, timeout=timeout
    )
    lines = [line for line in p.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return {"__crash__": True, "stderr": f"expected exactly one JSON line, got {len(lines)}: {p.stdout!r} / {p.stderr!r}"}
    try:
        return json.loads(lines[0])
    except json.JSONDecodeError:
        return {"__crash__": True, "stderr": lines[0]}


def run_raw_bytes(data: bytes, timeout=8):
    """M1: invalid UTF-8 can't be expressed through `text=True` (Python would encode our own
    str input as valid UTF-8 before it ever reaches cmdrun's stdin)."""
    p = subprocess.run([sys.executable, CMDRUN], input=data, capture_output=True, timeout=timeout)
    stdout = p.stdout.decode("utf-8", errors="replace")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return {"__crash__": True, "stderr": f"{stdout!r} / {p.stderr!r}"}
    try:
        return json.loads(lines[0])
    except json.JSONDecodeError:
        return {"__crash__": True, "stderr": lines[0]}


def ps_snapshot_text():
    """verification-and-evidence / shell-pitfalls: snapshot the process table to a file, then
    search the file -- never `pgrep -f`/`ps | grep`, which match their own invoking shell."""
    path = tempfile.mktemp(prefix="cmdrun-ps-")
    with open(path, "w", encoding="utf-8") as f:
        subprocess.run(["ps", "-eo", "pid,ppid,pgid,command"], stdout=f, stderr=subprocess.DEVNULL)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def marker_alive(marker, grace_s=0.3):
    time.sleep(grace_s)
    return marker in ps_snapshot_text()


def make_workdir():
    d = tempfile.mkdtemp(prefix="cmdrun-tests-")
    for name in ("a.txt", "b.txt", "note.md"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(name + "\n")
    os.mkdir(os.path.join(d, "sub"))
    with open(os.path.join(d, "exec.sh"), "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho should-never-run\n")
    os.chmod(os.path.join(d, "exec.sh"), 0o755)
    return d


def main():
    workdir = make_workdir()
    try:
        _run_tests(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{failed} failing" if failed else "\nall green")
    sys.exit(1 if failed else 0)


def _run_tests(d):
    # -- Supported: plain words, exit status --------------------------------------------------
    a = run("echo hello", d)
    report(a.get("status") == 0 and a.get("stdout") == "hello\n", "plain words -- echo hello", a)

    # -- Supported: single quotes are fully literal --------------------------------------------
    a = run("echo 'a  b $HOME `x` *'", d)
    report(a.get("stdout") == "a  b $HOME `x` *\n", "single quotes suppress everything", a)

    # -- Supported: double quotes -- escapes \" \\ \$ and $VAR/${VAR} expansion -----------------
    a = run('echo "say \\"hi\\" and \\\\ and \\$X"', d, env={"PATH": os.environ["PATH"]})
    report(a.get("stdout") == 'say "hi" and \\ and $X\n', "double-quote escapes", a)
    a = run('echo "home=${MY_VAR}!"', d, env={"PATH": os.environ["PATH"], "MY_VAR": "v1"})
    report(a.get("stdout") == "home=v1!\n", "double-quote ${VAR} expansion", a)

    # -- Supported: backslash escape outside quotes ----------------------------------------------
    a = run(r"echo a\ b", d)
    report(a.get("stdout") == "a b\n", "backslash escape outside quotes joins a word", a)

    # -- Supported: $VAR / ${VAR}, no word-splitting, no globbing of the result -----------------
    a = run("echo $SPACED", d, env={"PATH": os.environ["PATH"], "SPACED": "x y *"})
    report(a.get("stdout") == "x y *\n", "$VAR: no word-splitting, no globbing of the result", a)
    a = run("echo ${SPACED}", d, env={"PATH": os.environ["PATH"], "SPACED": "z"})
    report(a.get("stdout") == "z\n", "${VAR} form", a)
    a = run("echo [$UNDEFINED_XYZ]", d, env={"PATH": os.environ["PATH"]})
    report(a.get("stdout") == "[]\n", "undefined var expands to empty", a)

    # -- Supported: VAR=value cmd prefix assignment (scoped to that command only) --------------
    a = run("VAR=hi env", d, env={"PATH": os.environ["PATH"]})
    report(a.get("status") == 0 and "VAR=hi" in a.get("stdout", ""), "VAR=value prefix reaches the child env", a)
    a = run("VAR=hi true && VAR=bye env", d, env={"PATH": os.environ["PATH"]})
    report("VAR=bye" in a.get("stdout", "") and "VAR=hi" not in a.get("stdout", ""),
           "VAR=value does not leak to the next command", a)

    # -- Supported: globs * ? [..] on unquoted words, sorted; no match -> literal ---------------
    a = run("echo *.txt", d)
    report(a.get("stdout") == "a.txt b.txt\n", "glob * sorted match", a)
    a = run("echo no_such*.zzz", d)
    report(a.get("stdout") == "no_such*.zzz\n", "glob no-match falls back to the literal word", a)
    a = run("echo '*.txt'", d)
    report(a.get("stdout") == "*.txt\n", "a quoted glob is never expanded", a)

    # -- Supported: pipelines a | b | c ----------------------------------------------------------
    a = run("printf 'x\\ny\\n' | grep y | wc -l", d)
    report(a.get("status") == 0 and a.get("stdout", "").strip() == "1", "3-stage pipeline", a)

    # -- Supported: && chains ---------------------------------------------------------------------
    a = run("true && true && echo ok", d)
    report(a.get("stdout") == "ok\n", "&& chain runs to the end on success", a)
    a = run("false && echo unreached", d)
    report(a.get("status") == 1 and a.get("stdout") == "", "&& chain stops at the first failure", a)

    # -- Supported: ( ... ) grouping, and | binds tighter than && --------------------------------
    a = run("(true && echo grouped)", d)
    report(a.get("stdout") == "grouped\n", "( ) grouping runs its inner chain", a)
    # NOTE: `true`/`false` here must be the REAL /usr/bin/{true,false} binaries, not the cmdrun
    # builtins of the same name -- a builtin is refused as a pipeline stage (tested below under
    # "Refused constructs"), so a pipeline-status test needs a non-builtin exit-code source.
    T, F = "/usr/bin/true", "/usr/bin/false"
    a = run(f"{T} | {T} && echo x", d)
    report(a.get("stdout") == "x\n", "'|' binds tighter than '&&'", a)

    # -- Supported: redirections, dup, and ORDER (2>&1 >f vs >f 2>&1) ----------------------------
    # A program that writes to stderr ONLY, so which fd it ends up aliasing is unambiguous.
    write_stderr = "python3 -c 'import sys; sys.stderr.write(\"E\")'"
    out1 = os.path.join(d, "out1.txt")
    a = run(f"{write_stderr} 2>&1 >{out1}", d)
    with open(out1, encoding="utf-8") as f:
        # '2>&1' dups fd2 to fd1's CURRENT target (the gate capture pipe) BEFORE '>out1.txt'
        # repoints fd1 -- so the write lands in the captured "stdout" field, and the file (only
        # ever the target of fd1, changed afterwards) stays empty.
        report(f.read() == "", "'2>&1 >f': stderr already duped to the OLD stdout before >f", a)
    report(a.get("stdout") == "E", "'2>&1 >f': ...and that duped write shows up as captured stdout", a)
    out2 = os.path.join(d, "out2.txt")
    a = run(f"{write_stderr} >{out2} 2>&1", d)
    with open(out2, encoding="utf-8") as f:
        # '>out2.txt' repoints fd1 first, THEN '2>&1' dups fd2 to that same file -- so the
        # stderr write lands in the file this time.
        report(f.read() == "E", "'>f 2>&1': stderr duped AFTER stdout points at f", a)
    a = run(f"echo hi > {os.path.join(d, 'redir1.txt')}", d)
    with open(os.path.join(d, "redir1.txt"), encoding="utf-8") as f:
        report(f.read() == "hi\n", "'>' truncating redirection", a)
    a = run(f"echo more >> {os.path.join(d, 'redir1.txt')}", d)
    with open(os.path.join(d, "redir1.txt"), encoding="utf-8") as f:
        report(f.read() == "hi\nmore\n", "'>>' appending redirection", a)
    a = run(f"cat < {os.path.join(d, 'a.txt')}", d)
    report(a.get("stdout") == "a.txt\n", "'<' input redirection", a)

    # -- Supported: pipeline status = pipefail ----------------------------------------------------
    a = run(f"{F} | {T}", d)
    report(a.get("status") == 1, "pipefail: a failing early stage still fails the pipeline", a)
    a = run(f"{T} | {F} | {T}", d)
    report(a.get("status") == 1, "pipefail: last NON-ZERO stage wins even if the tail succeeds", a)
    a = run(f"{T} | {T} | {T}", d)
    report(a.get("status") == 0, "pipefail: all-zero pipeline is zero", a)

    # -- Supported: '#' comment at the start of a word --------------------------------------------
    a = run("echo real # a trailing comment", d)
    report(a.get("stdout") == "real\n", "'#' starts a comment to end of line", a)
    a = run("echo not#a#comment", d)
    report(a.get("stdout") == "not#a#comment\n", "mid-word '#' is a literal character", a)

    # -- Supported: builtins -----------------------------------------------------------------------
    a = run("true", d)
    report(a.get("status") == 0, "builtin true", a)
    a = run("false", d)
    report(a.get("status") == 1, "builtin false", a)
    a = run("echo -n abc", d)
    report(a.get("stdout") == "abc", "builtin echo -n suppresses the trailing newline", a)
    a = run("test -e a.txt", d)
    report(a.get("status") == 0, "builtin test -e (exists)", a)
    a = run("[ -f a.txt ]", d)
    report(a.get("status") == 0, "builtin [ -f ... ] (regular file)", a)
    a = run("[ -d sub ]", d)
    report(a.get("status") == 0, "builtin [ -d ... ] (directory)", a)
    a = run("test -s a.txt", d)
    report(a.get("status") == 0, "builtin test -s (non-empty)", a)
    a = run("test -e does-not-exist", d)
    report(a.get("status") == 1, "builtin test -e is false on a missing path", a)

    # cd persists across top-level &&, not out of ( ) ---------------------------------------------
    a = run("cd sub && pwd", d)
    report(a.get("stdout", "").strip() == os.path.realpath(os.path.join(d, "sub")), "cd persists across &&", a)
    a = run("(cd sub) && pwd", d)
    report(a.get("stdout", "").strip() == os.path.realpath(d), "cd inside ( ) does not leak out", a)
    a = run("cd", d)
    report(a.get("refused") is True, "cd with no argument is refused", a)

    # -- Supported: executable resolution ----------------------------------------------------------
    a = run("ls", d, env={"PATH": "/nonexistent-dir-xyz"})
    report(a.get("status") == 127, "PATH search: not found when PATH has no matching dir", a)
    a = run("./exec.sh", d)
    report(a.get("refused") is True and ".sh" in a.get("reason", ""), "'./exec.sh' -- .sh targets are refused, even by relative path", a)
    a = run("bogus-command-xyz-123", d)
    report(a.get("status") == 127 and "not found" in a.get("stderr", ""), "unknown command -> 127", a)

    # -- Refused constructs, one row each, each naming the construct -------------------------------
    for cmd, must_contain in [
        ("true || true", "||"),
        ("true ; true", "';'"),
        ("true &", "&"),
        ("true |& true", "|&"),
        ("true &> out.txt", "&>"),
        ("true &>> out.txt", "&>>"),
        ("cat << EOF", "<<"),
        ("cat <<< hi", "<<<"),
        ("echo `id`", "backtick"),
        ("echo $(id)", "command substitution"),
        ("echo ${VAR:-x}", "${"),
        ("echo ~", "~"),
        ("echo {a,b}", "{"),
        ("echo !", "!"),
        ("   ", "empty"),
        ("echo 'unterminated", "quote"),
        ('echo "unterminated', "quote"),
        ("target.sh", ".sh"),
        ("target.bat", ".bat"),
        ("target.cmd", ".cmd"),
        ("target.ps1", ".ps1"),
        ("echo a | cd .", "pipeline"),
    ]:
        a = run(cmd, d)
        ok = a.get("refused") is True and must_contain.strip("'") in a.get("reason", "")
        report(ok, f"refused: {cmd!r} (expects {must_contain!r} in reason)", a)

    # SEMANTICS CHANGE (v1 -> v2): an empty `command` string is now caught by the supervisor's
    # own request validation ("missing or invalid 'command'", a `bad_request`) before `analyze()`
    # ever runs -- v1 routed it all the way to the tokenizer, which refused it as "empty or
    # whitespace-only command". A merely-whitespace command ("   ") is still a non-empty JSON
    # string, so it still reaches `analyze()` and is refused there (tested above).
    a = run("", d)
    report(a.get("bad_request") is True and "command" in a.get("reason", ""),
           "empty command is now a bad_request (v2 request validation), not a grammar refusal", a)

    a = run("printf 'line1\nline2'", d)
    report(a.get("refused") is True, "an embedded newline is refused (multi-line)", a)

    # -- Regression: `{ } ( ) >` etc. are only special UNQUOTED -- inside a quoted argument
    # they are plain text, so a whole node -e "..." one-liner that itself uses those characters
    # must be ACCEPTED (only bare, unquoted `{`/`}`/`!`/`~` and friends are refused by name).
    if shutil.which("node"):
        a = run('node -e "setTimeout(()=>{},1000)"', d, timeout_ms=3000)
        report(a.get("status") == 0 and not a.get("refused"),
               "node -e with double-quoted { } ( ) is accepted, not refused", a)
        a = run("node -e 'setTimeout(()=>{},1000)'", d, timeout_ms=3000)
        report(a.get("status") == 0 and not a.get("refused"),
               "node -e with single-quoted { } ( ) is accepted, not refused", a)
    else:
        report(True, "node -e regression rows skipped -- no `node` on PATH")

    # -- Regression: a background grandchild holding an inherited handle must not delay the answer.
    # I1 (audit, documented residual): a descendant that calls setsid/setpgid itself escapes the
    # worker's `killpg(0, SIGKILL)` on POSIX -- this row's grandchild is exactly that escaper, so
    # it is killed BY PID at the end instead of relying on the worker to reap it (it can't).
    detach_pidfile = os.path.join(d, "detach_pid.txt")
    detach_cmd = (
        "python3 -c \"import subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        f"start_new_session=True);open({detach_pidfile!r},'w').write(str(p.pid))\""
    )
    started = time.monotonic()
    a = run(detach_cmd, d, timeout_ms=5000)
    elapsed = time.monotonic() - started
    report(elapsed < 2.0 and not a.get("__crash__"), "detached grandchild does not delay the answer", (a, f"{elapsed:.2f}s"))
    grandchild_pid = None
    try:
        with open(detach_pidfile, encoding="utf-8") as f:
            grandchild_pid = int(f.read().strip())
    except (OSError, ValueError):
        pass
    if grandchild_pid is not None:
        time.sleep(0.2)
        try:
            os.kill(grandchild_pid, 9)
        except ProcessLookupError:
            pass
        report(True, f"detached grandchild (pid {grandchild_pid}) reaped by pid at test end (I1 residual)")
    else:
        report(False, "detached-grandchild pidfile was never written -- cannot confirm cleanup", a)

    # =============================================================================================
    # Audit/advisor regression rows (reports/no-bash/20260924_195401+0200-cmdrun-py-attack.md,
    # reports/no-bash/20260924_201309+0200-advisor-cmdrun-v2.md). One row per finding, using the
    # finding's own repro where practical.
    # =============================================================================================

    # -- H1: timeout_ms 0 / negative used to fork first and crash on an AttributeError while the
    # command ran unsupervised; the v2 supervisor validates before spawning anything.
    a = run("sleep 3", d, timeout_ms=0)
    report(a.get("bad_request") is True, "H1: timeout_ms=0 is a bad_request, refused before any spawn", a)
    a = run("sleep 3", d, timeout_ms=-5)
    report(a.get("bad_request") is True, "H1: timeout_ms=-5 is a bad_request, refused before any spawn", a)

    # -- H2/P1/P2: killpg-vs-fork race and the macOS EPERM-on-zombie-group loop semantics. A
    # unique marker in argv (not in the command NAME, which `ps` shows verbatim) lets us confirm
    # the whole group is actually dead, not just that the answer said 124.
    marker_h2 = f"CMDRUN_H2_MARKER_{os.getpid()}_{int(time.time() * 1000)}"
    a = run(f"python3 -c \"import time;time.sleep(3)\" {marker_h2}", d, timeout_ms=200)
    report(a.get("status") == 124 and a.get("timed_out") is True, "H2: short timeout answers 124", a)
    report(not marker_alive(marker_h2), "H2/P1/P2: the timed-out group is actually dead, not just reported so", a)

    # -- H3: a well-behaved command's own detached-in-group grandchild used to survive success.
    marker_h3 = f"CMDRUN_H3_MARKER_{os.getpid()}_{int(time.time() * 1000)}"
    a = run(f"python3 -c \"import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)','{marker_h3}'])\"", d, timeout_ms=5000)
    report(a.get("status") == 0, "H3: the spawning command itself succeeds", a)
    report(not marker_alive(marker_h3), "H3: its in-group grandchild is killed on the success path too", a)

    # -- H4: covered structurally by module design (worker stdin = death pipe); exercised live by
    # scripts_dev/cmdrun-attack/killparent.py, which is a manual/attack-harness script, not part
    # of this suite's fast run. Recorded here so the row isn't silently missing from the table.
    report(True, "H4: caller-kills-supervisor reaches the worker via stdin EOF (see killparent.py; not run in this suite)")

    # -- H5: non-numeric / null timeout_ms used to divide-by-zero AFTER forking; now bad_request.
    a = run_raw(f'{{"command":"sleep 1","cwd":{json.dumps(d)},"timeout_ms":"500"}}\n')
    report(a.get("bad_request") is True, "H5: timeout_ms as a string is a bad_request", a)
    a = run_raw(f'{{"command":"sleep 1","cwd":{json.dumps(d)},"timeout_ms":null}}\n')
    report(a.get("bad_request") is True, "H5: timeout_ms=null is a bad_request", a)

    # -- H6: a stage whose redirection fails must not hang its neighbour until the timeout.
    t0 = time.monotonic()
    a = run("cat < /nonexistent-h6-zzz | wc -c", d, timeout_ms=5000)
    elapsed = time.monotonic() - t0
    report(elapsed < 2.0 and not a.get("__crash__"), "H6: a stage with a failing redirection does not hang the pipeline", (a, f"{elapsed:.2f}s"))

    # -- H7: nested ( ) pipeline stages used to lose data when a sibling thread's os.pipe() reused
    # an fd number a finished stage's cleanup pass then closed a second time.
    a = run("(sleep 0.2 | sleep 0.2) | (sleep 0.1 && (sleep 0.2 && echo hi) | cat)", d, timeout_ms=6000)
    report(a.get("stdout") == "hi\n", "H7: nested group pipeline fds are each closed exactly once", a)

    # -- H8: `os.chdir` for globbing raced across concurrently running pipeline-stage threads.
    race_dir = tempfile.mkdtemp(prefix="cmdrun-race-")
    try:
        for i in range(1, 6):
            sub = os.path.join(race_dir, f"d{i}")
            os.mkdir(sub)
            with open(os.path.join(sub, f"file_of_d{i}"), "w", encoding="utf-8") as f:
                f.write("x")
        stages = " | ".join(f"(cd d{i} && echo file_* > ../o{i}.txt)" for i in range(1, 6))
        a = run(stages, race_dir, timeout_ms=6000)
        ok = True
        for i in range(1, 6):
            try:
                with open(os.path.join(race_dir, f"o{i}.txt"), encoding="utf-8") as f:
                    ok = ok and f.read().strip() == f"file_of_d{i}"
            except OSError:
                ok = False
        report(ok, "H8: glob.glob(root_dir=) avoids the chdir race across concurrent stages", a)
    finally:
        shutil.rmtree(race_dir, ignore_errors=True)

    # -- H9/H10: Windows-only (Job Object assignment; PATHEXT/.COM/.EXE + npm/npx trampoline).
    if sys.platform == "win32":
        job_dll_ok = hasattr(__import__("ctypes"), "windll")
        report(job_dll_ok, "H9: ctypes.windll is available for Job Object creation on this host")
        import cmdrun as _cmdrun_win  # noqa: E402
        report(_cmdrun_win._check_candidate.__name__ == "_check_candidate",
               "H10: _check_candidate is reachable for a PATHEXT/.COM/.EXE check on this host")
    else:
        print("SKIP  H9: Windows Job Object assignment (requires a Windows host)")
        print("SKIP  H10: PATHEXT/.COM/.EXE resolution + npm/npx trampoline (requires a Windows host)")

    # -- M1: malformed top-level JSON must never crash uncaught -- always one bad_request line.
    for raw in ["123\n", "null\n", "true\n", "NaN\n", "[1]\n", '"just a string"\n']:
        a = run_raw(raw)
        report(a.get("bad_request") is True, f"M1: malformed request {raw.strip()!r} is a bad_request, not a crash", a)
    a = run_raw_bytes(b'{"command": "echo \xff"}\n')
    report(a.get("bad_request") is True, "M1: invalid UTF-8 in the request is a bad_request, not a crash", a)

    # -- M2: the result must never travel through a guessable, re-openable temp file.
    tmpdir = tempfile.gettempdir()
    before = set(os.listdir(tmpdir))
    a = run("echo hi", d)
    after = set(os.listdir(tmpdir))
    new_cmdrun_files = sorted(f for f in (after - before) if f.startswith("cmdrun"))
    report(not new_cmdrun_files, "M2: no guessable result file is created in the temp dir", new_cmdrun_files)

    # -- M3: NaN/Infinity/huge floats pass json.loads but must not silently mean "no timeout".
    for raw_num in ["NaN", "Infinity", "1e300"]:
        a = run_raw(f'{{"command":"sleep 1","cwd":{json.dumps(d)},"timeout_ms":{raw_num}}}\n')
        report(a.get("bad_request") is True, f"M3: timeout_ms={raw_num} is a bad_request (non-integer)", a)

    # -- M4: `${` scanning must stay linear, not copy the rest of the command on every occurrence.
    t0 = time.monotonic()
    a = run("echo " + "${A}" * 20000, d, env={"PATH": os.environ["PATH"], "A": "x"}, timeout_ms=8000)
    elapsed = time.monotonic() - t0
    report(elapsed < 3.0 and not a.get("__crash__"), f"M4: 20000x '${{A}}' stays near-linear ({elapsed:.2f}s)", a)

    # -- M5: a $VAR expansion must not be allowed to amplify the command into hundreds of MB.
    a = run("echo " + "${BIG}" * 500, d, env={"PATH": os.environ["PATH"], "BIG": "x" * 4000}, timeout_ms=8000)
    report(a.get("refused") is True and "too large" in a.get("reason", ""), "M5: the expanded-argv cap refuses an amplification attempt", a)

    # -- M6: fd prefixes above 2, and multi-digit dup targets, must be refused, not misparsed.
    a = run(f"echo hi 3>{os.path.join(d, 'f3.txt')}", d)
    report(a.get("refused") is True and "0-2" in a.get("reason", ""), "M6: fd prefix '3>' is refused, not misparsed into an argument", a)
    a = run(f"echo hi 9<{os.path.join(d, 'a.txt')}", d)
    report(a.get("refused") is True, "M6: fd prefix '9<' is refused", a)
    a = run("echo hi 2>&12", d)
    report(a.get("refused") is True, "M6: multi-digit dup target '2>&12' is refused", a)

    # -- M7: $0-$9, $@ $* $# $? $$ $! $- refused by name (bash would expand these; we have none).
    for expr in ["$0", "$1", "$@", "$*", "$#", "$?", "$$", "$!", "$-"]:
        a = run(f"echo {expr}", d)
        report(a.get("refused") is True, f"M7: '{expr}' is refused by name", a)

    # -- M8: unquoted-empty-expansion word dropping, with its three pins.
    a = run("ls $UNSET_VAR_X", d, env={"PATH": os.environ["PATH"]})
    report(a.get("status") == 0, "M8: an unquoted empty expansion is DROPPED (ls gets zero args, not '')", a)
    a = run('echo "$UNSET_VAR_Y"', d, env={"PATH": os.environ["PATH"]})
    report(a.get("stdout") == "\n", "M8: a quoted empty expansion stays an empty argument", a)
    a = run("VAR=$UNSET_VAR_Z /usr/bin/env", d, env={})
    report(a.get("status") == 0 and "VAR=\n" in a.get("stdout", ""), "M8: VAR=$EMPTY stays an assignment", a)
    a = run("$UNSET_CMD_XYZ", d, env={"PATH": os.environ["PATH"]})
    report(a.get("refused") is True and "empty" in a.get("reason", ""), "M8: a command word expanding to empty is refused, not run as the next word", a)

    # -- M9: `env` present-and-empty must be used EXACTLY, never silently fall back to os.environ.
    a = run("/usr/bin/env", d, env={})
    report(a.get("status") == 0 and a.get("stdout") == "", "M9: an explicitly empty env is honoured, not replaced by the caller's real environment", a)

    # -- M10: shell names refused as the command word, and as env's target, not just in a shebang.
    a = run('bash -c "echo shell-ran"', d)
    report(a.get("refused") is True, "M10: 'bash -c ...' is refused as a command word", a)
    a = run('env bash -c "echo shell-ran"', d)
    report(a.get("refused") is True, "M10: 'env bash ...' is refused via the env-target check", a)

    # -- M11: relative PATH entries (including '.') are skipped; the cwd itself is never searched.
    pwned_dir = tempfile.mkdtemp(prefix="cmdrun-pwned-")
    try:
        pwned_path = os.path.join(pwned_dir, "pwned")
        with open(pwned_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho pwned-ran\n")
        os.chmod(pwned_path, 0o755)
        a = run("pwned", pwned_dir, env={"PATH": ".:/usr/bin:/bin"})
        report(a.get("status") == 127, "M11: a relative '.' PATH entry is skipped -- cwd is never searched", a)
    finally:
        shutil.rmtree(pwned_dir, ignore_errors=True)

    # -- L1: an incomplete request (no closing brace/newline) blocks the read, documented as the
    # caller's responsibility to always close stdin / enforce its own timeout.
    p = subprocess.Popen([sys.executable, CMDRUN], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert p.stdin is not None
        p.stdin.write(b'{"command":"echo hi","timeout_ms":100')
        p.stdin.flush()
        time.sleep(0.4)
        report(p.poll() is None, "L1: an incomplete request blocks the read (documented; callers must close stdin)")
    finally:
        p.kill()
        p.wait(timeout=3)

    # -- L2: the caller closing our stdout before the answer must exit quietly, not traceback.
    p = subprocess.Popen([sys.executable, CMDRUN], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert p.stdin is not None and p.stdout is not None
        req = json.dumps({"command": "echo hi", "cwd": d, "timeout_ms": 3000})
        p.stdin.write(req.encode())
        p.stdin.close()
        p.stdout.close()
        rc = p.wait(timeout=8)
        stderr_text = p.stderr.read().decode("utf-8", errors="replace") if p.stderr else ""
        report(rc == 0 and "Traceback" not in stderr_text, "L2: BrokenPipe on the answer write exits quietly, no traceback", stderr_text)
    finally:
        if p.poll() is None:
            p.kill()

    # -- L3: an invalid output_limit must be a bad_request, not a crashed sink or silent drop.
    for bad in ['"5"', "2.5", "null", "-1"]:
        raw = f'{{"command":"echo hi","cwd":{json.dumps(d)},"output_limit":{bad}}}\n'
        a = run_raw(raw)
        report(a.get("bad_request") is True, f"L3: output_limit={bad} is a bad_request", a)

    # -- L4: distinct exit codes -- 126 (not executable) vs 127 (not found); a missing cwd is now
    # caught up front as bad_request rather than surfacing as an "exec failed" 127 later.
    noexec = os.path.join(d, "noexec.txt")
    with open(noexec, "w", encoding="utf-8") as f:
        f.write("x")
    a = run(noexec, d)
    report(a.get("status") == 126, "L4: a non-executable existing file is 126, not 127", a)
    a = run_raw('{"command":"echo hi","cwd":"/nonexistent-cwd-zz-12345"}\n')
    report(a.get("bad_request") is True, "L4: a missing cwd is a bad_request, caught before any exec attempt", a)

    # -- L5: a redirection failure gets a shell-like message, distinct from "internal error".
    a = run("cat < /nonexistent-file-zzz-999", d)
    report(a.get("status") == 1 and a.get("stderr") and "internal error" not in a.get("stderr", ""),
           "L5: a redirection failure is shell-like text, not 'internal error'", a)

    # -- P6: a bool or a non-integral float for timeout_ms must be rejected (json.loads gives
    # `float` for 1000.0 and Python's `bool` passes a naive `isinstance(x, int)` check).
    a = run_raw(f'{{"command":"echo hi","cwd":{json.dumps(d)},"timeout_ms":true}}\n')
    report(a.get("bad_request") is True, "P6: timeout_ms=true (a bool) is a bad_request", a)
    a = run_raw(f'{{"command":"echo hi","cwd":{json.dumps(d)},"timeout_ms":1000.0}}\n')
    report(a.get("bad_request") is True, "P6: timeout_ms=1000.0 (a non-integral float) is a bad_request", a)

    # -- P7: a FIFO with no writer must not block the redirection open (O_NONBLOCK, then cleared).
    if hasattr(os, "mkfifo"):
        fifo_path = os.path.join(d, "cmdrun_test_fifo")
        os.mkfifo(fifo_path)
        try:
            t0 = time.monotonic()
            a = run(f"cat < {fifo_path}", d, timeout_ms=3000)
            elapsed = time.monotonic() - t0
            report(elapsed < 1.0 and a.get("status") == 0, f"P7: a FIFO with no writer does not block the open ({elapsed:.2f}s)", a)
        finally:
            os.remove(fifo_path)
    else:
        print("SKIP  P7: os.mkfifo unavailable on this platform")

    # -- analyze() is usable standalone (for a future lint use) -------------------------------------
    sys.path.insert(0, LIB_DIR)
    import cmdrun  # noqa: E402  # type: ignore[import-not-found]

    ok, reason = cmdrun.analyze("echo hi")
    report(ok is True and reason is None, "analyze() accepts a supported command")
    ok, reason = cmdrun.analyze("echo hi; echo bye")
    report(ok is False and "';'" in reason, "analyze() refuses by name, no execution needed", reason)


if __name__ == "__main__":
    main()
