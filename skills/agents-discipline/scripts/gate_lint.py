#!/usr/bin/env python3
"""gate_lint.py : audit whether a ledger is worth passing.

The checker decides whether gates were met. It does not ask whether the
gates were worth meeting. A gate reading "the entire feature works
perfectly" with `CHECK: echo ok` and `EXPECT: ok` passes the checker and
the parent re-verification, because the oracle is real, runs,
and returns what it promised. Authoring is the one step in the enforcement
hierarchy that is still pure prose discipline, and this lints it.

This never executes a CHECK. It reads the ledger and judges its oracles.

  python3 gate_lint.py [options] <ledger.md ...>
    --strict   treat warnings as failures
    --json     machine-readable findings

exit codes: 0 no strict failures, 1 strict findings, 2 usage or parse error.

Usable as a gate, so a ledger can require its own quality:
  CHECK: python3 scripts/gate_lint.py GATES.md
  EXPECT: LINT OK

Port of gate-lint.mjs. The JavaScript test suite is the oracle: it runs unchanged
against this implementation (`AD_RUNTIME=python node tests/lint-tests.mjs`), so any
divergence is a porting defect rather than a re-specified test.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from gates import parse_gates, read_stable_regular_file  # noqa: E402  # type: ignore[import-not-found]
from jsapi import force_utf8_streams  # noqa: E402  # type: ignore[import-not-found]

# BEFORE anything can print -- see the function's docstring. On this script the failure is not a
# degraded message but a CRASH: MEASURED under `PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C`, a
# non-ASCII EXPECT made the port die with UnicodeEncodeError partway through its report while the
# oracle printed the warning. stderr defaults to backslashreplace and merely degrades; stdout,
# which this script writes to, has no such fallback.
force_utf8_streams()

# The usage line is the ONE string that legitimately differs from the JS original: a
# program names itself in its own usage line. Every other output string below is a
# byte-for-byte transliteration of gate-lint.mjs's, including its own self-references.
HELP = """usage: gate_lint.py [--strict] [--json] <ledger.md ...>

Audit gate quality, not gate completion. Report lexical signs of fixed-output
oracles, weak expectations, manual measurements, and titles that name an
activity instead of an outcome. Never executes a CHECK.

