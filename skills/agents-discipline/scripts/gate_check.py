#!/usr/bin/env python3
"""gate_check.py : execute gate oracles, update evidence, coordinate scopes, manage leases.

Port of gate-check.mjs. The JavaScript suite is the ORACLE: it is held FIXED and only this
implementation varies, so any divergence is a porting defect and never a re-specified test.

PARTIAL PORT -- THE ARGUMENT FRONT END, TARGET DISCOVERY, LEDGER-EXISTENCE CHECKS, AND THE
--list-scopes/--log/--claim/--release ACTIONS, gate-check.mjs:27-295.

The boundary now ends exactly where the `--claim`/`--release` action block's closing brace
ends (:295) and `let ledgers = target.files.map(loadLedger)` begins the un-ported default run
mode (:297), because :295 is the last point every REACHABLE branch of this file resolves to a
real exit on its own -- the default run mode (loading every ledger, resolving a shell,
executing CHECKs) is the only remaining branch, and it needs a resolved command shell and PATH
inspection this port does not yet have. Every behaviour up to :295 can still be driven
black-box by running the program and reading its exit code, stdout, and stderr, given only a
filesystem fixture (an `.agents-discipline/` tree, a lock directory, and/or ledger files) and
no shell or approval storage at all.

Anything past that boundary exits PORT_INCOMPLETE_EXIT (90) with a message naming the stop.
That is deliberate and load-bearing: a partial port that fell through to a silent success
would let a differential PASS on a vector it never actually implemented, which is this
project's recurring defect (an assertion satisfied by something other than the property it
names). 90 collides with no oracle exit code, so a vector that runs off the end of the port is
always visible as a divergence rather than as agreement.

That range claim is ENUMERATED, not induced from the codes a few scenarios happened to produce
-- a bound asserted from observation is exactly the shape this task has had to retract twice.
Every `process.exit(...)` in gate-check.mjs takes a LITERAL, at :118 :155 :186 :209 :212 :246
:263 :266 :279 :291 :294 :859 :904 :932 :936 :950, and the set of those literals is {0,1,2,3};
:856 sets `process.exitCode = 2`, which :859 turns into `exit(2)`. No site in that file computes
a code at runtime. Node's own uncaught-throw exit is 1, already in the set.

THE DOMAIN OF THAT ENUMERATION IS THE RUNNING PROGRAM, not one file -- a `process.exit()` inside
an IMPORTED module is the importer's exit too, so bounding only gate-check.mjs would bound a
smaller domain than the sentence covers. It matters here: `lib/check-supervisor.mjs` has BOTH a
`process.exit(127)` and a computed `process.exitCode = code`. It is not imported. gate-check's
imports are exactly `lib/gates.mjs`, `lib/process-tree.mjs`, `lib/dispatch.mjs`, and none of the
three exits the process at all; the supervisor is reached only by `spawn()` at :674, as a
separate process whose status is read as DATA (:607 `settle(exitCode, signal)`, :634, :685, :782
`"exit=" + result.exitCode`) and never re-raised, and `lib/regex-worker.mjs` only by `new
Worker()` at :555, a thread with no exit site of its own.

NONE OF THAT IS PROSE ANY MORE. python-lib-checks derives the literal set, asserts no computed
exit, walks the import CLOSURE to fixpoint (not one level -- a module gate-check imports may
itself import something that exits, and that exit is still gate-check's), and asserts no module
in that closure exits at all -- with vacuity controls on both extractions, because an empty set
intersects nothing and exits nowhere, so a regex that matched none of its targets would pass
every one of those rows by not looking. The line numbers above are a reader's shortcut; the
check is what holds them true. (Earlier drafts cited :634/:685/:782 from a planning document
rather than from the file. They were verified directly before being written here.)

SELF-NAMING follows the convention gate_lint.py established: the HELP usage line names this
file, and every other string -- including the "gate-check: " prefix and the "run gate-check.mjs
--help for usage" line -- is a byte-for-byte transliteration of the oracle's, self-references
included. The convention is deliberately literal; deviating from it would create a new class
of differential divergence in every ported script at once.
"""

import errno as _errno
import json
import math
import os
import re
import stat as _stat
import sys
import typing

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from gates import (  # noqa: E402  # type: ignore[import-not-found]
    AGENTS_DISCIPLINE_DIR, claim_leases, js_resolve, list_scopes, parse_gates,
    read_stable_regular_file, release_leases, resolve_target, append_status,
    validate_scope_id,
)
from jsapi import js_to_number  # noqa: E402  # type: ignore[import-not-found]

