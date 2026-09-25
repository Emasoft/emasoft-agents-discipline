#!/usr/bin/env python3
"""Hand-written expected-table tests for scripts/lib/cmdrun.py (no bash, no pytest).

D4 (docs_dev/no-bash-spec.md): the oracle for this interpreter is a hand-written expected
table, not a bash-generated one -- there is no bash left in this plugin to generate one from.
Every construct in the "Supported" and "Refused" lists of the Grammar section gets at least
one row here. The suite is import-safe and runs on Windows too: the rows that need
POSIX-only primitives (os.killpg to the supervisor's group, `ps` snapshots, `resource`
rlimits, absolute /usr/bin paths) gate on WIN/HAVE_RESOURCE with a printed SKIP; the
majority (parse/refuse/validate/redirect/answer-shape) is cross-platform by construction.

Run: python3 tests/cmdrun_tests.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(TESTS_DIR, "..", "scripts", "lib")
CMDRUN = os.path.join(LIB_DIR, "cmdrun.py")

failed = 0


def skip(name: str) -> None:
    """A printed SKIP that is NOT counted as a PASS -- for a row that executes nothing (no
    tool on PATH, no host to exercise a platform-only path). A bare `report(True, ...)` for
    "nothing ran" is a fake pass: it inflates the green count with zero coverage behind it."""
    print(f"SKIP  {name}")


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


# POSIX-only stdlib: the F2 EMFILE rows drive resource.setrlimit via preexec_fn. Absent on
# Windows -- those rows skip, everything else in the suite runs on any platform.
try:
    import resource  # noqa: F401  (used inside _run_tests F2 rows below)
    HAVE_RESOURCE = True
except ImportError:
    HAVE_RESOURCE = False
WIN = sys.platform == "win32"

# PY is the interpreter NAME the interpreter-under-test sees on PATH for its STAGES: `python3` on
# POSIX, `python` on Windows (setup-python exposes no `python3` shim there -- test-matrix.yml comment).
# Tests must not hardcode it: on Windows every `python3 ...` stage command would exit 127.
PY = "python" if sys.platform == "win32" else "python3"


def ps_snapshot_text():
    """verification-and-evidence / shell-pitfalls: snapshot the process table to a file, then
    search the file -- never `pgrep -f`/`ps | grep`, which match their own invoking shell.
    Windows has no `ps`; returning empty makes the leak-confirmation helpers honestly report
    "nothing found", and every row that DEPENDS on seeing a live process gates on `WIN`."""
    if WIN:
        return ""
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


def wait_until(pred, timeout_s, interval=0.05):
    """Polls `pred()` instead of one fixed `sleep` -- a single grace-period snapshot either
    fires too early (flaky fail on a slow CI box) or wastes the difference on a fast one."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def marker_pids(marker):
    """Returns the pids of every `ps` row whose command contains `marker` -- used to kill a
    survivor by pid before failing a test, per RULE 0 (never leave a stray attack-test process
    running after this suite ends)."""
    pids = []
    for line in ps_snapshot_text().splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and marker in parts[3] and "cmdrun_tests" not in parts[3]:
            try:
                pids.append(int(parts[0]))
            except ValueError:
                pass
    return pids


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


