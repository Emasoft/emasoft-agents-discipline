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
        ("", "empty"),
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

    # -- Regression: a background grandchild holding an inherited handle must not delay the answer
    detach_cmd = (
        "python3 -c \"import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        "start_new_session=True)\""
    )
    started = time.monotonic()
    a = run(detach_cmd, d, timeout_ms=5000)
    elapsed = time.monotonic() - started
    report(elapsed < 2.0 and not a.get("__crash__"), "detached grandchild does not delay the answer", (a, f"{elapsed:.2f}s"))

    # -- analyze() is usable standalone (for a future lint use) -------------------------------------
    sys.path.insert(0, LIB_DIR)
    import cmdrun  # noqa: E402  # type: ignore[import-not-found]

    ok, reason = cmdrun.analyze("echo hi")
    report(ok is True and reason is None, "analyze() accepts a supported command")
    ok, reason = cmdrun.analyze("echo hi; echo bye")
    report(ok is False and "';'" in reason, "analyze() refuses by name, no execution needed", reason)


if __name__ == "__main__":
    main()