exit codes: 0 no strict failures, 1 strict findings, 2 usage or parse error."""

KNOWN_OPTIONS = {"--strict", "--json", "--help", "-h"}
MAX_GATE_LEDGER_BYTES = 8 * 1024 * 1024
MAX_REPORTED_FINDINGS = 64
MAX_REPORT_BYTES = 256 * 1024
TRUNCATION_MARKER = "...[truncated]"


# Findings and CLI diagnostics can contain repository-controlled filenames,
# gate text, parser messages, or argv. Escape terminal controls at those data
# boundaries instead of rewriting complete output, so help text, structural
# newlines, and JSON indentation retain their intended formatting.
def _is_terminal_control(code):
    return (
        code <= 0x1F or (0x7F <= code <= 0x9F) or code == 0x061C or
        code in (0x200E, 0x200F) or (0x2028 <= code <= 0x202E) or
        (0x2066 <= code <= 0x2069)
    )


def terminal_safe(value, max_bytes=1024):
    pieces = []
    sizes = []
    total_bytes = 0
    truncated = False
    # Iterating a Python str already walks by Unicode code point, the same unit a JS
    # `for...of` over a string yields (it steps by code point, not UTF-16 code unit).
    for character in str(value):
        piece = character
        if _is_terminal_control(ord(character)):
            code = ord(character)
            if code <= 0xFF:
                piece = "\\x" + format(code, "02x")
            else:
                piece = "\\u" + format(code, "04x")
        elif 0xD800 <= ord(character) <= 0xDFFF:
            # A LONE SURROGATE, which is not a terminal control and is not UTF-8-encodable, so
            # the `size = len(piece.encode(...))` below RAISED. MEASURED, default locale, no
            # hostile environment needed -- `gate_lint.py <a filename containing byte 0xFF>`:
            #     oracle  gate-lint: cannot read bad<U+FFFD>arg: ENOENT: no such file...
            #     port    UnicodeEncodeError: ... '\udcff' ... surrogates not allowed
            # The port CRASHED while reporting that it could not read a file, which is the
            # loudest divergence this port can produce and it needed no unusual configuration:
            # CPython surrogateescape-decodes argv, so any command line carrying a byte that is
            # not valid UTF-8 reaches here.
            #
            # chr(0xFFFD) is what node substitutes, and it is byte-exact rather than merely
            # non-crashing: MEASURED, `Buffer.byteLength("\ud800")` is 3 and the bytes are
            # EF BF BD, so replacing the piece here gives the same emitted text AND the same
            # contribution to the truncation budget.
            piece = chr(0xFFFD)
        size = len(piece.encode("utf-8"))
        if total_bytes + size > max_bytes:
            truncated = True
            break
        pieces.append(piece)
        sizes.append(size)
        total_bytes += size
    if not truncated:
        return "".join(pieces)
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    while pieces and total_bytes + marker_bytes > max_bytes:
        pieces.pop()
        total_bytes -= sizes.pop()
    return "".join(pieces) + TRUNCATION_MARKER


args = sys.argv[1:]
if not args:
    print(HELP, file=sys.stderr)
    sys.exit(2)
# `--` makes every following token a filename, including literal files named
# `--help` and `-h`. Only scan the option prefix for the help flags.
positional_index = args.index("--") if "--" in args else -1
option_prefix = args if positional_index == -1 else args[:positional_index]
if "--help" in option_prefix or "-h" in option_prefix:
    print(HELP)
    sys.exit(0)
strict = False
as_json = False
positional = False
files = []
for arg in args:
    if not positional and arg == "--":
        positional = True
        continue
    if not positional and arg in KNOWN_OPTIONS:
        if arg == "--strict":
            strict = True
        elif arg == "--json":
            as_json = True
        continue
    if not positional and arg.startswith("-"):
        print("gate-lint: unknown option " + terminal_safe(arg, 512), file=sys.stderr)
        print("run gate-lint.mjs --help for usage", file=sys.stderr)
        sys.exit(2)
    files.append(arg)
if not files:
    print("gate-lint: name at least one ledger file", file=sys.stderr)
    sys.exit(2)

# This is deliberately advisory and whole-command only. Shell text beginning
# with `echo` can still chain a real verifier, and argv containing EXPECT says
# nothing about what the called program prints or whether it exits zero.
FIXED_OUTPUT_COMMAND = re.compile(
    r"^\s*(?:(?:echo|printf)(?:\s+[^&|;]*)?|true|:|exit\s+0)\s*\Z", re.IGNORECASE,
)
# Tokens that appear in failure output as readily as in success output.
WEAK_EXPECT = {
    "ok", "okay", "done", "pass", "passed", "success", "successful", "succeeded",
    "complete", "completed", "finished", "yes", "true", "0", "good", "fine", "working",
}
# Openings that name an activity rather than an outcome a stranger could judge. The
# pattern uses `\b` with no `\s`, so `re.ASCII` narrows only the ASCII-only-in-JS classes
# without touching a `\s` that JS leaves Unicode-wide (there is none here).
ACTIVITY_START = re.compile(
    r"^(work(ing)? on|improve|enhance|handle|support|ensure|make sure|try|attempt|"
    r"look (at|into)|investigate|consider|review|refactor|clean ?up|polish|update|"
    r"tidy|address|deal with|add support)\b",
    re.IGNORECASE | re.ASCII,
)

findings = []
error_count = 0
warning_count = 0
finding_count = 0


def add(file, level, gate, rule, message):
    global finding_count, error_count, warning_count
    finding_count += 1
    if level == "error":
        error_count += 1
    elif level == "warn":
        warning_count += 1
    if len(findings) >= MAX_REPORTED_FINDINGS:
        return
    findings.append({
        "file": terminal_safe(file, 512),
        "level": terminal_safe(level, 16),
        "gate": terminal_safe(gate, 128) if gate else None,
        "rule": terminal_safe(rule, 64),
        "message": terminal_safe(message, 1024),
    })


parse_failed = False

for file in files:
    try:
        # Explicit lint targets may intentionally live outside the current
        # working directory, so constrain file kind and size without a root.
        text = read_stable_regular_file(file, max_bytes=MAX_GATE_LEDGER_BYTES, label="gate ledger")
    except OSError as error:
        # Our own raised OSErrors carry only a message (no errno set), so str(error)
        # already IS that message; a real OS-raised error additionally has a strerror.
        message = error.strerror if (error.errno is not None and error.strerror) else str(error)
        print("gate-lint: cannot read " + terminal_safe(file, 512) + ": " + terminal_safe(message, 1024),
              file=sys.stderr)
        sys.exit(2)

    doc = parse_gates(text)
    if doc["errors"]:
        # A ledger the shared parser rejects cannot be judged on quality.
        parse_failed = True
        for error in doc["errors"]:
            add(file, "error", None, "parse", error)
        continue

    live = [gate for gate in doc["gates"] if gate["id"] not in doc["abandoned"]]
    runnable = [gate for gate in live if gate["check"]]

    for gate in live:
        gate_id, title, check, expect = gate["id"], gate["title"], gate["check"], gate["expect"]

        if check and FIXED_OUTPUT_COMMAND.match(check):
            add(file, "warn", gate_id, "tautological-check",
                'CHECK looks like a fixed-output command: "' + check +
                '"; use an oracle that observes the named outcome')

        if expect and expect.strip().lower() in WEAK_EXPECT:
            add(file, "warn", gate_id, "weak-expect",
                'EXPECT "' + expect + '" also appears in failure output; match a line only success can print')

        expectation = gate.get("expectation")
        if expectation and expectation.get("kind") == "regex" and expectation.get("pathLike"):
            add(file, "warn", gate_id, "path-read-as-regex",
                'EXPECT "' + expect +
                '" looks like a literal path but is read as a regular expression, so its dots are wildcards')

        if not check:
            add(file, "warn", gate_id, "manual-gate",
                "no CHECK, so this outcome is judged by hand and its evidence is only as good as the reader")
            # JS `\d` is ASCII-only regardless of the missing `u` flag; spell it as
            # `[0-9]` rather than reach for `re.ASCII` on a lone, otherwise-plain class.
            if re.search(r"[0-9]", title):
                add(file, "warn", gate_id, "unmeasured-number",
                    'title states a number that nothing measures: "' + title + '"')

        if ACTIVITY_START.match(title):
            add(file, "warn", gate_id, "activity-not-outcome",
                'names an activity, not an outcome a stranger could judge: "' + title + '"')

    if live and len(runnable) / len(live) < 0.5:
        add(file, "warn", None, "mostly-manual",
            str(len(runnable)) + "/" + str(len(live)) +
            " gates are runnable; a mostly manual ledger is prose with checkboxes")

failed = error_count > 0 or (strict and warning_count > 0)

if as_json:
    report = {
        "ok": not failed,
        "errors": error_count,
        "warnings": warning_count,
        "findings": list(findings),
        "truncated": finding_count > len(findings),
        "omittedFindings": finding_count - len(findings),
    }
    output = json.dumps(report, indent=2)
    while len(output.encode("utf-8")) > MAX_REPORT_BYTES and report["findings"]:
        report["findings"].pop()
        report["truncated"] = True
        report["omittedFindings"] = finding_count - len(report["findings"])
        output = json.dumps(report, indent=2)
    print(output)
else:
    last_file = None
    for finding in findings:
        if finding["file"] != last_file:
            print(finding["file"])
            last_file = finding["file"]
        label = "ERROR" if finding["level"] == "error" else "WARN "
        who = (finding["gate"] + ": ") if finding["gate"] else ""
        print("  " + label + " " + who + finding["message"] + "  [" + finding["rule"] + "]")
    omitted = finding_count - len(findings)
    if omitted:
        print("... [report truncated: " + str(omitted) + " finding(s) omitted]")
    if not failed:
        print("LINT OK (" + str(warning_count) + " warning(s))" if warning_count else "LINT OK")
    else:
        print("LINT FINDINGS: " + str(error_count) + " error(s), " + str(warning_count) + " warning(s)")

# Node comments here that a forced process.exit() can truncate an asynchronous pipe;
# Python's stdout is flushed on normal interpreter exit, so plain sys.exit() suffices.
sys.exit(2 if parse_failed else (1 if failed else 0))