def _h4_signal_row(sig_name, d):
    """H4, real regression (ported from scripts_dev/cmdrun-attack/killparent.py): spawn the
    supervisor in its OWN session running a `sleep <marker>` command, wait until the worker has
    actually started it (marker visible in `ps`), then send `sig_name` to the SUPERVISOR's own
    process group -- never the worker's, which is a separate session by design. Asserts the
    whole group is confirmed dead afterwards, killing any survivor by pid before failing."""
    sig = getattr(signal, sig_name)
    marker = f"{time.time():.6f}"
    p = subprocess.Popen(
        [sys.executable, CMDRUN],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert p.stdin is not None
        req = json.dumps({"command": f"sleep {marker}", "cwd": d, "timeout_ms": 60000})
        p.stdin.write(req.encode() + b"\n")
        p.stdin.close()  # main() blocks on a full-EOF stdin read -- an unclosed pipe never returns.

        started = wait_until(lambda: marker in ps_snapshot_text(), timeout_s=3.0)
        if not started:
            report(False, f"H4 {sig_name}: worker never started 'sleep {marker}'")
            return

        os.killpg(p.pid, sig)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        dead = wait_until(lambda: marker not in ps_snapshot_text(), timeout_s=5.0, interval=0.1)
        if not dead:
            survivors = marker_pids(marker)
            for pid in survivors:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            report(False, f"H4 {sig_name}: the process group survived the caller's signal to the supervisor", survivors)
            return

        if sig_name == "SIGKILL":
            # SIGKILL is uncatchable -- the supervisor dies outright, with no answer. The group
            # reaching this point already dead IS the assertion: it can only have died via the
            # worker's own death-watch thread noticing stdin EOF (its supervisor's death), not
            # via any code path in the supervisor itself.
            report(True, f"H4 {sig_name}: caller-kills-supervisor reaches the worker via stdin EOF -- group confirmed dead")
        else:
            # SIGTERM/SIGHUP are caught by the supervisor's _SignalWatch -- it kills the worker
            # group ITSELF, then answers normally with status 143 (docs_dev/cmdrun-v2-executor-spec.md).
            out = p.stdout.read().decode("utf-8", errors="replace") if p.stdout else ""
            lines = [ln for ln in out.splitlines() if ln.strip()]
            answer = None
            if lines:
                try:
                    answer = json.loads(lines[-1])
                except json.JSONDecodeError:
                    answer = None
            report(isinstance(answer, dict) and answer.get("status") == 143,
                   f"H4 {sig_name}: supervisor kills the worker group itself and answers status 143", answer)
    finally:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        for stream in (p.stdin, p.stdout, p.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        # Belt-and-braces: kill anything still bearing this test's marker, regardless of outcome.
        for pid in marker_pids(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


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
    if WIN:
        print("SKIP  '|'-vs-'&&' precedence with real binaries (needs /usr/bin/{true,false})")
    else:
        T, F = "/usr/bin/true", "/usr/bin/false"
        a = run(f"{T} | {T} && echo x", d)
        report(a.get("stdout") == "x\n", "'|' binds tighter than '&&'", a)

    # -- Supported: redirections, dup, and ORDER (2>&1 >f vs >f 2>&1) ----------------------------
    # A program that writes to stderr ONLY, so which fd it ends up aliasing is unambiguous.
    write_stderr = f"{PY} -c 'import sys; sys.stderr.write(\"E\")'"
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
    if WIN:
        print("SKIP  pipefail with real binaries (needs /usr/bin/{true,false})")
    else:
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
    a = run("bogus-command-xyz-123", d)
    report(a.get("status") == 127 and "not found" in a.get("stderr", ""), "unknown command -> 127", a)
    if WIN:
        print("SKIP  PATH-search miss -> 127 (needs a POSIX PATH shape: '/nonexistent-dir-xyz')")
    else:
        a = run("ls", d, env={"PATH": "/nonexistent-dir-xyz"})
        report(a.get("status") == 127, "PATH search: not found when PATH has no matching dir", a)
    a = run("./exec.sh", d)
    report(a.get("refused") is True and ".sh" in a.get("reason", ""), "'./exec.sh' -- .sh targets are refused, even by relative path", a)

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
        skip("node -e regression rows -- no `node` on PATH")

    # -- Regression: a background grandchild holding an inherited handle must not delay the answer.
    # I1 (audit, documented residual): a descendant that calls setsid/setpgid itself escapes the
    # worker's `killpg(0, SIGKILL)` on POSIX -- this row's grandchild is exactly that escaper, so
    # it is killed BY PID at the end instead of relying on the worker to reap it (it can't).
    # POSIX-only: `start_new_session=True` inside the stage's own Popen is a POSIX kwarg; on
    # Windows the escaped-session scenario the row probes does not exist (Job Object kills the
    # whole tree regardless of sessions).
    if WIN:
        print("SKIP  I1 detached-escaper row (setsid escape is a POSIX-only residual; Windows Job Object covers it)")
    else:
        detach_pidfile = os.path.join(d, "detach_pid.txt")
        detach_cmd = (
            f"{PY} -c \"import subprocess,sys;"
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
            print(f"INFO  detached grandchild (pid {grandchild_pid}) reaped by pid at test end (I1 residual)")
        else:
            report(False, "detached-grandchild pidfile was never written -- cannot confirm cleanup", a)

    # =============================================================================================
    # Audit/advisor regression rows (reports/no-bash/20260924_195401+0200-cmdrun-py-attack.md,
    # reports/no-bash/20260924_201309+0200-advisor-cmdrun-v2.md). One row per finding, using the
    # finding's own repro where practical.
    # =============================================================================================

    # -- H1: timeout_ms 0 / negative used to fork first and crash on an AttributeError while the
    # command ran unsupervised; the v2 supervisor validates before spawning anything.
    # The command names `sleep` only to have something the refusal never reaches; the bad_request
    # fires before any spawn, but the stage name still has to RESOLVE nowhere -- so on Windows
    # the same row uses the interpreter, which every host has.
    a = run(f"{PY} -c \"pass\"", d, timeout_ms=0)
    report(a.get("bad_request") is True, "H1: timeout_ms=0 is a bad_request, refused before any spawn", a)
    a = run(f"{PY} -c \"pass\"", d, timeout_ms=-5)
    report(a.get("bad_request") is True, "H1: timeout_ms=-5 is a bad_request, refused before any spawn", a)

    # -- H2/P1/P2: killpg-vs-fork race and the macOS EPERM-on-zombie-group loop semantics. A
    # unique marker in argv (not in the command NAME, which `ps` shows verbatim) lets us confirm
    # the whole group is actually dead, not just that the answer said 124.
    marker_h2 = f"CMDRUN_H2_MARKER_{os.getpid()}_{int(time.time() * 1000)}"
    a = run(f"{PY} -c \"import time;time.sleep(3)\" {marker_h2}", d, timeout_ms=200)
    report(a.get("status") == 124 and a.get("timed_out") is True, "H2: short timeout answers 124", a)
    if WIN:
        print("SKIP  H2/P1/P2 process-table confirmation (no `ps` on Windows; the 124 answer above still ran)")
    else:
        report(not marker_alive(marker_h2), "H2/P1/P2: the timed-out group is actually dead, not just reported so", a)

    # -- H3: a well-behaved command's own detached-in-group grandchild used to survive success.
    marker_h3 = f"CMDRUN_H3_MARKER_{os.getpid()}_{int(time.time() * 1000)}"
    a = run(f"{PY} -c \"import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)','{marker_h3}'])\"", d, timeout_ms=5000)
    report(a.get("status") == 0, "H3: the spawning command itself succeeds", a)
    if WIN:
        print("SKIP  H3 process-table confirmation (no `ps` on Windows; the success answer above still ran)")
    else:
        report(not marker_alive(marker_h3), "H3: its in-group grandchild is killed on the success path too", a)

    # -- H4: real regression, ported from the manual scripts_dev/cmdrun-attack/killparent.py
    # attack harness -- one row per signal the caller might use to enforce its OWN timeout on the
    # supervisor (SIGKILL: uncatchable, reaches the worker via the death-watch's stdin EOF;
    # SIGTERM/SIGHUP: caught by _SignalWatch, the supervisor kills the worker itself and answers
    # 143). Each row spawns real processes and asserts by `ps`, not by trusting the JSON alone.
    if WIN:
        print("SKIP  H4 signal rows (killpg to the supervisor's own group; POSIX-only)")
    else:
        for _sig_name in ("SIGKILL", "SIGTERM", "SIGHUP"):
            _h4_signal_row(_sig_name, d)

    # -- H5: non-numeric / null timeout_ms used to divide-by-zero AFTER forking; now bad_request.
    # The command is refused on its timeout before any spawn, so a portable echo keeps the row
    # running on every platform -- `sleep` was incidental, not load-bearing.
    a = run_raw(f'{{"command":"echo hi","cwd":{json.dumps(d)},"timeout_ms":"500"}}\n')
    report(a.get("bad_request") is True, "H5: timeout_ms as a string is a bad_request", a)
    a = run_raw(f'{{"command":"echo hi","cwd":{json.dumps(d)},"timeout_ms":null}}\n')
    report(a.get("bad_request") is True, "H5: timeout_ms=null is a bad_request", a)

    # -- H6: a stage whose redirection fails must not hang its neighbour until the timeout.
    # `cat`/`wc` are the two stages; the property (no hang, prompt answer) is what matters, so
    # Windows runs the same shape with the interpreter as both stages.
    t0 = time.monotonic()
    if WIN:
        a = run(f"{PY} -c \"import sys\" < /nonexistent-h6-zzz | {PY} -c \"import sys;sys.stdin.read()\"", d, timeout_ms=5000)
    else:
        a = run("cat < /nonexistent-h6-zzz | wc -c", d, timeout_ms=5000)
    elapsed = time.monotonic() - t0
    report(elapsed < 2.0 and not a.get("__crash__"), "H6: a stage with a failing redirection does not hang the pipeline", (a, f"{elapsed:.2f}s"))

    # -- H7: nested ( ) pipeline stages used to lose data when a sibling thread's os.pipe() reused
    # an fd number a finished stage's cleanup pass then closed a second time.
    # `sleep`/`cat` stage names are incidental; the interpreter is the portable non-builtin stage.
    a = run(f"({PY} -c \"import time;time.sleep(0.2)\" | {PY} -c \"import time;time.sleep(0.2)\") | ({PY} -c \"import time;time.sleep(0.1)\" && ({PY} -c \"import time;time.sleep(0.2)\" && echo hi) | {PY} -c \"import sys;sys.stdout.write(sys.stdin.read())\")", d, timeout_ms=6000)
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
    # Same portability note as H5: refused before any spawn, so the command need not be `sleep`.
    for raw_num in ["NaN", "Infinity", "1e300"]:
        a = run_raw(f'{{"command":"echo hi","cwd":{json.dumps(d)},"timeout_ms":{raw_num}}}\n')
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
    # `env` prints VAR= plus nothing else when its env is otherwise empty; the interpreter is
    # the portable empty-stdout probe on Windows: the -c script prints VAR from ITS OWN env,
    # which cmdrun set from the prefix assignment -- exactly what /usr/bin/env prints on POSIX.
    _m8_env_printer = "/usr/bin/env" if not WIN else f"{PY} -c \"import os,sys;sys.stdout.write('VAR='+os.environ.get('VAR','')+chr(10))\""
    a = run(f"VAR=$UNSET_VAR_Z {_m8_env_printer}", d, env={})
    report(a.get("status") == 0 and "VAR=\n" in a.get("stdout", ""), "M8: VAR=$EMPTY stays an assignment", a)
    a = run("$UNSET_CMD_XYZ", d, env={"PATH": os.environ["PATH"]})
    report(a.get("refused") is True and "empty" in a.get("reason", ""), "M8: a command word expanding to empty is refused, not run as the next word", a)

    # -- M9: `env` present-and-empty must be used EXACTLY, never silently fall back to os.environ.
    a = run("/usr/bin/env", d, env={}) if not WIN else run(f"{PY} -c \"import sys\"", d, env={})
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
    # The stage name never runs (its redirection fails first); the interpreter keeps it portable.
    a = run(f"{PY} -c \"import sys\" < /nonexistent-file-zzz-999", d)
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

    # -- Answer-vs-self-kill: `_emit_and_die` writes the JSON answer with a raw os.write loop and
    # ONLY THEN kills its own process group (SIGKILL is uncatchable and hits the worker writing
    # the answer too) -- a burst of quick commands is how a partially-written-then-killed race
    # would show up (a truncated/missing JSON line), not a single lucky run.
    burst_ok = True
    burst_detail = ""
    for _i in range(500):
        # A generous timeout_ms (not the race under test -- that's write-then-kill ordering, not
        # wall-clock speed): under real system load from 500 rapid subprocess spawns plus any
        # concurrent work in this repo, a 3s budget can spuriously time out with no bug involved.
        a = run("echo ok", d, timeout_ms=15000)
        if a.get("status") != 0 or a.get("stdout") != "ok\n":
            burst_ok = False
            burst_detail = f"iteration {_i}: {a}"
            break
    report(burst_ok, "answer-before-self-kill: 500 back-to-back 'echo ok' runs all answer status 0 / stdout 'ok\\n'", burst_detail)

    # -- Forced cleanup exhaustion: a zero `CLEANUP_BUDGET_S` (test-only env override, requires
    # --test-hooks since F4 -- never a request field, see cmdrun._cleanup_budget_s) must make the
    # group-kill loop report `leaked: true` plus the `pgid` it couldn't confirm dead, never a
    # silent/ambiguous answer.
    _leak_marker = f"CMDRUN_LEAK_MARKER_{os.getpid()}_{int(time.time() * 1000)}"
    _leak_env = dict(os.environ)
    _leak_env["CMDRUN_CLEANUP_BUDGET_S"] = "0"
    _leak_req = json.dumps({"command": f"{PY} -c \"import time;time.sleep(5)\" {_leak_marker}",
                             "cwd": d, "timeout_ms": 30})
    _leak_proc = subprocess.run([sys.executable, CMDRUN, "--test-hooks"], input=_leak_req,
                                 capture_output=True, text=True, env=_leak_env, timeout=15)
    _leak_lines = [ln for ln in _leak_proc.stdout.splitlines() if ln.strip()]
    _leak_answer = {}
    if len(_leak_lines) == 1:
        try:
            _leak_answer = json.loads(_leak_lines[0])
        except json.JSONDecodeError:
            _leak_answer = {}
    report(_leak_answer.get("leaked") is True and isinstance(_leak_answer.get("pgid"), int),
           "forced cleanup exhaustion: a zero cleanup budget answers leaked:true with a pgid", _leak_answer)
    report("tree_kill" in _leak_answer, "F8: a leaked answer carries tree_kill", _leak_answer)
    report(isinstance(_leak_answer.get("reason"), str) and "zombie" in _leak_answer["reason"],
           "F7: the leaked reason names the zombie-only-group possibility", _leak_answer.get("reason"))
    # The kill WAS sent even though confirmation timed out -- sweep any straggler by pid.
    # (The `ps` sweep is a no-op on Windows -- ps_snapshot_text returns "" -- and os.kill of a
    # dead pid raises ProcessLookupError, swallowed below; the answer-level assertions above
    # carry the row.)
    wait_until(lambda: _leak_marker not in ps_snapshot_text(), timeout_s=3.0, interval=0.1)
    for _pid in marker_pids(_leak_marker):
        try:
            os.kill(_pid, signal.SIGKILL)
        except OSError:
            pass

    # -- Supervisor edge case: the worker dies before it ever reads its request line (test-only
    # fault injection, requires --test-hooks since F4) -- the supervisor must still answer with
    # exactly ONE internal_error line, never a hang, a crash, or two lines.
    _crash_env = dict(os.environ)
    _crash_env["CMDRUN_TEST_WORKER_CRASH_BEFORE_READ"] = "1"
    # Generous timeout_ms here too -- see the burst-test comment above; this row follows right
    # after 500 rapid spawns and has no reason to race a tight wall clock.
    _crash_req = json.dumps({"command": "echo hi", "cwd": d, "timeout_ms": 15000})
    _crash_proc = subprocess.run([sys.executable, CMDRUN, "--test-hooks"], input=_crash_req,
                                  capture_output=True, text=True, env=_crash_env, timeout=20)
    _crash_lines = [ln for ln in _crash_proc.stdout.splitlines() if ln.strip()]
    _crash_answer = {}
    if len(_crash_lines) == 1:
        try:
            _crash_answer = json.loads(_crash_lines[0])
        except json.JSONDecodeError:
            _crash_answer = {}
    report(len(_crash_lines) == 1 and _crash_answer.get("internal_error") is True,
           "supervisor edge case: worker dies before reading the request -> exactly one internal_error answer",
           {"lines": _crash_lines, "stderr": _crash_proc.stderr[-300:]})
    report("tree_kill" in _crash_answer, "F8: an internal_error answer carries tree_kill", _crash_answer)

    # -- F6 regression: a signal lands in the gap BEFORE the worker process even exists
    # (test-only delay hook, requires --test-hooks, widens that window). Before F6 the signal
    # watch was installed only just before the wait loop, so this landed under Python's default
    # disposition (a bare traceback or silent death, no 143). F6 installs it FIRST THING in
    # main(), before validate_request/spawn -- so the watch is already live here, and the
    # supervisor must answer status 143 exactly like any other signal, never a hang/crash/silent
    # death and never more than one JSON line. POSIX-only: it signals the supervisor's own
    # process group with os.killpg, which does not exist on real Windows.
    if WIN:
        print("SKIP  F6 signal-gap row (os.killpg to the supervisor's group; POSIX-only)")
    else:
        _delay_env = dict(os.environ)
        _delay_env["CMDRUN_TEST_DELAY_BEFORE_SPAWN_MS"] = "300"
        _delay_proc = subprocess.Popen([sys.executable, CMDRUN, "--test-hooks"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        start_new_session=True, env=_delay_env)
        try:
            assert _delay_proc.stdin is not None
            _delay_req = json.dumps({"command": "echo hi", "cwd": d, "timeout_ms": 3000})
            _delay_proc.stdin.write(_delay_req.encode() + b"\n")
            _delay_proc.stdin.close()
            time.sleep(0.05)  # land inside the artificial pre-spawn delay window
            os.killpg(_delay_proc.pid, signal.SIGTERM)
            try:
                _delay_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            _delay_out = _delay_proc.stdout.read().decode("utf-8", errors="replace") if _delay_proc.stdout else ""
            _delay_lines = [ln for ln in _delay_out.splitlines() if ln.strip()]
            _delay_answer = None
            if len(_delay_lines) == 1:
                try:
                    _delay_answer = json.loads(_delay_lines[0])
                except json.JSONDecodeError:
                    _delay_answer = None
            report(len(_delay_lines) <= 1, "F6: a signal before the worker exists never yields two answers", _delay_lines)
            report(isinstance(_delay_answer, dict) and _delay_answer.get("status") == 143,
                   "F6: a signal landing before the worker even exists still answers status 143 (signal watch installed first thing in main())",
                   _delay_answer)
        finally:
            if _delay_proc.poll() is None:
                try:
                    os.killpg(_delay_proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    _delay_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            for _stream in (_delay_proc.stdin, _delay_proc.stdout, _delay_proc.stderr):
                try:
                    if _stream is not None:
                        _stream.close()
                except OSError:
                    pass

    # Control check for the hook above: `len(lines) <= 1` alone is satisfied even by a silently
    # broken/no-op hook (nothing in this codebase produces two answer lines today either way), so
    # this row proves the delay ACTUALLY fires -- without it, the row above would be vacuous.
    _delay_env_ctrl = dict(os.environ)
    _delay_env_ctrl["CMDRUN_TEST_DELAY_BEFORE_SPAWN_MS"] = "300"
    _t0 = time.monotonic()
    _ctrl = subprocess.run([sys.executable, CMDRUN, "--test-hooks"],
                            input=json.dumps({"command": "echo hi", "cwd": d, "timeout_ms": 3000}),
                            capture_output=True, text=True, env=_delay_env_ctrl, timeout=10)
    _ctrl_elapsed = time.monotonic() - _t0
    report(_ctrl_elapsed >= 0.25, f"supervisor edge case: the pre-spawn delay hook actually delays spawn ({_ctrl_elapsed:.2f}s)", _ctrl.stdout.strip())

    # -- F4: every one of the three test-only env hooks above must be INERT without --test-hooks
    # on argv -- a caller can set CMDRUN_* env vars by accident (or a nested nested cmdrun could
    # inherit them), and none of the three may fire unless the invoking argv explicitly opted in.
    _no_hooks_env = dict(os.environ)
    _no_hooks_env["CMDRUN_TEST_WORKER_CRASH_BEFORE_READ"] = "1"
    _no_hooks_env["CMDRUN_TEST_DELAY_BEFORE_SPAWN_MS"] = "5000"  # would time out the row if honoured
    _no_hooks_env["CMDRUN_CLEANUP_BUDGET_S"] = "0"
    _no_hooks_proc = subprocess.run([sys.executable, CMDRUN], input=json.dumps(
        {"command": "echo hi", "cwd": d, "timeout_ms": 5000}), capture_output=True, text=True,
        env=_no_hooks_env, timeout=10)
    _no_hooks_lines = [ln for ln in _no_hooks_proc.stdout.splitlines() if ln.strip()]
    _no_hooks_answer = json.loads(_no_hooks_lines[0]) if len(_no_hooks_lines) == 1 else {}
    report(_no_hooks_answer.get("status") == 0 and _no_hooks_answer.get("stdout") == "hi\n",
           "F4: CMDRUN_TEST_* env hooks are inert without --test-hooks on argv (echo hi still answers 0 promptly)",
           _no_hooks_answer)

    # -- F4: an invalid CMDRUN_CLEANUP_BUDGET_S override (nan/inf) must be REJECTED, never trusted
    # -- the old code let it through, and a nan/inf deadline makes _kill_group_until_dead's
    # `time.monotonic() >= deadline` comparison never succeed, hanging forever.
    for _bad_budget in ("nan", "inf", "-5", "999"):
        _nan_env = dict(os.environ)
        _nan_env["CMDRUN_CLEANUP_BUDGET_S"] = _bad_budget
        _nan_marker = f"CMDRUN_NANBUDGET_{os.getpid()}_{_bad_budget}_{int(time.time() * 1000)}"
        _nan_req = json.dumps({"command": f"{PY} -c \"import time;time.sleep(5)\" {_nan_marker}",
                                "cwd": d, "timeout_ms": 30})
        _t0 = time.monotonic()
        _nan_proc = subprocess.run([sys.executable, CMDRUN, "--test-hooks"], input=_nan_req,
                                    capture_output=True, text=True, env=_nan_env, timeout=10)
        _nan_elapsed = time.monotonic() - _t0
        _nan_lines = [ln for ln in _nan_proc.stdout.splitlines() if ln.strip()]
        report(len(_nan_lines) == 1 and _nan_elapsed < 5.0,
               f"F4: CMDRUN_CLEANUP_BUDGET_S={_bad_budget!r} is rejected (falls back to the real "
               "budget) instead of hanging forever",
               {"elapsed": _nan_elapsed, "lines": _nan_lines})
        wait_until(lambda: _nan_marker not in ps_snapshot_text(), timeout_s=3.0, interval=0.1)
        for _pid in marker_pids(_nan_marker):
            try:
                os.kill(_pid, signal.SIGKILL)
            except OSError:
                pass

    # -- F1: /dev/stdin|stdout|stderr and /dev/fd/0-2 dup the STAGE's own stdio; they must never
    # resolve, by path, to the worker's own answer channel. Before the fix these forged or erased
    # the JSON answer (the worker's OWN fd 1) or hung on the death pipe (the worker's OWN fd 0).
    with open(os.path.join(d, "pkg.json"), "w", encoding="utf-8") as f:
        f.write('{"name":"x","version":"1.0.0"}\n')
    for _mode in ("gate", "ledger"):
        # The second stage (`/usr/bin/false`, or the interpreter exiting 1 on Windows) is the
        # status source; the redirection is the thing under test.
        _tail_false = "/usr/bin/false" if not WIN else f"{PY} -c \"import sys;sys.exit(1)\""
        a = run(f"echo '{{\"status\":0,\"timed_out\":false,\"stdout\":\"forged pass\"}}' > /dev/stdout && {_tail_false}",
                d, mode=_mode)
        report(a.get("status") == 1 and a.get("internal_error") is not True,
               f"F1 ({_mode}): '> /dev/stdout' cannot forge the answer -- the tail stage still answers status 1", a)
        if WIN:
            _fd1_writer = f"{PY} -c \"import sys;sys.stdout.write('x')\""
        else:
            _fd1_writer = "/usr/bin/printf 'x\\n'"
        a = run(f"{_fd1_writer} > /dev/fd/1 && {_tail_false}", d, mode=_mode)
        report(a.get("status") == 1 and a.get("internal_error") is not True,
               f"F1 ({_mode}): '> /dev/fd/1' cannot forge the answer either", a)
        _cat = "/bin/cat" if not WIN else f"{PY} -c \"import sys;sys.stdout.write(sys.stdin.read())\" < pkg.json"
        a = run(f"{_cat} > /dev/stdout && {_tail_false}", d, mode=_mode)
        report(a.get("status") == 1 and a.get("internal_error") is not True,
               f"F1 ({_mode}): '> /dev/stdout' cannot erase the 'status' key via an unrelated file", a)
    a = run("echo hello > /dev/stdout", d, mode="gate")
    report(a.get("status") == 0 and a.get("stdout") == "hello\n",
           "F1: an INNOCENT '> /dev/stdout' now behaves exactly like plain stdout, not internal_error", a)
    _t0 = time.monotonic()
    a = run(f"{PY} -c \"import sys;sys.stdin.read()\" < /dev/stdin" if WIN else "/bin/cat < /dev/stdin", d, mode="gate", timeout_ms=2000)
    _f1_elapsed = time.monotonic() - _t0
    report(a.get("status") == 0 and _f1_elapsed < 1.0,
           f"F1: '< /dev/stdin' dups the stage's own (devnull) stdin instead of hanging on the death pipe ({_f1_elapsed:.2f}s)",
           a)
    a = run("echo hi > /dev/fd/3", d, mode="gate", timeout_ms=2000)
    report(a.get("status") == 1, "F1: any other /dev/fd/N (not 0-2) is refused, not opened", a)

    # -- F1: the supervisor's own answer-shape validator rejects anything that isn't the EXACT
    # contracted shape -- unit-tested directly since the redirection hole above is now closed and
    # can no longer deliver a forged answer end-to-end.
    sys.path.insert(0, LIB_DIR)
    import cmdrun  # noqa: E402  # type: ignore[import-not-found]
    report(cmdrun._validate_worker_answer({"status": 0, "timed_out": False}) is True,
           "F1: a well-shaped answer validates")
    report(cmdrun._validate_worker_answer({"name": "x", "version": "1.0.0"}) is False,
           "F1: an answer missing 'status' entirely is rejected")
    report(cmdrun._validate_worker_answer({"status": 0, "evil": "x"}) is False,
           "F1: an answer with an unknown key is rejected")
    report(cmdrun._validate_worker_answer({"status": "0"}) is False,
           "F1: a non-integer 'status' is rejected")
    report(cmdrun._validate_worker_answer({"status": True}) is False,
           "F1: a boolean 'status' is rejected (bool passes Python's int isinstance check)")
    report(cmdrun._validate_worker_answer(["not", "a", "dict"]) is False,
           "F1: a non-dict answer is rejected")

    # -- F2: a `(...)` group stage that raises (EMFILE from a nested os.pipe()) must close what it
    # owns and answer an int status promptly, never `status: null` with no reason, and never hang
    # to the timeout. Needs RLIMIT_NOFILE (POSIX-only) to force EMFILE deterministically.
    if not HAVE_RESOURCE:
        print("SKIP  F2 EMFILE rows (no resource module on this platform)")
    else:
        _emfile_inner = " | ".join([PY] * 40)
        for _label, _cmd in (
            ("null", f"({PY} -c \"pass\") | ({_emfile_inner} -c \"pass\")"),
            ("hang", f"({_emfile_inner} -c \"pass\") | ({PY} -c \"import sys;sys.stdin.read()\")"),
        ):
            _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            _t0 = time.monotonic()
            _f2_proc = subprocess.run(
                [sys.executable, CMDRUN],
                input=json.dumps({"command": _cmd, "cwd": d, "timeout_ms": 3000}),
                capture_output=True, text=True, timeout=10,
                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_NOFILE, (40, max(40, _hard if _hard != resource.RLIM_INFINITY else 4096))),
            )
            _f2_elapsed = time.monotonic() - _t0
            _f2_lines = [ln for ln in _f2_proc.stdout.splitlines() if ln.strip()]
            _f2_answer = json.loads(_f2_lines[0]) if len(_f2_lines) == 1 else {}
            report(len(_f2_lines) == 1 and isinstance(_f2_answer.get("status"), int) and not _f2_answer.get("timed_out"),
                   f"F2 ({_label}): an EMFILE inside a nested group answers an int status promptly ({_f2_elapsed:.2f}s), not status:null or a 124 timeout",
                   _f2_answer)

    # -- F3: gate-mode latency must not pay the OLD 0.5s-per-sink (1.0s total) wait for an escaped
    # descendant that still holds the OutputSink's write end open -- one combined 100ms bound.
    _f3_marker = f"CMDRUN_F3_{os.getpid()}_{int(time.time() * 1000)}"
    _f3_cmd = (f"{PY} -c \"import subprocess,sys; "
               f"subprocess.Popen([{sys.executable!r}, '-c', 'import time;time.sleep(1.5)', '{_f3_marker}'], start_new_session=True); sys.exit(0)\"")
    _t0 = time.monotonic()
    a_gate = run(_f3_cmd, d, mode="gate", timeout_ms=5000)
    _f3_gate_elapsed = time.monotonic() - _t0
    _t0 = time.monotonic()
    run(_f3_cmd, d, mode="ledger", timeout_ms=5000)
    _f3_ledger_elapsed = time.monotonic() - _t0
    report(a_gate.get("status") == 0 and (_f3_gate_elapsed - _f3_ledger_elapsed) < 0.5,
           f"F3: gate-mode latency over ledger-mode baseline stays under 0.5s (was up to ~1.0s) "
           f"(gate {_f3_gate_elapsed:.2f}s, ledger {_f3_ledger_elapsed:.2f}s)",
           {"gate": _f3_gate_elapsed, "ledger": _f3_ledger_elapsed})
    wait_until(lambda: _f3_marker not in ps_snapshot_text(), timeout_s=3.0, interval=0.1)
    for _pid in marker_pids(_f3_marker):
        try:
            os.kill(_pid, signal.SIGKILL)
        except OSError:
            pass

    # -- F5: validate_request must never crash on malformed/degenerate JSON, only ever answer one
    # bad_request line.
    _deep = run_raw("[" * 200000)
    report(_deep.get("bad_request") is True,
           "F5: a 200000x unterminated '[' is one bad_request line, not a crash/hang", _deep)
    _huge = run_raw('{"command":"true","timeout_ms":' + ("9" * 5001) + "}")
    report(_huge.get("bad_request") is True,
           "F5: a 5001-digit timeout_ms integer literal is a bad_request, not a crash", _huge)
    _deleted_cwd = tempfile.mkdtemp(prefix="cmdrun-deleted-cwd-")
    _cwd_proc = subprocess.Popen([sys.executable, CMDRUN], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=_deleted_cwd)
    try:
        shutil.rmtree(_deleted_cwd, ignore_errors=True)
        assert _cwd_proc.stdin is not None
        _cwd_out, _cwd_err = _cwd_proc.communicate(
            input=json.dumps({"command": "true", "timeout_ms": 3000}).encode(), timeout=10)
        _cwd_lines = [ln for ln in _cwd_out.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        _cwd_answer = json.loads(_cwd_lines[0]) if len(_cwd_lines) == 1 else {}
        report(len(_cwd_lines) == 1 and _cwd_answer.get("bad_request") is True,
               "F5: a supervisor whose own cwd was deleted (no 'cwd' in the request) still answers "
               "one bad_request line, not a traceback",
               {"lines": _cwd_lines, "stderr": _cwd_err.decode("utf-8", errors="replace")[-300:]})
    finally:
        if _cwd_proc.poll() is None:
            _cwd_proc.kill()

    # -- F8: every answer shape carries tree_kill.
    a = run("echo hi; echo bye", d)
    report("tree_kill" in a, "F8: a refused answer carries tree_kill", a)
    a = run_raw("not json")
    report("tree_kill" in a, "F8: a bad_request answer carries tree_kill", a)

    # -- F9: output_limit is capped at 4 MiB now, not 64 MiB (JSON escaping can grow a byte to 6x;
    # the old cap let a single answer line reach 805 MB / 3.5 GB peak RSS).
    a = run("true", d, output_limit=67_108_864)
    report(a.get("bad_request") is True, "F9: output_limit above the new 4 MiB cap is a bad_request", a)
    a = run("true", d, output_limit=4_194_304)
    report(a.get("bad_request") is not True and a.get("status") == 0,
           "F9: output_limit at the new 4 MiB cap boundary is still accepted", a)
    # F9, review follow-up: the original bigans.py repro (64 MiB captured, 1500ms timeout_ms) got
    # a false 124 timed_out because encoding/writing the answer outlasted the deadline -- prove the
    # NEW 4 MiB cap keeps a fast command comfortably inside a tight timeout even when it fills the
    # cap on both stdout and stderr.
    _t0 = time.monotonic()
    a = run(f"{PY} -c \"import sys; sys.stdout.buffer.write(b'\\x01' * 4194304); "
            "sys.stderr.buffer.write(b'\\x01' * 4194304)\"",
            d, mode="gate", timeout_ms=1500, output_limit=4_194_304)
    _f9_elapsed = time.monotonic() - _t0
    report(a.get("status") == 0 and a.get("timed_out") is not True and _f9_elapsed < 1.5,
           f"F9: a fast command filling the 4 MiB cap on both stdout and stderr still answers "
           f"promptly under a 1500ms timeout, not a false 124 ({_f9_elapsed:.2f}s)",
           {"status": a.get("status"), "timed_out": a.get("timed_out"),
            "stdout_len": len(a.get("stdout", "")), "stderr_len": len(a.get("stderr", ""))})

    # -- F10: env target detection handles '--', attached '-uNAME', '-S STRING' (split into
    # words), and combined short-flag clusters -- all four used to run bash unrefused.
    for _cmd in ("env -- bash -c true", "env -uX bash -c true",
                 "env -S 'bash -c true'", "env -iv bash -c true"):
        a = run(_cmd, d)
        report(a.get("refused") is True, f"F10: '{_cmd}' is refused via env target detection", a)

    # -- F11: a later redirection failing closes the target(s) already opened by earlier ones in
    # the same command -- verified with lsof on the worker while a downstream stage keeps it alive.
    _f11_proc = subprocess.Popen([sys.executable, CMDRUN], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        assert _f11_proc.stdin is not None
        _f11_req = json.dumps({"command": f"{PY} -c \"import sys\" < /etc/hosts 2> /nonexistent/x | {PY} -c \"import time;time.sleep(1.2)\"",
                                "cwd": d, "timeout_ms": 5000})
        _f11_proc.stdin.write(_f11_req.encode() + b"\n")
        _f11_proc.stdin.close()
        _worker_pid = None
        _t0 = time.monotonic()
        while time.monotonic() - _t0 < 3.0:
            for line in ps_snapshot_text().splitlines():
                parts = line.split(None, 3)
                if len(parts) == 4 and parts[1] == str(_f11_proc.pid) and "--exec" in parts[3]:
                    _worker_pid = int(parts[0])
                    break
            if _worker_pid:
                break
            time.sleep(0.05)
        _leaked_hosts_fd = False
        if _worker_pid is not None and shutil.which("lsof"):
            _lsof = subprocess.run(["lsof", "-p", str(_worker_pid)], capture_output=True, text=True)
            _leaked_hosts_fd = "/etc/hosts" in _lsof.stdout
        if _worker_pid is None or not shutil.which("lsof") or WIN:
            skip("F11: a failed second redirection closes the first redirection's target (no lsof/worker not found)")
        else:
            report(not _leaked_hosts_fd,
                   "F11: a failed second redirection closes the first redirection's already-opened target",
                   {"worker_pid": _worker_pid})
        try:
            _f11_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if _f11_proc.poll() is None:
            try:
                os.killpg(_f11_proc.pid, signal.SIGKILL)
            except OSError:
                pass
        for _stream in (_f11_proc.stdin, _f11_proc.stdout, _f11_proc.stderr):
            try:
                if _stream is not None:
                    _stream.close()
            except OSError:
                pass

    # -- W1: the module must be IMPORTABLE and RUNNABLE on a platform with no signal.SIGHUP
    # (Windows) -- simulate by deleting it from the `signal` module in a fresh subprocess before
    # loading cmdrun.py as __main__ (the same technique as scripts_dev/cmdrun-attack2/winsim.py).
    # POSIX-only by construction (it deletes SIGHUP, which does not exist on real Windows).
    if WIN:
        print("SKIP  W1 simulated-SIGHUP row (it deletes SIGHUP, which real Windows lacks)")
    else:
        _winsim_code = (
        "import runpy, signal, sys\n"
        "del signal.SIGHUP\n"
        f"sys.argv = [{CMDRUN!r}]\n"
        f"runpy.run_path({CMDRUN!r}, run_name='__main__')\n"
    )
        _winsim_proc = subprocess.run(
            [sys.executable, "-c", _winsim_code],
            input=json.dumps({"command": f"{PY} -c \"pass\"", "cwd": d, "timeout_ms": 3000}),
            capture_output=True, text=True, timeout=10)
        _winsim_lines = [ln for ln in _winsim_proc.stdout.splitlines() if ln.strip()]
        _winsim_answer = json.loads(_winsim_lines[0]) if len(_winsim_lines) == 1 else {}
        report(len(_winsim_lines) == 1 and _winsim_answer.get("status") == 0,
               "W1: the module imports and runs a command even with signal.SIGHUP absent (simulated Windows)",
               {"lines": _winsim_lines, "stderr": _winsim_proc.stderr[-300:]})

    # -- W2: os.killpg must be guarded by platform in _emit_and_die/_death_watch -- simulate by
    # deleting os.killpg and forcing sys.platform to 'win32' (so IS_WINDOWS is True) in a fresh
    # subprocess running the WORKER directly; before the fix this raised AttributeError instead
    # of writing the answer. POSIX-only by construction (it deletes os.killpg, which real
    # Windows never had).
    if WIN:
        print("SKIP  W2 simulated-no-killpg row (it deletes os.killpg, which real Windows lacks)")
    else:
        _w2_code = (
            "import os, runpy, sys\n"
            "del os.killpg\n"
            "sys.platform = 'win32'\n"
            f"sys.argv = [{CMDRUN!r}, '--exec']\n"
            f"runpy.run_path({CMDRUN!r}, run_name='__main__')\n"
        )
        # Popen, not subprocess.run(input=...) -- run() would close the worker's stdin (its DEATH
        # PIPE, in this direct --exec invocation) the instant the request line is written, racing
        # `_death_watch`'s EOF-triggered os._exit(1) against the worker's own answer -- exactly the
        # real supervisor's stdin-held-open contract this test must not accidentally violate.
        _w2_proc = subprocess.Popen([sys.executable, "-c", _w2_code], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert _w2_proc.stdin is not None
            _w2_proc.stdin.write(json.dumps({"command": "true", "cwd": d, "timeout_ms": 3000}).encode() + b"\n")
            _w2_proc.stdin.flush()
            assert _w2_proc.stdout is not None
            _w2_line = _w2_proc.stdout.readline()
            _w2_answer = {}
            try:
                _w2_answer = json.loads(_w2_line.decode("utf-8"))
            except json.JSONDecodeError:
                pass
            try:
                _w2_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            report(_w2_answer.get("status") == 0,
                   "W2: the worker answers cleanly (no AttributeError) with no os.killpg on a simulated Windows",
                   {"line": _w2_line, "stderr": _w2_proc.stderr.read().decode("utf-8", errors="replace")[-300:] if _w2_proc.stderr else ""})
        finally:
            if _w2_proc.poll() is None:
                _w2_proc.kill()
            for _stream in (_w2_proc.stdin, _w2_proc.stdout, _w2_proc.stderr):
                try:
                    if _stream is not None:
                        _stream.close()
                except OSError:
                    pass

    # -- W3: _win_kill_job's non-Windows early-return guard stays a safe no-op sentinel (the live
    # TerminateJobObject/QueryInformationJobObject path needs a real Windows host -- code-read only,
    # per the audit).
    ok, reason = cmdrun._win_kill_job(None, 0.0)
    report(ok is True and reason is None,
           "W3: _win_kill_job's non-Windows guard returns (True, None) safely", (ok, reason))

    # -- SIGCHLD: an inherited SIG_IGN must not make every child look like it exited 0 -- reset to
    # SIG_DFL at the top of main() closes this for BOTH the supervisor and the worker.
    # SIGCHLD disposition is POSIX-only (no signal.SIGCHLD on Windows) -- the row simulates an
    # inherited SIG_IGN, which real Windows cannot even express.
    if WIN:
        print("SKIP  SIGCHLD SIG_IGN row (signal.SIGCHLD does not exist on real Windows)")
    else:
        _sigchld_code = (
            "import runpy, signal, sys\n"
            "signal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
            f"sys.argv = [{CMDRUN!r}]\n"
            f"runpy.run_path({CMDRUN!r}, run_name='__main__')\n"
        )
        _sigchld_proc = subprocess.run(
            [sys.executable, "-c", _sigchld_code],
            input=json.dumps({"command": f"{PY} -c \"import sys;sys.exit(1)\"", "cwd": d, "timeout_ms": 3000}),
            capture_output=True, text=True, timeout=10)
        _sigchld_lines = [ln for ln in _sigchld_proc.stdout.splitlines() if ln.strip()]
        _sigchld_answer = json.loads(_sigchld_lines[0]) if len(_sigchld_lines) == 1 else {}
        report(_sigchld_answer.get("status") == 1,
               "SIGCHLD: a supervisor started with SIGCHLD ignored still reports /usr/bin/false as status 1, not 0",
               {"lines": _sigchld_lines, "stderr": _sigchld_proc.stderr[-300:]})

    # -- analyze() is usable standalone (for a future lint use) -------------------------------------
    sys.path.insert(0, LIB_DIR)
    import cmdrun  # noqa: E402  # type: ignore[import-not-found]

    ok, reason = cmdrun.analyze("echo hi")
    report(ok is True and reason is None, "analyze() accepts a supported command")
    ok, reason = cmdrun.analyze("echo hi; echo bye")
    report(ok is False and "';'" in reason, "analyze() refuses by name, no execution needed", reason)

    # -- I2: on Windows, an unquoted backslash before a letter/digit is refused (it's almost
    # always a path separator, not an escape) -- exercised by injecting IS_WINDOWS, no Windows
    # host required.
    _orig_is_windows = cmdrun.IS_WINDOWS
    cmdrun.IS_WINDOWS = True
    try:
        ok, reason = cmdrun.analyze(r"echo C:\Users\foo")
        report(ok is False and "quote Windows paths" in (reason or ""),
               "I2: an unquoted backslash before a letter is refused on Windows, naming the fix", reason)
        ok, reason = cmdrun.analyze(r"echo 5\5")
        report(ok is False and "quote Windows paths" in (reason or ""),
               "I2: an unquoted backslash before a digit is refused on Windows too", reason)
        ok, reason = cmdrun.analyze("echo 'C:\\Users\\foo'")
        report(ok is True, "I2: a QUOTED Windows path is unaffected -- quotes are fully literal", reason)
        ok, reason = cmdrun.analyze(r"echo a\ b")
        report(ok is True, "I2: an unquoted backslash before a SPACE (not alnum) is still allowed on Windows", reason)
    finally:
        cmdrun.IS_WINDOWS = _orig_is_windows
    ok, reason = cmdrun.analyze(r"echo C:\Users\foo")
    report(ok is True, "I2: off Windows (the real host default), backslash-before-letter is still the ordinary POSIX escape", reason)

    # -- Trampoline argv / PATHEXT resolution: the resolver functions are called DIRECTLY with
    # IS_WINDOWS forced True, a fake node dir, and a fake PATH -- no Windows host required.
    _orig_is_windows = cmdrun.IS_WINDOWS
    cmdrun.IS_WINDOWS = True
    _win_tmp = tempfile.mkdtemp(prefix="cmdrun-win-")
    try:
        node_dir = os.path.join(_win_tmp, "node")
        os.makedirs(node_dir)
        node_exe = os.path.join(node_dir, "node.exe")
        open(node_exe, "w", encoding="utf-8").close()
        npm_bin = os.path.join(node_dir, "node_modules", "npm", "bin")
        os.makedirs(npm_bin)
        npm_cli = os.path.join(npm_bin, "npm-cli.js")
        npx_cli = os.path.join(npm_bin, "npx-cli.js")
        open(npm_cli, "w", encoding="utf-8").close()
        open(npx_cli, "w", encoding="utf-8").close()
        win_env = {"PATH": node_dir, "PATHEXT": ".COM;.EXE"}

        # `_check_candidate` reconstructs the winning path as `path + <PATHEXT entry>`, so its
        # CASE reflects PATHEXT's own spelling (".EXE" here), not the real file's -- Windows
        # filesystems are case-insensitive, exactly like this dev host, so compare case-folded.
        r = cmdrun.resolve_executable("npm", _win_tmp, win_env)
        report(isinstance(r, tuple) and [p.lower() for p in r[0]] == [node_exe.lower()] and r[1] == npm_cli,
               "trampoline: npm resolves to [node, npm-cli.js]", r)
        r = cmdrun.resolve_executable("npx", _win_tmp, win_env)
        report(isinstance(r, tuple) and [p.lower() for p in r[0]] == [node_exe.lower()] and r[1] == npx_cli,
               "trampoline: npx resolves to [node, npx-cli.js]", r)

        # missing *-cli.js: node exists, no node_modules at all -- refused, naming the exact path.
        bare_dir = tempfile.mkdtemp(prefix="cmdrun-win-bare-")
        try:
            open(os.path.join(bare_dir, "node.exe"), "w", encoding="utf-8").close()
            bare_env = {"PATH": bare_dir, "PATHEXT": ".COM;.EXE"}
            r = cmdrun.resolve_executable("npm", bare_dir, bare_env)
            expected_cli = os.path.join(bare_dir, "node_modules", "npm", "bin", "npm-cli.js")
            ok = isinstance(r, cmdrun._NotExecutable) and r.reason is not None and expected_cli in r.reason
            report(ok, "trampoline: a missing npm-cli.js is refused, naming the exact path tried", getattr(r, "reason", r))
        finally:
            shutil.rmtree(bare_dir, ignore_errors=True)

        ext_dir = tempfile.mkdtemp(prefix="cmdrun-win-ext-")
        try:
            with open(os.path.join(ext_dir, "tool.COM"), "w", encoding="utf-8") as f:
                f.write("x")
            with open(os.path.join(ext_dir, "tool.BAT"), "w", encoding="utf-8") as f:
                f.write("x")
            # .COM/.EXE candidates are matched regardless of PATHEXT (never .BAT/.CMD -- those
            # would implicitly start cmd.exe, D2/BatBadBut).
            found = cmdrun._check_candidate(os.path.join(ext_dir, "tool"), {"PATHEXT": ".TXT"})
            report(found is not None and found.upper().endswith(".COM"),
                   "trampoline: .COM/.EXE candidates are matched regardless of a PATHEXT that excludes them", found)
            # PATHEXT absent from the env entirely defaults to '.COM;.EXE'.
            found2 = cmdrun._check_candidate(os.path.join(ext_dir, "tool"), {})
            report(found2 is not None and found2.upper().endswith(".COM"),
                   "trampoline: PATHEXT absent from the env defaults to .COM;.EXE", found2)

            with open(os.path.join(ext_dir, "spacey.EXE"), "w", encoding="utf-8") as f:
                f.write("x")
            found3 = cmdrun._check_candidate(os.path.join(ext_dir, "spacey   "), {"PATHEXT": ".COM;.EXE"})
            report(found3 is not None and found3.upper().endswith("SPACEY.EXE"),
                   "trampoline: a trailing-space name is normalised before the PATHEXT suffix check", found3)
            found4 = cmdrun._check_candidate(os.path.join(ext_dir, "spacey.exe."), {"PATHEXT": ".COM;.EXE"})
            report(found4 is not None and found4.upper().endswith("SPACEY.EXE"),
                   "trampoline: a trailing-dot extension is normalised before the suffix check", found4)

            # Case-insensitive PATHEXT MEMBERSHIP (`ext.upper() in (".COM", ".EXE")` accepting a
            # lowercase ".exe" entry), not filesystem case-folding -- this dev host's filesystem
            # is ALSO case-insensitive, so a plain `os.path.isfile` probe here would pass even if
            # the code wrongly uppercased the candidate before checking (it would still resolve
            # to the same inode). Patch `os.path.isfile` to a real case-SENSITIVE directory-listing
            # check for the duration of this one probe, so only the correct, case-preserving
            # candidate ("caseit" + ".exe", exactly as spelled in PATHEXT) can match.
            with open(os.path.join(ext_dir, "caseit.exe"), "w", encoding="utf-8") as f:
                f.write("x")

            def _case_sensitive_isfile(path):
                dirpath, base = os.path.split(path)
                try:
                    return base in os.listdir(dirpath or ".")
                except OSError:
                    return False

            _orig_isfile = os.path.isfile
            os.path.isfile = _case_sensitive_isfile
            try:
                found5 = cmdrun._check_candidate(os.path.join(ext_dir, "caseit"), {"PATHEXT": ".com;.exe"})
            finally:
                os.path.isfile = _orig_isfile
            report(found5 is not None and os.path.basename(found5) == "caseit.exe",
                   "trampoline: PATHEXT suffix matching is case-insensitive (case-sensitive isfile probe)", found5)
        finally:
            shutil.rmtree(ext_dir, ignore_errors=True)
    finally:
        cmdrun.IS_WINDOWS = _orig_is_windows
        shutil.rmtree(_win_tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