# Only the first line differs from the oracle's HELP; a program names itself in its own usage
# line. Every other line is transliterated exactly.
HELP = """usage: gate_check.py [options] [file ...]

run modes:
  (default)             run unmet runnable gates and update their ledgers
  --status              report only; never execute, approve, or write
  --reverify            re-run every runnable gate and demote stale failures
  --approve             approve each exact pending oracle, then run it
  --jobs N              rolling concurrency, integer 1..64 (default 1)
  --timeout S           per-check timeout, integer seconds 1..86400 (default 120)
  --shell PATH          command shell (AGENTS_DISCIPLINE_SHELL, then platform default)
  --cwd DIR             default CHECK directory (explicit: file dir; discovered: --root)

pipeline actions:
  --claim --scope ID [--leaf NAME]   atomically claim the leaf's OWNS paths
  --release --scope ID [--leaf NAME] release serialized ownership leases
  --log TEXT --scope ID              append one status line
  --list-scopes                      list .agents-discipline pipelines

targeting:
  --scope ID             use .agents-discipline/ID (or AGENTS_DISCIPLINE_SCOPE)
  --root DIR             repository/pipeline root (default current directory)
  file ...               explicit regular ledger files; all are honored

CHECK execution requires prior approval keyed to the exact CHECK, EXPECT,
resolved CWD, resolved shell, timeout, output/regex limits, platform, and PATH.
Approvals live outside the repository under ~/.agents-discipline/approved by default.

exit codes: 0 all met/action succeeded; 1 unmet; 2 usage/parse/infrastructure;
            3 lease conflict."""

FLAG_OPTIONS = {
    "--status", "--reverify", "--approve", "--claim", "--release",
    "--list-scopes", "--help", "-h",
}
VALUE_OPTIONS = {
    "--scope", "--leaf", "--timeout", "--jobs", "--cwd", "--root",
    "--log", "--shell",
}
DEFAULT_TIMEOUT_SECONDS = 120
# gate-check.mjs:67, a local constant the oracle never exports from lib/gates.mjs -- kept
# local here too rather than promoted into gates.py, so this file's constants mirror exactly
# what the oracle declares at its own top level.
MAX_GATE_LEDGER_BYTES = 8 * 1024 * 1024

# Exit code for "this vector reached un-ported territory". NOT an oracle exit code (0/1/2/3),
# so it can never be mistaken for agreement -- see the module docstring.
PORT_INCOMPLETE_EXIT = 90

# gate-check.mjs:78, expressed as RANGES and assembled at import time.
#
# The pattern string is BUILT from ASCII format specifiers so that no non-printing character
# exists in this source file at all. That is not fastidiousness: the first version of this very
# line was authored as the literal character class, and the writing layer put a raw NUL, a raw
# U+2028 and raw C0 controls straight into the file -- the THIRD time this project has been bitten
# by it (the earlier two are recorded in digest-drive.mjs's header). Worse, the comment sitting
# directly above it claimed the line was written as escapes, so the file documented itself
# falsely and a reader had no way to see the corruption in a diff. Construction removes the
# failure mode rather than relying on the author to keep getting it right.
_UNSAFE_TERMINAL_RANGES = (
    (0x0000, 0x001F), (0x007F, 0x009F), (0x061C, 0x061C),
    (0x200E, 0x200F), (0x2028, 0x202E), (0x2066, 0x2069),
)
UNSAFE_TERMINAL_RE = re.compile(
    "[" + "".join("\\u%04x-\\u%04x" % pair for pair in _UNSAFE_TERMINAL_RANGES) + "]")


def terminal_safe(value):
    """gate-check.mjs:79. Repository-controlled text must not rewrite terminal history."""
    return UNSAFE_TERMINAL_RE.sub(" ", str(value))


def log(message):
    """console.log after the :80-83 wrapper, which routes every argument through terminalSafe."""
    sys.stdout.write(terminal_safe(message) + "\n")


def error(message):
    """console.error after the same wrapper."""
    sys.stderr.write(terminal_safe(message) + "\n")


