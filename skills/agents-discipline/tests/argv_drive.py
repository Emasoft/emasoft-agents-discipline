#!/usr/bin/env python3
"""Run the PORT (scripts/gate_check.py) over the oracle driver's argv corpus.

Pair of tests/argv-drive.mjs -- read that file for why this differential is black-box, and for
why an accepted numeric value is paired with a failing --cwd to make acceptance observable.
CASES below is shared VERBATIM with it; the two lists must stay identical row for row.
"""

import sys
sys.dont_write_bytecode = True
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "..", "scripts", "gate_check.py")

MISSING = "/nonexistent-agents-discipline-xyz"

# Shared VERBATIM with argv-drive.mjs. Non-printing characters are built with chr(), never
# written literally -- the same discipline digest_drive.py follows, and for the same reason:
# a source file that carries a raw U+2028 loses it silently on the next edit.
CASES = [
    # --- parseArgs: unknown / malformed options ---
    ["--bogus"],
    ["-x"],
    ["-"],
    ["-hh"],
    ["--="],
    ["--unknown=value"],
    # --- parseArgs: a value option with no value ---
    ["--timeout"],
    ["--timeout="],
    ["--jobs"],
    ["--shell="],
    ["--scope="],
    ["--timeout", "--jobs"],
    # --- parseArgs: duplicate detection ---
    ["--scope=x", "--scope=y"],
    ["--scope", "a", "--scope", "b"],
    ["--status", "--status"],
    ["--claim", "--claim"],
    ["--help", "-h"],
    # --- help ---
    ["-h"],
    ["--help"],
    ["--help", "--bogus"],
    # --- mutual exclusion ---
    ["--status", "--reverify"],
    ["--status", "--approve"],
    ["--claim", "--release"],
    ["--log", "text", "--list-scopes"],
    ["--claim", "--status"],
    ["--release", "--reverify"],
    ["--claim", "ledger.md"],
    ["ledger.md", "--scope", "s"],
    ["--leaf", "x"],
    ["--timeout", "5", "--status"],
    ["--jobs", "2", "--claim"],
    ["--shell", "/bin/sh", "--status"],
    ["--cwd", ".", "--status"],
    # --- asDirectory ---
    ["--root", MISSING],
    ["--cwd", MISSING],
    # --- value REJECTIONS ---
    ["--timeout", "abc"],
    ["--timeout", "0"],
    ["--timeout", "86401"],
    ["--timeout", "5.5"],
    ["--timeout", "1_000"],
    ["--timeout", "-5"],
    ["--timeout", "Infinity"],
    ["--timeout", "1e400"],
    ["--timeout", "NaN"],
    ["--timeout", "0x"],
    ["--timeout", "+0x10"],
    ["--jobs", "65"],
    ["--jobs", "0"],
    ["--jobs", "abc"],
    ["--jobs", "2.5"],
    # --- value ACCEPTANCE, paired with a failing --cwd ---
    ["--cwd", MISSING, "--timeout", "0x15180"],
    ["--cwd", MISSING, "--timeout", "0x15181"],
    ["--cwd", MISSING, "--jobs", "0x40"],
    ["--cwd", MISSING, "--jobs", "0x41"],
    ["--cwd", MISSING, "--timeout", "1e3"],
    ["--cwd", MISSING, "--timeout", "5.0"],
    ["--cwd", MISSING, "--timeout", " 12 "],
    ["--cwd", MISSING, "--timeout", "1e-3"],
    ["--cwd", MISSING, "--jobs", "1e1"],
    ["--cwd", MISSING, "--timeout", "1"],
    ["--cwd", MISSING, "--timeout", "86400"],
    # --- `--` and positional handling ---
    ["--root", MISSING, "--", "--bogus"],
    ["--root", MISSING, "--", "-h"],
    ["--root", MISSING, "--scope", "s", "--", "--"],
    ["--scope", "s", "--", "ledger.md"],
    # --- hostile values ---
    ["--timeout", chr(0x2028)],
    ["--timeout", "a" + chr(0x001B) + "b"],
    ["--timeout", chr(0x00E9)],
    ["--timeout", chr(0x202F)],
    ["--timeout", "a" + chr(0x0000) + "b"],
    ["--timeout", chr(0x200E)],
    ["--timeout", '"quoted"'],
    ["--timeout", "back\\slash"],
]


def normalize(stdout):
    """See argv-drive.mjs: collapse ONLY the program's self-name in its own usage line."""
    return stdout.replace("usage: gate_check.py ", "usage: <PROGRAM> ")


out = []
for args in CASES:
    # PYTHONDONTWRITEBYTECODE is propagated deliberately: a stale .pyc under scripts/lib once
    # executed while the .py on disk read correctly, and two runs of "the same" code disagreed.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, ENTRY] + args, capture_output=True, env=env)
    # errors="surrogateescape" so an undecodable byte survives into the JSON comparison as a
    # visible difference instead of raising here and destroying the whole run.
    out.append({
        "args": args,
        "status": result.returncode,
        "stdout": normalize(result.stdout.decode("utf-8", errors="surrogateescape")),
        "stderr": result.stderr.decode("utf-8", errors="surrogateescape"),
    })

json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
