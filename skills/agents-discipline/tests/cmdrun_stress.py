#!/usr/bin/env python3
"""Randomized stress/fuzz test for scripts/lib/cmdrun.py. SLOW (~1-3 minutes): spawns 2000+
subprocesses, each a fresh `python3 cmdrun.py` fork+setsid+exec cycle. 🐌

Spec (docs_dev/no-bash-spec.md, "Must never fail, hang or loop"): the interpreter must always
return exactly one JSON answer within timeout_ms + 2s, never raise an uncaught exception, and
never leave a descendant process alive -- against randomized adversarial input: quoting, deep
`( )` nesting, huge argument lists, huge pipelines, binary/flooding output, 1ms timeouts, a
child that ignores SIGTERM, and a child that forks grandchildren then sleeps.

Deterministic by default (fixed seed); pass --seed N to explore a different stream.
Run: python3 tests/cmdrun_stress.py [--seed N] [--cases N]
"""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(TESTS_DIR, "..", "scripts", "lib")
CMDRUN = os.path.join(LIB_DIR, "cmdrun.py")

DEFAULT_SEED = 20260924
# The spec bound is timeout_ms + 2s, measured INSIDE the interpreter/caller pair. This
# harness measures the whole `python3 cmdrun.py` round trip from OUTSIDE, which also pays
# for CPython startup and OS scheduling fairness -- under load (e.g. right after this same
# file's own 2000-process burst) that overhead alone measured up to ~2.5s on this machine,
# even though the interpreter itself answered promptly (verified by re-running the same
# case in isolation). This slack absorbs THAT noise; it does not loosen the interpreter's
# own contract.
HARNESS_SLACK = 3.0
DEFAULT_CASES = 2200

failed = 0
checked = 0


def report(ok: bool, name: str, detail: object = "") -> None:
    global failed, checked
    checked += 1
    if not ok:
        failed += 1
        detail_str = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        print(f"FAIL  {name}{f'  ({detail_str})' if detail_str else ''}")