def js_json_stringify(value):
    """JSON.stringify for the values that reach a usage message: always an argv string.

    `ensure_ascii=False` is the project-wide standard (gates.py:964, digest_drive.py:111):
    JSON.stringify never escapes non-ASCII, json.dumps escapes it by default.

    KNOWN LIMIT, stated because it is measured rather than hypothetical: on a LONE SURROGATE the
    two disagree -- JSON.stringify emits the ASCII escape backslash-u-d800, json.dumps emits the
    raw code unit. It is left alone deliberately. A lone surrogate can only reach argv through
    invalid UTF-8, and there the runtimes have ALREADY diverged before any quoting happens: node
    decodes an invalid byte to U+FFFD, CPython to a U+DCxx surrogate. A bespoke quoter would
    change which wrong answer is printed without making the two agree, so it would buy nothing
    and hide the real cause. The differential carries a row for it instead.
    """
    return json.dumps(value, ensure_ascii=False)


def parse_args(argv):
    """gate-check.mjs:85-113. Returns {"error": str} or {"options": dict, "files": list}."""
    options = {}
    files = []
    positional = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            # Checked BEFORE the `positional` guards, exactly as the oracle does, so a SECOND
            # `--` is also swallowed rather than becoming a file argument.
            positional = True
            index += 1
            continue
        if not positional and arg in FLAG_OPTIONS:
            # `^-+` strips EVERY leading dash, so `-h` keys as "h" and `--help` as "help".
            # They are therefore distinct keys and `--help -h` is not a duplicate.
            key = re.sub(r"^-+", "", arg)
            if key in options:
                return {"error": "duplicate option " + arg}
            options[key] = True
            index += 1
            continue
        if not positional and arg.startswith("--"):
            equals = arg.find("=")
            name = arg if equals == -1 else arg[:equals]
            if name not in VALUE_OPTIONS:
                return {"error": "unknown option " + name}
            key = name[2:]
            if key in options:
                return {"error": "duplicate option " + name}
            if equals == -1:
                # `argv[++index]` advances the cursor even when it runs off the end, where JS
                # yields `undefined`; the separated bump reproduces both halves.
                index += 1
                value = argv[index] if index < len(argv) else None
            else:
                value = arg[equals + 1:]
            if value is None or value == "":
                return {"error": name + " needs a value"}
            options[key] = value
            index += 1
            continue
        if not positional and arg.startswith("-"):
            return {"error": "unknown option " + arg}
        files.append(arg)
        index += 1
    return {"options": options, "files": files}


def fail_usage(message) -> typing.NoReturn:
    """gate-check.mjs:115-119.

    NoReturn, same reason as dispatch.py's `_fail`: several callers below (load_ledger's
    OSError arm, the --claim `selected` lookup) assign the return value of a function that
    calls fail_usage on one branch and returns a real value on the other. Without this
    annotation a checker sees every such call as possibly yielding None and flags the
    downstream `.get`/subscript as an optional-access error, even though fail_usage never
    falls through at runtime.
    """
    error("gate-check: " + message)
    error("run gate-check.mjs --help for usage")
    sys.exit(2)


def as_directory(path, label):
    """gate-check.mjs:121-128.

    `statSync` FOLLOWS symlinks, so os.stat (not os.lstat) is the faithful call.

    The non-ENOENT arm prints node's `error.message`, whose format is
    `CODE: lowercased description, stat 'path'` -- measured against node, not assumed. CPython's
    OSError stringifies as `[Errno 20] Not a directory: '/p'`, which shares almost no bytes with
    it, so the message is RECONSTRUCTED rather than passed through.
    """
    try:
        stat_result = os.stat(path)
    except OSError as exc:
        # OSError.errno is Optional, so the table lookup needs the guard even though every
        # errno reaching here in practice is a real one. "UNKNOWN" is the same fallback the
        # lookup already used, so the guard changes no reachable behaviour.
        name = _errno.errorcode.get(exc.errno, "UNKNOWN") if exc.errno is not None else "UNKNOWN"
        if name == "ENOENT":
            fail_usage(label + " does not exist: " + path)
        description = (exc.strerror or "").lower()
        fail_usage("cannot inspect " + label + " " + path + ": "
                   + name + ": " + description + ", stat '" + path + "'")
        return
    if not _stat.S_ISDIR(stat_result.st_mode):
        fail_usage(label + " is not a directory: " + path)


def _validated_integer(value, flag, low, high):
    """The shared body of timeoutValue/jobCount (:130-146), which differ only in flag and range.

    `Number(value)` is js_to_number, NOT float(): the oracle accepts `0x10` as 16 and `1e3` as
    1000, and rejects `1_000` as NaN, where float() crashes on the first and accepts the last.
    `Number.isInteger` is true for a float with no fractional part, so `--timeout 5.0` is
    ACCEPTED by the oracle and must be here too.
    """
    number = js_to_number(value)
    if not math.isfinite(number) or not float(number).is_integer() \
            or number < low or number > high:
        fail_usage(flag + " needs an integer from " + str(low) + " through " + str(high)
                   + ", got " + js_json_stringify(value))
    return int(number)


