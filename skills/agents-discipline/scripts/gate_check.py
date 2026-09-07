#!/usr/bin/env python3
"""gate_check.py : execute gate oracles, update evidence, coordinate scopes, manage leases.

Port of gate-check.mjs. The JavaScript suite is the ORACLE: it is held FIXED and only this
implementation varies, so any divergence is a porting defect and never a re-specified test.

PARTIAL PORT -- THE ARGUMENT FRONT END, TARGET DISCOVERY, LEDGER-EXISTENCE CHECKS, THE
--list-scopes/--log/--claim/--release ACTIONS, AND (NEW) THE DEFAULT RUN MODE'S LEDGER
LOADING, GATE SELECTION, AND FULL APPROVAL-CLASSIFICATION LOOP, gate-check.mjs:27-775.

The boundary sat at :295 (the `--claim`/`--release` block's closing brace) until this pass
moved it forward. It CANNOT sit at :297 (`ledgers = target.files.map(loadLedger)`, where the
default run mode begins) plus a "no approval" carve-out, because gate-check.mjs:753-768 shows
approval STATE feeding the verdict from *inside* the classification loop: an unapproved task
becomes `notRun` right there via `if (!opt.approve) { ...; notRun.push(task); continue; }`,
and an approved-and-just-recorded one becomes `runnable`. Approval is not a later phase that
can be skipped while still reporting a correct MET/UNMET/NOT-RUN verdict -- it is decided
inline, so porting "up to the verdict" without porting approval would have reproduced this
project's recurring defect one more time: a port that agrees with the oracle on every
unapproved fixture (which is what the differential in tests/gate-args-diff.sh exercises) and
silently diverges the moment `--approve` is passed (which tests/run-tests.mjs:45-47 injects
into nearly every case) -- exactly the shape of bug a differential run without that flag would
never catch.

The boundary now ends exactly at gate-check.mjs:775, the last line of the RUN-print loop, one
statement before `results = opt.status ? [] : await runRolling(runnable, jobs)` at :776 --
the single call that spawns lib/check-supervisor.mjs (via lib/dispatch.mjs's sibling,
lib/process-tree.mjs, and a `node:worker_threads` Worker pool for regex matching). That is the
ONLY process/thread-spawning site reachable from this default run mode, so it is where "no
spawn, no Worker, no process-tree teardown, no timeout machinery" has to draw the line. Ported
in this pass: ledger loading (:297), the `pending` gate-selection loop with real CWD
resolution and validation (:716-739), shell resolution and the full approval-token machinery
-- `resolveShell`/`oracle`/`approvalOracleSignature`/`approvalPath`/`validatedApprovalDir`/
`assertPrivateApprovalEntry`/`assertApprovalDirUnchanged`/`readApprovalFile`/`approvalExists`/
`recordApproval`/`printOracle` (:299-519) -- and the classification loop that turns `pending`
into `runnable`/`notRun` while genuinely reading and writing the on-disk approval store
(:741-775). These are LOCAL to gate-check.mjs (0 matches for any of them in lib/gates.mjs), so
they are ported HERE rather than assumed to already exist in lib/gates.py.

PORT_INCOMPLETE_EXIT (90) now fires unconditionally the moment this file reaches the line
that would call `runRolling` -- not only when `runnable` is non-empty. A tempting shortcut was
rejected here: when `runnable` and `notRun` are BOTH empty (nothing needed running -- either
`--status`, or every gate was already met and `--reverify` was not given), `results` really is
always `[]`, and the oracle's tail (the staleResults ledger-rewrite loop, the reloaded-ledger
MET/UNMET tally, dispatchStatus aggregation, the extraUnmet bookkeeping across `--reverify` and
stale results) is complex enough that reproducing it from the empty-`results` case alone would
have been exactly the kind of "looks complete, is not measured" simplification this project's
own history keeps warning about. So the sentinel fires every time, and the next porting pass
inherits a clean, single, textually-obvious boundary instead of a conditional one that would
need re-auditing later. Anything past that boundary exits PORT_INCOMPLETE_EXIT (90) with a
message naming the stop.

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
import time
import typing

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from gates import (  # noqa: E402  # type: ignore[import-not-found]
    AGENTS_DISCIPLINE_DIR, MAX_CHECK_OUTPUT_BYTES, claim_leases, gate_definition_digest,
    gate_state, js_dirname, js_resolve, list_scopes, parse_gates, qualify,
    read_stable_regular_file, release_leases, resolve_target, same_file_identity, sha256,
    sleep, stat_current_named_file, append_status, validate_scope_id, write_atomic,
    # Underscore-prefixed: module-private in gates.py, but already the CORRECT port of the
    # exact primitives gate-check.mjs's own approval machinery needs (path.join,
    # pathIsInside, and JSON.stringify-as-text). REUSE them rather than re-derive a second,
    # driftable copy -- the standing rule for every helper this project has already ported.
    _js_join, _js_json_text, _LONE_SURROGATE_RE, _path_is_inside,
)
from jsapi import js_json_object, js_length, js_slice, js_to_number  # noqa: E402  # type: ignore[import-not-found]
from dispatch import _iso_now  # noqa: E402  # type: ignore[import-not-found]

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
# gate-check.mjs:65-70, the same local-constant convention as MAX_GATE_LEDGER_BYTES above.
MAX_OUTPUT_BYTES = MAX_CHECK_OUTPUT_BYTES
MAX_APPROVAL_BYTES = 256 * 1024
REGEX_TIMEOUT_MS = 250
REGEX_STARTUP_TIMEOUT_MS = 5000
MAX_REGEX_WORKERS = 4

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
    timeout_seconds = timeout_value(opt.get("timeout"))
    jobs = job_count(opt.get("jobs"))
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

    # Default run mode, gate-check.mjs:297 onward. See the module docstring for why the
    # boundary is HERE (gate-check.mjs:775, one statement before the runRolling spawn) and not
    # at :297 with an "approval skipped" carve-out.

    def resolved_gate_cwd(gate, file):
        """gate-check.mjs:332-338."""
        base = default_cwd if opt.get("cwd") is not None else (
            js_dirname(js_resolve(file)) if target["mode"] == "explicit" else root)
        return js_resolve(base, gate["cwd"]) if gate.get("cwd") else base

    ledgers = [load_ledger(f) for f in target["files"]]

    # gate-check.mjs:322-330. Guarded exactly like the oracle: `--status` never resolves a
    # shell, reads PATH, or touches approval storage, so a garbage AGENTS_DISCIPLINE_SHELL
    # must not fail a `--status` run.
    if opt.get("status"):
        shell = None
        path_value = None
        path_transcript = None
    else:
        def executable_candidates(name):
            """gate-check.mjs:299-304."""
            if sys.platform != "win32":
                return [name]
            if re.search(r"\.[A-Za-z0-9]+\Z", name):
                return [name]
            extensions = [ext for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if ext]
            return [name] + [name + ext.lower() for ext in extensions] \
                + [name + ext.upper() for ext in extensions]

        def resolve_shell(raw):
            """gate-check.mjs:306-320."""
            requested = raw or os.environ.get("AGENTS_DISCIPLINE_SHELL") or (
                (os.environ.get("ComSpec") or "cmd.exe") if sys.platform == "win32" else "/bin/sh")
            contains_separator = "/" in requested or "\\" in requested or os.path.isabs(requested)
            candidates = []
            if contains_separator:
                candidates.append(js_resolve(os.getcwd(), requested))
            else:
                path_delimiter = ";" if sys.platform == "win32" else ":"
                for directory in [d for d in str(os.environ.get("PATH") or "").split(path_delimiter) if d]:
                    for name in executable_candidates(requested):
                        candidates.append(_js_join(directory, name))
            for candidate in candidates:
                try:
                    if _stat.S_ISREG(os.stat(candidate).st_mode):
                        return candidate
                except OSError:
                    continue  # keep looking
            fail_usage("cannot resolve command shell " + js_json_stringify(requested) + " from PATH")

        shell = resolve_shell(opt.get("shell"))
        path_value = str(os.environ.get("PATH") or "")
        path_transcript = (re.sub(r"[\r\n]", " ", js_slice(path_value, 0, 800))
                            + ("..." if js_length(path_value) > 800 else ""))

    def oracle(file, gate):
        """gate-check.mjs:340-356. `sys.platform` matches node's `process.platform` for every
        value this project runs on (darwin/linux/win32 -- the three the oracle itself is only
        MEASURED against, per stat_current_named_file's own note in lib/gates.py)."""
        return {
            "schema": 1, "check": gate["check"], "expect": gate["expect"],
            "cwd": resolved_gate_cwd(gate, file), "shell": shell,
            "timeoutMs": timeout_seconds * 1000, "maxOutputBytes": MAX_OUTPUT_BYTES,
            "regexTimeoutMs": REGEX_TIMEOUT_MS, "regexStartupTimeoutMs": REGEX_STARTUP_TIMEOUT_MS,
            "maxRegexWorkers": MAX_REGEX_WORKERS, "platform": sys.platform, "path": path_value,
        }

    def approval_oracle_signature(file, gate):
        """gate-check.mjs:358-360."""
        return sha256(_js_json_text(js_json_object(oracle(file, gate))))

    if opt.get("status"):
        approval_dir = None
        canonical_root = None
    else:
        # gate-check.mjs:367-370.
        approval_dir = js_resolve(os.environ.get("AGENTS_DISCIPLINE_APPROVAL_DIR")
                                   or _js_join(os.path.expanduser("~"), ".agents-discipline", "approved"))
        canonical_root = os.path.realpath(root)
        if _path_is_inside(root, approval_dir):
            fail_usage("AGENTS_DISCIPLINE_APPROVAL_DIR must be outside the repository root")

    def approval_path(file, gate, directory=None):
        """gate-check.mjs:372-375."""
        identity = js_resolve(file) + "\0" + gate["id"] + "\0" + approval_oracle_signature(file, gate)
        return _js_join(approval_dir if directory is None else directory, sha256(identity) + ".json")

    def assert_private_approval_entry(path, info, kind):
        """gate-check.mjs:377-392."""
        is_symlink = _stat.S_ISLNK(info.st_mode)
        is_right_type = _stat.S_ISDIR(info.st_mode) if kind == "directory" else _stat.S_ISREG(info.st_mode)
        if is_symlink or not is_right_type:
            raise RuntimeError(str(path) + " must be a real " + kind)
        # os.geteuid()/os.getuid() are POSIX-only, matching node's own
        # `typeof process.geteuid === "function"` guard (absent on win32).
        uid = os.geteuid() if hasattr(os, "geteuid") else (os.getuid() if hasattr(os, "getuid") else None)
        if uid is not None and info.st_uid != uid:
            raise RuntimeError(str(path) + " must be owned by the current user")
        extra_permissions = info.st_mode & 0o077
        if sys.platform != "win32" and extra_permissions != 0:
            raise RuntimeError(str(path) + " must not grant group or other permissions")

    def validated_approval_dir(create=False):
        """gate-check.mjs:394-406.

        `approval_dir` is None under --status (the :324-329 ternaries), and a type checker
        cannot see that this function is then unreachable: --status makes the gate loop
        `continue` before anything is pushed to `pending`, so the loop that calls this never
        has a task. cast() rather than an assert -- an assert would add a runtime check the
        oracle does not have, and this file's job is to match the oracle, not improve on it.
        """
        directory = typing.cast(str, approval_dir)
        if create:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        elif not os.path.exists(directory):
            return None
        info = os.lstat(directory)
        assert_private_approval_entry(directory, info, "directory")
        canonical = os.path.realpath(directory)
        if _path_is_inside(canonical_root, canonical):
            raise RuntimeError("approval directory resolves inside the repository root: " + canonical)
        canonical_info = os.lstat(canonical)
        assert_private_approval_entry(canonical, canonical_info, "directory")
        return {"path": canonical, "dev": canonical_info.st_dev, "ino": canonical_info.st_ino}

    def assert_approval_dir_unchanged(store):
        """gate-check.mjs:408-414."""
        current = os.lstat(store["path"])
        assert_private_approval_entry(store["path"], current, "directory")
        if not same_file_identity(current, store):
            raise RuntimeError("approval directory changed during use: " + store["path"])

    def read_approval_file(path):
        """gate-check.mjs:416-447."""
        fd = None
        try:
            no_follow = 0 if sys.platform == "win32" else getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | no_follow)
            opened = os.fstat(fd)
            named = stat_current_named_file(
                path, {"maxBytes": MAX_APPROVAL_BYTES, "label": "approval record"})
            assert_private_approval_entry(path, opened, "file")
            if opened.st_size > MAX_APPROVAL_BYTES:
                raise RuntimeError("approval record exceeds " + str(MAX_APPROVAL_BYTES)
                                   + " bytes: " + path)
            assert_private_approval_entry(path, named, "file")
            if opened.st_nlink != 1 or not same_file_identity(named, opened):
                raise RuntimeError("refusing linked or replaced approval record " + path)
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            # errors="replace", matching readFileSync's "utf8" substitution behaviour --
            # same reasoning as read_stable_regular_file in lib/gates.py.
            text = b"".join(chunks).decode("utf-8", errors="replace")
            after = stat_current_named_file(
                path, {"maxBytes": MAX_APPROVAL_BYTES, "label": "approval record"})
            assert_private_approval_entry(path, after, "file")
            if not same_file_identity(after, opened):
                raise RuntimeError("approval record changed while it was read: " + path)
            return text
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def approval_exists(file, gate):
        """gate-check.mjs:449-465."""
        store = validated_approval_dir()
        if not store:
            return False
        path = approval_path(file, gate, store["path"])
        try:
            text = read_approval_file(path)
        except OSError as exc:
            if exc.errno == _errno.ENOENT:
                return False
            raise
        try:
            value = json.loads(text)
        except ValueError:
            return False
        assert_approval_dir_unchanged(store)
        return (isinstance(value, dict) and value.get("file") == js_resolve(file)
                and value.get("gate") == gate["id"]
                and value.get("signature") == approval_oracle_signature(file, gate))

    def record_approval(file, gate):
        """gate-check.mjs:467-509. Synchronous, unlike the oracle's async lock-wait loop --
        this whole file is synchronous, the same shape difference with_file_lock documents in
        lib/gates.py."""
        # create=True, so the `return None` arm above is unreachable here -- that arm fires only
        # when the directory is ABSENT and we were not asked to create it.
        store = typing.cast(dict, validated_approval_dir(create=True))
        token = approval_path(file, gate, store["path"])
        lock = token + ".lock"
        deadline = time.monotonic() + 10.0
        owner = os.urandom(16).hex()
        fd = None
        while True:
            try:
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except OSError as exc:
                if exc.errno != _errno.EEXIST:
                    raise
                # Fail closed instead of trying to steal by path -- same reasoning as the
                # oracle's own comment at this exact site.
                try:
                    os.stat(lock)
                except OSError as stat_exc:
                    if stat_exc.errno != _errno.ENOENT:
                        raise
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting for approval lock")
                sleep(20)
        try:
            # gate-check.mjs:494 is `JSON.stringify({owner, pid, at})` -- COMPACT, no indent.
            # _js_json_text, not a bare json.dumps, for the same two reasons gates.py:1623 gives
            # at the identical write: json.dumps defaults to `", "`/`": "` where JSON.stringify
            # emits `,`/`:`, and it leaves a lone surrogate raw so the .encode below raises where
            # the oracle writes the file. `owner` is a token today, but a helper is chosen for
            # what the call site GUARANTEES, not for what today's inputs happen to be.
            os.write(fd, _js_json_text({"owner": owner, "pid": os.getpid(),
                                        "at": int(time.time() * 1000)}).encode("utf-8"))
            value = {
                "schema": 1, "file": js_resolve(file), "gate": gate["id"],
                "signature": approval_oracle_signature(file, gate),
                "oracle": oracle(file, gate), "approvedAt": _iso_now(),
            }
            # gate-check.mjs:500 is `JSON.stringify(value, null, 2) + "\n"`. `_js_json_text` is
            # NOT usable verbatim here -- it hardcodes compact separators for the DIGEST path,
            # where compactness is part of the hashed bytes -- so this repeats only its lone-
            # surrogate substitution over an indent=2 dump, sharing the PATTERN so the two cannot
            # drift apart on what counts as a surrogate.
            #
            # WHY THIS LINE EXISTS: it shipped as a bare json.dumps and that was a live bug, the
            # THIRD instance of one class (gates.py:961 and digest_drive.py:147 are the other
            # two). ensure_ascii=False leaves a lone surrogate raw, write_atomic then encodes
            # utf-8, and the port raises UnicodeEncodeError where the oracle writes the approval
            # and exits 0 -- so the port classified the task not_run instead. Reachable without
            # anything exotic: CPython surrogateescape-decodes sys.argv ITSELF, so one invalid
            # UTF-8 byte in --cwd (or in PATH) puts a U+DCxx into `oracle`, which this line
            # serializes. Found by an end-to-end differential, not by reading; the import list at
            # :108 already said "REUSE them rather than re-derive a second, driftable copy", and
            # the rule being written down two hundred lines above did not stop the call site.
            write_atomic(token, _LONE_SURROGATE_RE.sub(
                lambda m: "\\u%04x" % ord(m.group(0)),
                json.dumps(js_json_object(value), indent=2, ensure_ascii=False)) + "\n")
            assert_approval_dir_unchanged(store)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                with open(lock, encoding="utf-8") as fh:
                    current = json.load(fh)
                if current.get("owner") == owner:
                    os.unlink(lock)
            except Exception:  # noqa: BLE001 -- manual cleanup or a successor owns the lock
                pass

    def print_oracle(file, gate, prefix):
        """gate-check.mjs:511-519."""
        value = oracle(file, gate)
        log(prefix + " " + qualify(file, gate["id"]))
        log("    CHECK: " + str(value["check"]))
        log("    EXPECT: " + str(value["expect"]))
        log("    CWD: " + str(value["cwd"]))
        log("    SHELL: " + str(value["shell"]))
        log("    PATH: " + str(path_transcript))

    # gate-check.mjs:716-739: gate selection. Every gate that is abandoned or has no CHECK is
    # skipped outright; a `--status` run or an already-met gate under a non-`--reverify` run
    # is skipped before its CWD is even resolved, matching the oracle's ordering exactly (a
    # bad CWD on a gate `--status` would never run must never fail a `--status` run).
    pending = []
    for ledger in ledgers:
        for gate in ledger["doc"]["gates"]:
            if gate["id"] in ledger["doc"]["abandoned"] or not gate.get("check"):
                continue
            state = gate_state(gate, ledger["doc"]["abandoned"])
            if opt.get("status") or (not opt.get("reverify") and state == "met"):
                continue
            cwd = resolved_gate_cwd(gate, ledger["file"])
            try:
                cwd_stat = os.stat(cwd)
            except OSError as exc:
                if exc.errno == _errno.ENOENT:
                    fail_usage("gate " + qualify(ledger["file"], gate["id"])
                               + " CWD does not exist: " + cwd)
                name = _errno.errorcode.get(exc.errno, "UNKNOWN") if exc.errno is not None else "UNKNOWN"
                description = (exc.strerror or "").lower()
                fail_usage("cannot inspect gate CWD " + cwd + ": " + name + ": " + description
                           + ", stat '" + cwd + "'")
            else:
                if not _stat.S_ISDIR(cwd_stat.st_mode):
                    fail_usage("gate " + qualify(ledger["file"], gate["id"])
                               + " CWD is not a directory: " + cwd)
            pending.append({
                "file": ledger["file"], "gate": gate, "cwd": cwd, "startingState": state,
                "wasMet": state == "met", "definitionDigest": gate_definition_digest(gate),
                "approvalSignature": approval_oracle_signature(ledger["file"], gate),
            })

    # gate-check.mjs:741-771: approval classification. Reads and, under `--approve`, WRITES
    # the real on-disk approval store -- this is the loop the corrected boundary exists for.
    runnable = []
    not_run = []
    approval_infrastructure_failures = 0
    for task in pending:
        approved = False
        try:
            approved = approval_exists(task["file"], task["gate"])
        except Exception as exc:  # noqa: BLE001 -- mirrors the oracle's catch-everything here
            error("gate-check: could not validate approval for "
                  + qualify(task["file"], task["gate"]["id"]) + ": " + str(exc))
            approval_infrastructure_failures += 1
            not_run.append(task)
            continue
        if not approved:
            print_oracle(task["file"], task["gate"], "APPROVAL REQUIRED")
            if not opt.get("approve"):
                log("    NOT RUN: inspect this oracle, then re-run with --approve")
                not_run.append(task)
                continue
            try:
                record_approval(task["file"], task["gate"])
                # FAITHFUL TO AN ORACLE QUIRK, DELIBERATELY NOT "FIXED". gate-check.mjs:762 is
                #     approvalPath(task.file, task.gate, validatedApprovalDir().path)
                # -- `create` defaults to false, so if the approval directory vanished between
                # record_approval creating it and this line, the oracle dereferences null and
                # throws, and the catch below turns that into an approvalInfrastructureFailures
                # bump. The port must do the SAME thing: cast() keeps the null-deref (a TypeError
                # here, "Cannot read properties of null" there), both land in the same handler.
                # Passing record_approval's already-validated `store` would be tidier AND would
                # be a divergence -- the oracle re-validates, and that re-validation is what
                # makes the TOCTOU window observable at all.
                log("    APPROVED: " + approval_path(
                    task["file"], task["gate"],
                    typing.cast(dict, validated_approval_dir())["path"]))
            except Exception as exc:  # noqa: BLE001
                error("gate-check: could not record approval for "
                      + qualify(task["file"], task["gate"]["id"]) + ": " + str(exc))
                approval_infrastructure_failures += 1
                not_run.append(task)
                continue
        runnable.append(task)

    for task in runnable:
        log("  RUN  " + qualify(task["file"], task["gate"]["id"]) + " shell=" + str(shell)
            + " cwd=" + task["cwd"] + " PATH=" + str(path_transcript))

    # THE PORT STOPS HERE -- gate-check.mjs:776, `results = opt.status ? [] : await
    # runRolling(runnable, jobs)`. That call is the only place this default run mode spawns a
    # process (lib/check-supervisor.mjs) or a Worker (the regex pool); everything from there to
    # the final MET/UNMET tally at :950 depends on its output. See the module docstring for why
    # this fires even when `runnable` is empty. Loud on purpose: see the module docstring on
    # why a silent fall-through would let a differential agree on a vector nothing implemented.
    error("gate_check.py: PORT INCOMPLETE -- ledger loading, gate selection, and the full "
          "approval-classification loop end at gate-check.mjs:775; the actual CHECK "
          "execution (spawning lib/check-supervisor.mjs, the regex Worker pool, process-tree "
          "teardown, per-check timeouts) and the final ledger/verdict tally are not ported "
          "yet. jobs=" + str(jobs) + " pending=" + str(len(pending)) + " runnable="
          + str(len(runnable)) + " not_run=" + str(len(not_run)) + " approval_infra_failures="
          + str(approval_infrastructure_failures))
    sys.exit(PORT_INCOMPLETE_EXIT)


if __name__ == "__main__":
    main(sys.argv[1:])