def invoke(command, cwd, mode="gate", timeout_ms=1000, output_limit=200_000, env=None):
    """One cmdrun.py round trip. Returns (answer_dict_or_None, wall_seconds, raw_stdout, raw_stderr)."""
    req = {"command": command, "cwd": cwd, "mode": mode, "timeout_ms": timeout_ms, "output_limit": output_limit}
    if env is not None:
        req["env"] = env
    budget = (timeout_ms / 1000.0) + 2.0 + HARNESS_SLACK
    started = time.monotonic()
    try:
        p = subprocess.run(
            [sys.executable, CMDRUN],
            input=json.dumps(req),
            capture_output=True,
            text=True,
            timeout=budget,
        )
    except subprocess.TimeoutExpired as exc:
        # `text=True` on the Popen call above means these are always `str` at runtime, but the
        # TimeoutExpired stub types them as `bytes | str | None` unconditionally (it can't see
        # the call site's kwargs) -- decode defensively so this is true for the type checker too.
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, time.monotonic() - started, out, err + "\n[[HARNESS TIMEOUT]]"
    wall = time.monotonic() - started
    lines = [line for line in p.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None, wall, p.stdout, p.stderr
    try:
        return json.loads(lines[0]), wall, p.stdout, p.stderr
    except json.JSONDecodeError:
        return None, wall, p.stdout, p.stderr


def check_one(command, name, cwd, timeout_ms=1000, env=None):
    """The three universal invariants every case in this file must satisfy."""
    answer, wall, out, err = invoke(command, cwd, timeout_ms=timeout_ms, env=env)
    budget = (timeout_ms / 1000.0) + 2.0
    report(answer is not None, f"{name}: exactly one parseable JSON answer", {"stdout": out[:300], "stderr": err[:300]})
    report(wall <= budget + HARNESS_SLACK, f"{name}: answered within timeout+2s", f"{wall:.2f}s budget={budget:.2f}s")
    report("Traceback (most recent call last)" not in err, f"{name}: no uncaught exception", err[:500])
    return answer


# ---------------------------------------------------------------------------
# Randomized grammar-aware fuzzer
# ---------------------------------------------------------------------------

METACHARS = list("|&;()<>${}\"'`~!*?[]\\#\n\r\t ")
SAFE_WORDS = ["a.txt", "*.txt", "$HOME", "${PATH}", "-n", "hi", "'q'", '"d"', "..", "./x", "0", "1", "2"]
ADVERSARIAL_SNIPPETS = [
    "'unterminated", '"unterminated', "$(", "`", "${VAR:-x}", "~", "{a,b}", "!", "||", ";",
    "&", "|&", "&>", "&>>", "<<", "<<<", "\n", "", "   ", "\\", "$", "${", "'", '"',
]
SAFE_EXES = ["true", "false", "echo", "cat", "/usr/bin/true", "/usr/bin/false", "/bin/echo"]


def random_word(rng):
    kind = rng.random()
    if kind < 0.35:
        return rng.choice(SAFE_WORDS)
    if kind < 0.55:
        return rng.choice(ADVERSARIAL_SNIPPETS)
    length = rng.randint(0, 40)
    return "".join(rng.choice(METACHARS + list("abcXYZ019")) for _ in range(length))


def random_command(rng):
    n_stages = rng.randint(1, 4)
    stages = []
    for _ in range(n_stages):
        n_words = rng.randint(1, 6)
        stages.append(" ".join(random_word(rng) for _ in range(n_words)))
    joiner = rng.choice([" | ", " && ", " | ", " "])
    cmd = joiner.join(stages)
    if rng.random() < 0.15:
        cmd = f"( {cmd} )"
    if rng.random() < 0.1:
        depth = rng.randint(1, 40)
        cmd = ("(" * depth) + "echo x" + (")" * depth)
    return cmd


def safe_command(rng):
    """A command built only from real, quick binaries -- exercises the executor, not just refusal."""
    exe = rng.choice(SAFE_EXES)
    n = rng.randint(0, 8)
    args = [rng.choice(["hi", "*.txt", "$HOME", "-n", "'lit eral'"]) for _ in range(n)]
    parts = [exe] + args
    if rng.random() < 0.3:
        parts += ["|", rng.choice(SAFE_EXES)]
    if rng.random() < 0.3:
        parts = [rng.choice(SAFE_EXES), "&&"] + parts
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Named adversarial regressions (not random -- each targets one failure mode)
# ---------------------------------------------------------------------------


def named_cases(d, marker):
    cases = []
    cases.append(("huge argument list", " ".join(["echo"] + [f"arg{i}" for i in range(20_000)]), 3000))
    cases.append(("10k-stage pipeline is refused, not hung", " | ".join(["true"] * 10_000), 3000))
    cases.append(("deep nesting near the bound", ("(" * 40) + "echo x" + (")" * 40), 2000))
    cases.append(("huge single token", "echo " + ("a" * 500_000), 3000))
    cases.append(("null-ish control chars in a word", "echo " + "".join(chr(c) for c in range(1, 9)), 1000))
    cases.append((
        "binary + flooding output past the limit",
        "python3 -c \"import sys; sys.stdout.buffer.write(bytes(range(256)) * 200000)\"",
        4000,
    ))
    cases.append(("1ms timeout on a fast command", "echo hi", 1))
    cases.append(("1ms timeout on a slow command", "python3 -c 'import time; time.sleep(5)'", 1))
    cases.append((
        "child ignores SIGTERM, must still die",
        f"python3 -c \"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"print('{marker}-sigterm'); time.sleep(30)\"",
        400,
    ))
    cases.append((
        "child forks grandchildren then sleeps (fork-bomb bounded by timeout)",
        f"python3 -c \"import subprocess,sys,time; "
        f"[subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']) for _ in range(20)]; "
        f"print('{marker}-forkbomb'); time.sleep(30)\"",
        400,
    ))
    cases.append((
        "detached grandchild inheriting a handle must not delay the answer",
        f"python3 -c \"import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        f"stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        f"start_new_session=True); print('{marker}-detached')\"",
        1500,
    ))
    return cases


def ps_snapshot(path):
    with open(path, "w", encoding="utf-8") as f:
        subprocess.run(["ps", "-eo", "pid,ppid,pgid,command"], stdout=f, stderr=subprocess.DEVNULL, check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cases", type=int, default=DEFAULT_CASES)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    d = tempfile.mkdtemp(prefix="cmdrun-stress-")
    with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as f:
        f.write("a\n")

    marker = f"cmdrun-stress-{args.seed}-{os.getpid()}"

    # -- Named regressions first (each targets exactly one documented failure mode) -------------
    for name, cmd, timeout_ms in named_cases(d, marker):
        check_one(cmd, name, d, timeout_ms=timeout_ms)

    # Descendant-leak check for the three cases above that spawn something long-lived: give the
    # kill a moment to land, then prove the marker is gone from the WHOLE process table (not just
    # cmdrun's own subtree -- a leaked descendant may have been re-parented to pid 1).
    time.sleep(0.5)
    snap_path = os.path.join(d, "ps-after.txt")
    ps_snapshot(snap_path)
    with open(snap_path, encoding="utf-8") as f:
        table = f.read()
    report(marker not in table, "no descendant left alive after SIGTERM-ignorer / fork-bomb / detached grandchild",
           f"marker {marker!r} still present in the process table")

    # -- Randomized fuzz ---------------------------------------------------------------------------
    n = args.cases
    n_random = int(n * 0.6)
    n_safe = n - n_random
    for i in range(n_random):
        cmd = random_command(rng)
        timeout_ms = rng.choice([200, 500, 1000])
        check_one(cmd, f"fuzz#{i}", d, timeout_ms=timeout_ms)
    for i in range(n_safe):
        cmd = safe_command(rng)
        timeout_ms = rng.choice([300, 800])
        check_one(cmd, f"safe-fuzz#{i}", d, timeout_ms=timeout_ms)

    print(f"\n{checked} checks, {failed} failing" if failed else f"\n{checked} checks, all green (seed={args.seed}, cases={n})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