def timeout_value(value):
    """gate-check.mjs:130-137."""
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    return _validated_integer(value, "--timeout", 1, 86400)


def job_count(value):
    """gate-check.mjs:139-146."""
    if value is None:
        return 1
    return _validated_integer(value, "--jobs", 1, 64)


def main(argv):
    parsed = parse_args(argv)
    if "error" in parsed:
        fail_usage(parsed["error"])
    opt = parsed["options"]
    file_args = parsed["files"]

    if opt.get("help") or opt.get("h"):
        # HELP is a fixed local constant, so preserve its intentional layout instead of routing
        # it through the untrusted-value terminal sanitizer (:152-154 says exactly this).
        sys.stdout.write(HELP + "\n")
        sys.exit(0)

    action_names = []
    for key in ("claim", "release", "list-scopes"):
        if opt.get(key):
            action_names.append("--" + key)
    for key in ("log",):
        if opt.get(key) is not None:
            action_names.append("--" + key)
    if len(action_names) > 1:
        fail_usage("pipeline actions are mutually exclusive: " + ", ".join(action_names))
    action = action_names[0] if action_names else None

    if opt.get("status") and opt.get("reverify"):
        fail_usage("--status and --reverify are mutually exclusive")
    if opt.get("status") and opt.get("approve"):
        fail_usage("--status never approves commands; remove --approve")
    if action and (opt.get("status") or opt.get("reverify") or opt.get("approve")):
        fail_usage(action + " cannot be combined with a run mode")
    if action and file_args:
        fail_usage(action + " cannot be combined with explicit files")
    if file_args and opt.get("scope"):
        fail_usage("explicit files and --scope are mutually exclusive")
    if opt.get("leaf") and not opt.get("claim") and not opt.get("release"):
        fail_usage("--leaf is only valid with --claim or --release")
    if (opt.get("timeout") or opt.get("jobs") or opt.get("shell") or opt.get("cwd")) \
            and (action or opt.get("status")):
        fail_usage("--timeout, --jobs, --shell, and --cwd are execution options only")

    root = js_resolve(opt.get("root") or os.getcwd())
    as_directory(root, "--root")
    timeout_value(opt.get("timeout"))
    job_count(opt.get("jobs"))
    default_cwd = js_resolve(root, opt.get("cwd") or ".")
    if not action and not opt.get("status"):
        as_directory(default_cwd, "--cwd")

    if action == "--list-scopes":
        scopes = list_scopes(root)
        if scopes:
            for scope_name in scopes:
                log(scope_name)
        else:
            log("(no pipelines under " + AGENTS_DISCIPLINE_DIR + "/)")
        sys.exit(0)

    if opt.get("scope"):
        scope_error = validate_scope_id(opt["scope"])
        if scope_error:
            fail_usage(scope_error)

    target = resolve_target({"root": root, "scope": opt.get("scope"), "files": file_args})
    # A deleted/crashed pipeline can leave coordination leases behind after its ledger
    # directory is gone. An explicit, validated release target must remain usable for that
    # recovery path; claims still require a live exact ledger.
    if target.get("error") and action == "--release" and opt.get("scope"):
        target = {"mode": "scope", "scope": opt["scope"], "files": []}
    elif target.get("error"):
        fail_usage(target["error"])
    scope = target.get("scope")

    if action == "--log":
        if not scope:
            fail_usage("--log needs --scope ID or exactly one discoverable pipeline")
        if not str(opt.get("log")).strip():
            fail_usage("--log needs non-blank text")
        try:
            path = append_status(root, scope, opt["log"])
            log("appended to " + path)
            sys.exit(0)
        except OSError as exc:
            error("gate-check: cannot append status: " + str(exc))
            sys.exit(2)

    if target.get("discoveryErrors") and action != "--release":
        fail_usage("; ".join(target["discoveryErrors"]))

    if not target["files"] and action != "--release":
        fail_usage("no gate files found (looked for " + AGENTS_DISCIPLINE_DIR
                   + "/<scope>/, then GATES.md and gates/*.md under " + root + ")")

    def read_ledger_file(file):
        # `...(mode === "explicit" ? {} : { root })`: root is passed ONLY when the ledger came
        # from discovery, never for an explicit file argument -- the oracle lets an explicit
        # file live anywhere, discovery results are pinned to have come from under root.
        return read_stable_regular_file(
            file, max_bytes=MAX_GATE_LEDGER_BYTES, label="gate ledger",
            root=None if target["mode"] == "explicit" else root)

    for file in target["files"]:
        try:
            read_ledger_file(file)
        except OSError as exc:
            if exc.errno == _errno.ENOENT:
                fail_usage("no such gate file: " + file)
            fail_usage("cannot inspect gate file " + file + ": " + str(exc))

    def load_ledger(file):
        try:
            text = read_ledger_file(file)
        except OSError as exc:
            fail_usage("cannot read " + file + ": " + str(exc))
        doc = parse_gates(text)
        for warning in doc["warnings"]:
            error("gate-check: " + file + ": warning: " + warning)
        if doc["errors"]:
            for doc_error in doc["errors"]:
                error("gate-check: " + file + ": " + doc_error)
            sys.exit(2)
        return {"file": file, "doc": doc}

    if action == "--claim" or action == "--release":
        if not scope:
            fail_usage(action + " needs --scope ID or exactly one discoverable pipeline")
        stems = [re.sub(r"\.md\Z", "", os.path.basename(f), flags=re.IGNORECASE)
                 for f in target["files"]]
        if opt.get("leaf"):
            leaf_error = validate_scope_id(opt["leaf"], "leaf")
            if leaf_error:
                fail_usage(leaf_error)
            if stems and opt["leaf"] not in stems:
                fail_usage("unknown --leaf " + opt["leaf"] + " (have: " + ", ".join(stems) + ")")
        if action == "--release":
            try:
                count = release_leases(root, {"scope": scope, "leaf": opt.get("leaf") or None})
                log("released " + str(count) + " lease(s) for " + scope
                    + (("/" + opt["leaf"]) if opt.get("leaf") else ""))
                sys.exit(0)
            except OSError as exc:
                error("gate-check: cannot release leases: " + str(exc))
                sys.exit(2)

        leaf = opt.get("leaf") or (stems[0] if len(stems) == 1 else None)
        if not leaf:
            fail_usage("--claim needs --leaf NAME when a scope has several gate files")
        selected_file = next(
            (f for f in target["files"]
             if re.sub(r"\.md\Z", "", os.path.basename(f), flags=re.IGNORECASE) == leaf),
            None)
        selected = load_ledger(selected_file)
        if not selected["doc"]["owns"]:
            fail_usage(os.path.basename(selected["file"]) + " declares no OWNS paths")
        try:
            result = claim_leases(
                root, {"scope": scope, "leaf": leaf, "globs": selected["doc"]["owns"]})
        except OSError as exc:
            error("gate-check: cannot claim leases: " + str(exc))
            sys.exit(2)
        if not result.get("ok"):
            if result.get("error"):
                fail_usage(result["error"])
            for conflict in result["conflicts"]:
                if conflict.get("identity"):
                    log("CONFLICT " + conflict["with"]
                        + " already holds a live lease; release it before claiming again")
                else:
                    log("CONFLICT " + conflict["glob"] + " overlaps " + conflict["theirGlob"]
                        + " held by " + conflict["with"])
            log("CLAIM REFUSED (" + str(len(result["conflicts"])) + " conflict(s))")
            sys.exit(3)
        log("CLAIMED " + str(len(result["globs"])) + " path(s) for " + scope + "/" + leaf
            + ": " + ", ".join(result["globs"]))
        sys.exit(0)

    # THE PORT STOPS HERE -- gate-check.mjs:297 (`ledgers = target.files.map(loadLedger)`,
    # the entry into the default run-mode) begins the CHECK-execution shell/PATH plumbing
    # (executableCandidates/resolveShell at :299) and everything after it. The boundary sits
    # BEFORE that line rather than after it -- even though load_ledger above is fully ported
    # and used by --claim -- so the sentinel lands exactly at the last action this file's own
    # branches fully resolve (--list-scopes/--log/--claim/--release all reach a real exit
    # above), instead of one line into a batch load whose only consumer is the un-ported run
    # loop. Loud on purpose: see the module docstring on why a silent fall-through would let a
    # differential agree on a vector nothing implemented.
    error("gate_check.py: PORT INCOMPLETE -- argument handling, target/ledger resolution, and "
          "the --list-scopes/--log/--claim/--release actions end at gate-check.mjs:295; the "
          "default run mode (loading every ledger, resolving a shell, executing CHECKs) is "
          "not ported yet")
    sys.exit(PORT_INCOMPLETE_EXIT)


if __name__ == "__main__":
    main(sys.argv[1:])
