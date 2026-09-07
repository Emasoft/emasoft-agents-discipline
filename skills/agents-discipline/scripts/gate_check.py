#!/usr/bin/env python3
"""gate_check.py : execute gate oracles, update evidence, coordinate scopes, manage leases.

Port of gate-check.mjs. The JavaScript suite is the ORACLE: it is held FIXED and only this
implementation varies, so any divergence is a porting defect and never a re-specified test.

FULL PORT of gate-check.mjs:27-950 -- the argument front end, target discovery,
ledger-existence checks, the --list-scopes/--log/--claim/--release actions, the default run
mode's ledger loading, gate selection, the full approval-classification loop, CHECK execution
(spawning lib/check_supervisor.py, the regex-worker subprocess pool, process-tree teardown,
per-check timeouts, and the PASS/FAIL print loop), the ledger EVIDENCE rewrite under
withFileLock (stale-result detection included), and the final reloaded-ledger MET/UNMET/
ABANDONED tally that decides the process exit code.

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

Everything that SPAWNS a process or a worker in this default run
mode is ported: `runRolling`/`run_rolling` (:700-714), `runCheck`/`run_check`
(:593-698, spawning lib/check_supervisor.py -- ported here as CHECK_SUPERVISOR -- as a
detached process-group leader, capturing stdout/stderr with the same live MAX_OUTPUT_BYTES
cap, tearing the group down via lib/process_tree.py's `terminate_process_tree` on overflow or
per-check timeout), and `safeRegexMatch`/`safe_regex_match` (:538-586, a `MAX_REGEX_WORKERS`-
wide pool of disposable lib/regex_worker.py subprocesses in place of the oracle's
`node:worker_threads` Worker pool -- the same one-shot, single-message contract
regex_worker.py's own docstring already documents, bounded by the same
REGEX_STARTUP_TIMEOUT_MS/REGEX_TIMEOUT_MS pair). These are LOCAL to gate-check.mjs (0 matches
for any of them in lib/gates.mjs), so they are ported HERE rather than assumed to already
exist in lib/gates.py.

Ledger loading (:297), the `pending` gate-selection loop with real CWD
resolution and validation (:716-739), shell resolution and the full approval-token machinery
-- `resolveShell`/`oracle`/`approvalOracleSignature`/`approvalPath`/`validatedApprovalDir`/
`assertPrivateApprovalEntry`/`assertApprovalDirUnchanged`/`readApprovalFile`/`approvalExists`/
`recordApproval`/`printOracle` (:299-519) -- the classification loop that turns `pending` into
`runnable`/`notRun` while genuinely reading and writing the on-disk approval store
(:741-775) -- CHECK execution itself: `runCheck`, `safeRegexMatch`, `runRolling`,
`outputFingerprint`, and `failureOutput` (:538-800), all spawning real subprocesses rather
than being stubbed -- and, finally, the ledger-mutation half: `evidenceFor`/
`insertOrUpdateEvidence` writing PASS/FAIL back into EVIDENCE lines under `withFileLock`
(:802-858), the reloaded-ledger MET/UNMET/ABANDONED tally (:861-895), `dispatchStatus`
aggregation for a scoped pipeline's native dispatch waves (:897-910), and the `extraUnmet`
bookkeeping across `--reverify` and stale results that decides the final exit code
(:912-950).

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
import hashlib
import json
import math
import os
import re
import signal as _signal_module
import stat as _stat
import subprocess
import sys
import threading
import time
import typing

_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
sys.path.insert(0, _LIB_DIR)
from gates import (  # noqa: E402  # type: ignore[import-not-found]
    AGENTS_DISCIPLINE_DIR, MAX_AUTOMATIC_EVIDENCE_CHARS, MAX_CHECK_OUTPUT_BYTES,
    automatic_evidence_prefix, claim_leases, format_document, gate_definition_digest,
    gate_state, js_basename, js_dirname, js_resolve, list_scopes, mkdirs, parse_gates, qualify,
    read_stable_regular_file, release_leases, resolve_target, same_file_identity, sha256,
    sleep, stat_current_named_file, append_status, validate_scope_id, with_file_lock,
    write_atomic,
    # Underscore-prefixed: module-private in gates.py, but already the CORRECT port of the
    # exact primitives gate-check.mjs's own approval machinery needs (path.join,
    # pathIsInside, and JSON.stringify-as-text). REUSE them rather than re-derive a second,
    # driftable copy -- the standing rule for every helper this project has already ported.
    _js_join, _js_json_text, _LONE_SURROGATE_RE, _path_is_inside, node_fs_message,
)
from jsapi import (  # noqa: E402  # type: ignore[import-not-found]
    force_utf8_streams, js_json_object, js_length, js_slice, js_to_number, js_trim,
    normalize_argv)
from dispatch import _iso_now, dispatch_status  # noqa: E402  # type: ignore[import-not-found]
from process_tree import terminate_process_tree  # noqa: E402  # type: ignore[import-not-found]

# BEFORE anything can print. node's stdout/stderr are UTF-8 whatever the locale is; CPython's
# follow the locale, so on an ASCII stream this program backslash-escapes a character the oracle
# prints. MEASURED under `PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C` -- see the function's
# docstring for the transcript, and for why a bare `LC_ALL=C` measures nothing.
force_utf8_streams()

# AND BEFORE anything can READ an argument. CPython surrogateescape-decodes argv where node runs
# the WHATWG UTF-8 decoder, so the two runtimes hold different STRINGS for the same bytes before
# either program starts. See the function's docstring for the measured transcript.
normalize_argv()

# gate-check.mjs:72 -- the sibling supervisor process that keeps a stable process-group
# leader alive until CHECK's stdio closes. lib/regex_worker.py is the disposable one-shot
# EXPECT matcher (gate-check.mjs's node:worker_threads Worker, ported onto stdio -- see
# regex_worker.py's own module docstring).
CHECK_SUPERVISOR = os.path.join(_LIB_DIR, "check_supervisor.py")
REGEX_WORKER = os.path.join(_LIB_DIR, "regex_worker.py")

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
        if exc.errno == _errno.ENOENT:
            fail_usage(label + " does not exist: " + path)
        # node_fs_message, not a hand-rolled copy of it. These six lines REPRODUCED that helper
        # inline -- code lookup, lowercased strerror, `, stat '<path>'` suffix -- and they were
        # CORRECT, which is why no differential found them and why the `str(exc)` grep that drove
        # the errno sweep did not list them: they never spelled `str(exc)`. A private copy of a
        # function whose measured errno scope is documented at ONE place drifts silently the day
        # that scope changes, and nothing here would say so.
        # `stat`, not `open`, and this is the sweep's only non-`open` site: the try wraps os.stat,
        # and the oracle's statSync catch names `stat` too. Row 10 of errno-message-diff.sh was
        # added BEFORE the change, for a site with no defect, purely to guard the edit.
        #
        # THE `errno is not None` BRANCH IS DEAD HERE, and the guard is for UNIFORMITY, not
        # fidelity -- a distinction worth drawing because at every OTHER site in this sweep the
        # fallback is genuinely correct. There, an errno-less error is one the port AUTHORED
        # (`OSError("<label> must be one unchanged regular single-link file: <path>")`) and str()
        # round-trips exactly the text the oracle throws. Here the try wraps a bare os.stat: no
        # authored error is raised inside it, os.stat always sets an errno, and node's statSync
        # always carries a code. So no oracle behaviour exists for this branch to match, the old
        # `UNKNOWN: , stat '<path>'` was an invention too, and the only real criterion left is
        # that a reader should not have to wonder why this site's shape differs from the nine
        # others.
        # WHAT IT IS NOT is a reason to drop the guard, which the first version of this refactor
        # did on the argument that the helper does the same job. MEASURED on the port's real
        # authored shape (one arg, no filename -- an earlier transcript set .filename by hand and
        # published a string the port cannot produce):
        #     str(exc)  gate ledger must be one unchanged regular single-link file: /p
        #     helper    gate ledger must be ...: /p: gate ledger must be ...: /p, stat
        # The helper doubles the message and appends a bare `, stat` with no path. Row 10
        # exercises EACCES, where helper and hand-rolled agree exactly, so it stayed green
        # through a change that was not the pure refactor its commit called it.
        fail_usage("cannot inspect " + label + " " + path + ": "
                   + (node_fs_message(exc, "stat") if exc.errno is not None else str(exc)))
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
            # gate-check.mjs:211 interpolates `error.message`. THE SEVENTH SITE, and the first
            # one whose ROW WAS WRITTEN FIRST: it went red on an unwritable scope directory
            # before this line was touched, then green after. That ordering is worth keeping --
            # a row verified against a defect that ACTUALLY EXISTS proves it detects the real
            # shape, where a row written after the fix can only be mutation-tested against a
            # shape I planted.
            #     oracle  ... : EACCES: permission denied, open '<root>/.../status.log'
            #     port    ... : [Errno 13] Permission denied: '<root>/.../status.log'
            # `open` measured, not assumed: append_status opens the log to append, and that open
            # is what fails when the directory is unwritable.
            error("gate-check: cannot append status: "
                  + (node_fs_message(exc, "open") if exc.errno is not None else str(exc)))
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
            # gate-check.mjs:234 interpolates `error.message`. THE FIFTH SITE OF ONE DEFECT, and
            # the first one a RUNNER found rather than a person: errno-message-diff.sh's
            # gate-check row was written green-or-red-is-information and came back red on its
            # first execution.
            #     oracle  ... : EACCES: permission denied, open '<path>'
            #     port    ... : [Errno 13] Permission denied: '<path>'
            # `open` even though the message says "inspect": this is a read_ledger_file catch,
            # not a stat, so the failing syscall is the helper's open -- confirmed by the oracle
            # naming `open` in the measured output above rather than assumed from the wording.
            fail_usage("cannot inspect gate file " + file + ": "
                       + (node_fs_message(exc, "open") if exc.errno is not None else str(exc)))

    def load_ledger(file):
        try:
            text = read_ledger_file(file)
        except OSError as exc:
            # gate-check.mjs:241. Fixed in the same edit as the site above on a STRUCTURAL
            # identity, not on the analogy an earlier version of this comment gave ("its twin --
            # every previous instance was found alone and its sibling left in place"). That
            # sentence is a lesson about process, and it would have justified this edit just as
            # readily if the two sites had wrapped DIFFERENT calls, which is how the same reuse
            # of an argument went wrong at dispatch.py's read_state. The real warrant: both catch
            # around the SAME call, read_ledger_file(file), so same helper, same first syscall,
            # same errno surface -- and the oracle's :241 is a bare readLedgerFile catch,
            # identical in shape to :234, which is the one measured.
            # UNGUARDED, and said out loud: no row reaches this line. The pre-flight loop above
            # rejects an unreadable file first, so arriving here needs the file to become
            # unreadable BETWEEN that loop and this read -- a race no static fixture produces.
            fail_usage("cannot read " + file + ": "
                       + (node_fs_message(exc, "open") if exc.errno is not None else str(exc)))
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
                # gate-check.mjs:265. Fixed alongside the --claim site below on the structural
                # identity the :472/:478 pair established: both wrap a lease helper whose failing
                # syscall is the open of the same `<hex>.filelock`. Only --claim has a row (an
                # unwritable locks/ directory); --release reaches its own line only when a lease
                # already exists in a directory that has since become unwritable, which no static
                # fixture builds. UNGUARDED and said so, rather than left to look covered.
                # WHICH HALF IS MEASURED: the GUARD is certainly right -- `errno is not None`
                # degrades to str() exactly as before when there is none. The SYSCALL CONSTANT is
                # inherited from the twin and has never been observed at this line. One word, and
                # a reader should know to check it rather than trust it because the block around
                # it sounds settled.
                error("gate-check: cannot release leases: "
                      + (node_fs_message(exc, "open") if exc.errno is not None else str(exc)))
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
            # gate-check.mjs:278. THE EIGHTH SITE, row written first and red before this line
            # was touched:
            #     oracle  ... : EACCES: permission denied, open '<root>/.../<hex>.filelock'
            #     port    ... : [Errno 13] Permission denied: '<root>/.../<hex>.filelock'
            # The row needed its own scrub: a lock name is a digest of the RESOLVED scope path,
            # so two per-runtime roots produce two different names by construction.
            error("gate-check: cannot claim leases: "
                  + (node_fs_message(exc, "open") if exc.errno is not None else str(exc)))
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
        path_evidence = None
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
        # gate-check.mjs:326-328. `os.pathsep` is node's `path.delimiter` exactly (":" on
        # posix, ";" on win32). Unlike resolve_shell's own PATH split above, this one does
        # NOT filter empty segments -- `pathValue.split(delimiter).length` counts them.
        # NOT gates.py's sha256() -- it is deliberately FAIL-CLOSED on a lone surrogate, and
        # that is right for the LOCK key it was written for (node hashes a lossy key there, so
        # two different paths can share one lock; refusing is safer). This is a DISPLAY hash in
        # the RUN transcript, where the oracle at :326 emits a value, so refusing is a crash
        # where the oracle prints -- measured: UnicodeEncodeError at this line on a PATH
        # carrying one invalid UTF-8 byte, while gate-check.mjs completes.
        #
        # gates.py's own comment says "no spelling of errors= reproduces Node's
        # one-U+FFFD-per-surrogate" -- true of errors= ("replace" gives "?", surrogatepass gives
        # three bytes), and it does not follow that nothing reproduces it. An explicit
        # substitution does, exactly: for "/usr/bin:\udcff" both node and this line give
        # 29f7a3f616c4. So the port matches the oracle here rather than trading a fail-closed
        # divergence for a crash.
        path_hash = hashlib.sha256(
            _LONE_SURROGATE_RE.sub("�", path_value).encode("utf-8")).hexdigest()[:12]
        path_count = len(path_value.split(os.pathsep)) if path_value else 0
        path_evidence = path_hash + "/" + str(path_count) + " entries"

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
            # mkdirs, NOT os.makedirs: this line inherited BOTH defects that helper exists to
            # prevent, and neither was visible from here. (1) os.makedirs applies `mode` to the
            # FINAL component only and leaves intermediates at the umask default, where the
            # oracle's recursive mkdirSync applies it to all -- so an approval tree created
            # through a missing parent had a world-readable ancestor above a 0700 leaf. (2) The
            # escaping error named the wrong syscall: MEASURED, an unwritable approval parent
            # gave node "EACCES: permission denied, mkdir '<dir>'" against this port's
            # "... open '<dir>'", because the catch upstream hardcodes `open` and a bare
            # makedirs attaches nothing to say otherwise.
            mkdirs(directory, 0o700)
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
                # The second hand-rolled copy, same six lines, same `stat` syscall, same reason
                # for going through the helper instead -- see the --root site above, including
                # the `errno is not None` guard and why dropping it was not the pure refactor it
                # was called. UNGUARDED by a row: reaching it needs a gate CWD that exists but
                # cannot be stat'd, which means an unsearchable parent, and the ledger under it
                # would then be unreadable first. So this site was never RUN in either form --
                # the previous commit called both copies "correct" on one measurement taken at
                # the OTHER one, which is an assertion by symmetry wearing a measurement's words.
                fail_usage("cannot inspect gate CWD " + cwd + ": "
                           + (node_fs_message(exc, "stat") if exc.errno is not None else str(exc)))
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
            # gate-check.mjs:748, the twin of the record site below -- both catch around a helper
            # that opens a file under the approval directory. Row 9 reaches the RECORD site; this
            # one needs approval_exists to fail while the record path is never entered, which no
            # static fixture builds. UNGUARDED, and fixed as a structural twin rather than an
            # analogy: same directory, same open, same errno surface.
            # `Exception`, not OSError, so getattr -- the oracle's catch is equally wide.
            error("gate-check: could not validate approval for "
                  + qualify(task["file"], task["gate"]["id"]) + ": "
                  + (node_fs_message(exc, "open")
                     if getattr(exc, "errno", None) is not None else str(exc)))
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
                # THE RE-CALL IS A SECURITY RE-VALIDATION, WHICH IS WHY IT MUST STAY.
                # gate-check.mjs:762 is
                #     approvalPath(task.file, task.gate, validatedApprovalDir().path)
                # and validatedApprovalDir re-runs lstat + assertPrivateApprovalEntry + realpath
                # + the pathIsInside repo-root check.
                #
                # PRECISELY WHAT THE RE-CALL ADDS, because an earlier version of this comment
                # said it would "skip a security re-validation" and that OVERSTATES it:
                # record_approval already calls assert_approval_dir_unchanged(store) after the
                # write, and that re-runs assert_private_approval_entry (symlink, ownership,
                # group/other bits) plus a same_file_identity check. But it does so on
                # store["path"], which is the CANONICAL path only. The re-call additionally
                #   (a) re-lstats and re-validates the ORIGINAL approval_dir -- the path that
                #       could have been swapped for a symlink pointing somewhere else, which is
                #       exactly what a canonical-only check cannot see, and
                #   (b) re-runs the pathIsInside repo-root containment check.
                # So reusing `store` would still be a real divergence, just a narrower one than
                # first claimed: the ownership/permission re-check is NOT what would be lost.
                #
                # The null-deref below is a side effect of keeping that re-call, not the reason
                # for it: `create` defaults to false, so if the directory vanished entirely the
                # oracle dereferences null and the catch turns it into an
                # approvalInfrastructureFailures bump. cast() keeps that shape. An earlier
                # version of this comment led with the deref, which reads like rationalising a
                # bug; the re-validation is the actual argument and it makes the choice obvious.
                #
                # WHAT IS FAITHFUL, PRECISELY -- an earlier version of this comment said the two
                # "crash identically", and MEASURED that is too strong:
                #     JS   Cannot read properties of null (reading 'path')
                #     PY   'NoneType' object is not subscriptable
                # The CONTROL FLOW is identical (deref -> same handler -> same counter -> same
                # exit); the MESSAGE TEXT is not. That difference is not created by this line: it
                # is the general, unavoidable class at EVERY `except Exception as exc` site in
                # this port, because str(exc) is CPython's text where the oracle prints V8's. It
                # is worth naming here rather than leaving "identically" to be read as byte
                # equality by whoever audits this next.
                #
                # Passing record_approval's already-validated `store` would be tidier AND a
                # CONTROL-FLOW divergence -- the oracle re-validates, and that re-validation is
                # what makes the TOCTOU window observable at all.
                log("    APPROVED: " + approval_path(
                    task["file"], task["gate"],
                    typing.cast(dict, validated_approval_dir())["path"]))
            except Exception as exc:  # noqa: BLE001
                # gate-check.mjs:764. THE NINTH SITE and the last of this sweep, row written
                # first and red before this line was touched:
                #     oracle  ... : EACCES: permission denied, open '<dir>/<digest>.json.lock'
                #     port    ... : [Errno 13] Permission denied: '<dir>/<digest>.json.lock'
                # The row needs two scrubs of its own: the approval dir cannot live inside the
                # root (gate-check refuses that), so the runtimes use sibling dirs, and the file
                # is named by a digest that BINDS the resolved ledger path.
                error("gate-check: could not record approval for "
                      + qualify(task["file"], task["gate"]["id"]) + ": "
                      + (node_fs_message(exc, "open")
                         if getattr(exc, "errno", None) is not None else str(exc)))
                approval_infrastructure_failures += 1
                not_run.append(task)
                continue
        runnable.append(task)

    for task in runnable:
        log("  RUN  " + qualify(task["file"], task["gate"]["id"]) + " shell=" + str(shell)
            + " cwd=" + task["cwd"] + " PATH=" + str(path_transcript))

    # gate-check.mjs:521-536: a bounded, MAX_REGEX_WORKERS-wide pool of disposable EXPECT
    # matchers. JS backs this with a Promise-queue semaphore around node:worker_threads
    # Worker instances; a plain counting Semaphore around one-shot regex_worker.py
    # subprocesses is the same bound with this file's synchronous, thread-based concurrency
    # model (every `run_check` call already executes inside its own `run_rolling` worker
    # thread, so blocking here blocks only that one task, exactly like awaiting the JS Promise
    # blocks only that one task's async chain).
    _regex_worker_semaphore = threading.Semaphore(MAX_REGEX_WORKERS)

    def safe_regex_match(expectation, output):
        """gate-check.mjs:538-586. `text` is a substring check; `regex` spawns the disposable
        one-shot lib/regex_worker.py and bounds it by the same two timeouts the oracle uses:
        REGEX_STARTUP_TIMEOUT_MS for the worker to come alive, REGEX_TIMEOUT_MS for the match
        itself (catastrophic backtracking budget) once it has."""
        if expectation["kind"] == "text":
            return {"matched": expectation["value"] in output}
        _regex_worker_semaphore.acquire()
        try:
            try:
                worker = subprocess.Popen(
                    [sys.executable, REGEX_WORKER],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            except OSError as exc:
                # NOT node_fs_message, and "str() is correct here" would be too generous -- it is
                # the least wrong available. The errno sweep left this site and the two below
                # alone on the strength of their WORDING ("worker", "spawn"); read instead:
                # gate-check.mjs:557 wraps `new Worker(...)`, a V8 Error with no errno, while
                # this wraps subprocess.Popen, which raises ENOENT/EACCES. So the two runtimes
                # produce structurally different text by construction, and node_fs_message would
                # invent an fs shape node never emits here. UNMEASURED DIVERGENCE, recorded as
                # one rather than filed under "correct": nothing makes these two agree, and
                # :985's note explains why that is out of scope rather than why it is fine.
                return {"matched": False, "error": "EXPECT worker could not start: " + str(exc)}
            payload = _js_json_text({
                "source": expectation["source"], "flags": expectation.get("flags") or "",
                "output": output,
            }).encode("utf-8") + b"\n"
            budget_seconds = (REGEX_STARTUP_TIMEOUT_MS + REGEX_TIMEOUT_MS) / 1000.0
            try:
                # stderr is deliberately dropped: the worker's diagnostics are not part of the
                # match verdict, and the oracle ignores them too. Bare `_`, not `_stderr_bytes`
                # -- a checker treats only `_` as the intentionally-unused name.
                stdout_bytes, _ = worker.communicate(input=payload, timeout=budget_seconds)
            except subprocess.TimeoutExpired:
                # kill() + a bounded communicate() reaps the process before this function
                # returns on every path -- constraint 1: no process this file spawns may
                # outlive the call that spawned it.
                worker.kill()
                try:
                    worker.communicate(timeout=5.0)
                except subprocess.TimeoutExpired:
                    threading.Thread(target=worker.wait, daemon=True).start()
                return {"matched": False, "error": "EXPECT regex exceeded " + str(REGEX_TIMEOUT_MS) + "ms"}
            if worker.returncode != 0:
                return {"matched": False,
                        "error": "EXPECT worker exited " + str(worker.returncode) + " without a result"}
            try:
                first_line = stdout_bytes.decode("utf-8", errors="replace").splitlines()[0]
                return json.loads(first_line)
            except (IndexError, ValueError) as exc:
                return {"matched": False,
                        "error": "EXPECT worker exited " + str(worker.returncode)
                                 + " without a result: " + str(exc)}
        finally:
            _regex_worker_semaphore.release()

    def output_fingerprint(output):
        """gate-check.mjs:588-591."""
        value = str(output)
        return {"sha256": sha256(value), "bytes": len(value.encode("utf-8"))}

    def _signal_name(returncode):
        """`subprocess.Popen.returncode` is POSIX's own convention (negative = killed by
        signal -returncode); Node instead reports `(code=null, signal="SIGxxx")` for the same
        event -- the same translation lib/check_supervisor.py already does at its own exit."""
        if returncode is None or returncode >= 0:
            return None
        try:
            return _signal_module.Signals(-returncode).name
        except ValueError:
            return "unknown signal"

    def run_check(task):
        """gate-check.mjs:593-698. Synchronous (this whole file is), unlike the oracle's
        callback-driven Promise -- the concurrency `runRolling`/`run_rolling` gives each task
        is a worker THREAD here rather than an event-loop turn there, so a blocking call here
        blocks only that one task, matching the oracle's per-task isolation.

        Constraint 1 (process reaping): every path below either (a) never spawns a process
        (the OSError branch), or (b) spawns exactly one lib/check_supervisor.py and calls
        `child.wait()` before returning -- including the timeout/overflow branch, which sends
        SIGKILL to the whole process group first (terminate_process_tree) so the blocking wait
        below resolves promptly. A bounded final `wait(timeout=...)` is followed by a daemon
        reaper thread only for the pathological case where the group refuses to die even after
        SIGKILL -- so this call never blocks forever on a wedged descendant, and the process is
        still reaped eventually rather than left a zombie."""
        state = {
            "bytes": 0, "raw_overflow": False, "timed_out": False, "stop_called": False,
            "cleanup_diagnostic": None,
        }
        lock = threading.Lock()
        chunks = {"stdout": [], "stderr": []}

        try:
            # `shell` is Optional only because :535 sets it None under --status, and run_check
            # is unreachable there: --status makes the :808 guard `continue` for every gate, so
            # `pending` is empty and `runnable` with it. MEASURED, not inferred from the oracle's
            # shape -- the port run gives pending=0 under --status and pending=1 with the same
            # ledger and only that flag removed. Same justification as the cast at :623.
            child = subprocess.Popen(
                [sys.executable, CHECK_SUPERVISOR, typing.cast(str, shell), task["gate"]["check"]],
                cwd=task["cwd"], env=os.environ,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=(sys.platform != "win32"),
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
        except OSError as exc:
            fingerprint = output_fingerprint("")
            return {**task, "ok": False, "output": "", "outputFingerprint": fingerprint,
                    "exitCode": None, "signal": None, "matched": False, "error": str(exc)}

        def stop_child():
            # gate-check.mjs:639-660 (`stopChild`). Idempotent -- both the byte-cap capture
            # thread and the timeout timer can call this, and only the first one must act.
            with lock:
                if state["stop_called"]:
                    return
                state["stop_called"] = True
            cleanup = terminate_process_tree(child)
            state["cleanup_diagnostic"] = cleanup.get("diagnostic")

        def capture(stream_name, handle):
            # gate-check.mjs:662-671 (`capture`). Enforces MAX_OUTPUT_BYTES as data arrives,
            # not after the process exits -- a runaway CHECK is cut off mid-stream.
            #
            # WHAT THIS READS FROM, because the distinction decides the read/read1 argument
            # below and an earlier version of that comment got it one layer wrong: `handle` is
            # the SUPERVISOR's pipe, not the CHECK's. lib/check_supervisor.py sits between,
            # doing its own read1/write/flush, so the chunk sizes arriving here are shaped by
            # the SUPERVISOR's write sizes -- not by how the CHECK writes. Measured on ~469 KB:
            # 8 appends, i.e. 7 full 65536 reads plus a 10142 remainder, so the buffer does fill
            # on this path (an average of ~58 KB across the 8 is just what 7-full-plus-1-short
            # arithmetic gives, not evidence of a smaller quantum).
            while True:
                try:
                    # read(), not read1(), and that is DELIBERATE after being wrong about it.
                    # 3c78513 changed this line to read1() alongside the real fix in
                    # check_supervisor.py, justified by "the timeout path closes the handle, so
                    # a blocked read() loses its buffer". THE PREMISE IS FALSE. capture() closes
                    # the handle only after its own loop has already exited, and NO CLOSE-LIKE
                    # CALL APPEARS ANYWHERE IN lib/process_tree.py -- searched for .close(),
                    # os.close, communicate(, `with ...Popen(`, closefd and __exit__ across all
                    # 377 lines, zero matches. (Stated as what was searched, not as "the
                    # function only signals": terminate_process_tree's body is unread, and an
                    # earlier version of this comment asserted the stronger form from a grep
                    # that could not even have seen communicate().) So the writer dies, the
                    # reader gets EOF, and read() returns its partial buffer normally.
                    # FALSIFIED, not argued: with this line reverted to read() and only the
                    # supervisor fixed, `echo starting; sleep 30` under --timeout 3 gives
                    # `output=starting`, byte-identical to the oracle. The read1() here changed
                    # no observable, so it is gone -- an inert edit inside a bugfix commit is
                    # how a wrong diagnosis survives review.
                    chunk = handle.read(65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                trigger = False
                with lock:
                    remaining = MAX_OUTPUT_BYTES - state["bytes"]
                    if remaining > 0:
                        chunks[stream_name].append(chunk[:remaining])
                    state["bytes"] += len(chunk)
                    if state["bytes"] > MAX_OUTPUT_BYTES and not state["raw_overflow"]:
                        state["raw_overflow"] = True
                        trigger = True
                if trigger:
                    stop_child()
            try:
                handle.close()
            except OSError:
                pass

        stdout_thread = threading.Thread(target=capture, args=("stdout", child.stdout), daemon=True)
        stderr_thread = threading.Thread(target=capture, args=("stderr", child.stderr), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        def on_timeout():
            state["timed_out"] = True
            stop_child()

        timeout_timer = threading.Timer(timeout_seconds, on_timeout)
        timeout_timer.daemon = True
        timeout_timer.start()

        # SIGKILL to the whole process group (stop_child, above) closes every inherited pipe
        # almost immediately, so these joins return promptly on the overflow/timeout paths
        # too; the bound below only guards the pathological case of a descendant that escaped
        # the group and still holds a pipe open.
        stdout_thread.join(timeout=5.0)
        stderr_thread.join(timeout=5.0)
        try:
            exit_status = child.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            stop_child()
            try:
                exit_status = child.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                # Never block the caller forever on a wedged descendant; keep trying to reap
                # it in the background so it does not linger as a zombie (constraint 1).
                threading.Thread(target=child.wait, daemon=True).start()
                exit_status = None
        timeout_timer.cancel()

        stdout_text = b"".join(chunks["stdout"]).decode("utf-8", errors="replace")
        stderr_text = b"".join(chunks["stderr"]).decode("utf-8", errors="replace")
        output = stdout_text + ("\n" if stdout_text and stderr_text else "") + stderr_text
        fingerprint = output_fingerprint(output)
        normalized_overflow = fingerprint["bytes"] > MAX_OUTPUT_BYTES
        overflow = state["raw_overflow"] or normalized_overflow
        timed_out = state["timed_out"]
        if timed_out or overflow:
            match = {"matched": False}
        else:
            match = safe_regex_match(task["gate"]["expectation"], output)
        cleanup_diagnostic = state["cleanup_diagnostic"]
        cleanup_suffix = ("; cleanup: " + cleanup_diagnostic) if cleanup_diagnostic else ""
        if timed_out:
            check_error = "timed out after " + str(timeout_seconds) + "s" + cleanup_suffix
        elif overflow:
            check_error = ("output exceeded " + str(MAX_OUTPUT_BYTES) + " bytes"
                            + ("" if state["raw_overflow"] else " after stdout/stderr UTF-8 combination")
                            + cleanup_suffix)
        else:
            check_error = match.get("error")
        exit_code = None if exit_status is None or exit_status < 0 else exit_status
        return {
            **task, "output": output, "outputFingerprint": fingerprint, "exitCode": exit_code,
            "signal": _signal_name(exit_status), "matched": bool(match.get("matched")),
            "error": check_error, "ok": not check_error and exit_code == 0 and bool(match.get("matched")),
        }

    def run_rolling(tasks, limit):
        """gate-check.mjs:700-714. A pull-based work queue -- `limit` worker threads race to
        claim the next index, so `results[index]` always lands the right task's result even
        though completion order is unordered, exactly like the oracle's `limit` concurrent
        async workers racing the same shared `next` counter."""
        # Annotated because `[None] * n` alone infers list[None], after which EVERY later
        # `results[i] = run_check(...)` and every `result["..."]` read is a type error --
        # 9 of the 10 the checker raised here cascade from this one line. The prefill is
        # deliberate: the oracle's runRolling writes results BY INDEX so output order matches
        # task order regardless of completion order, and a list that grows by append would
        # silently reorder them under concurrency.
        results: list = [None] * len(tasks)
        next_index = [0]
        index_lock = threading.Lock()

        def worker():
            while True:
                with index_lock:
                    index = next_index[0]
                    next_index[0] += 1
                if index >= len(tasks):
                    return
                results[index] = run_check(tasks[index])

        workers = []
        for _ in range(min(limit, len(tasks))):
            thread = threading.Thread(target=worker)
            thread.start()
            workers.append(thread)
        for thread in workers:
            thread.join()
        return results

    # gate-check.mjs:776: the single call that spawns every CHECK process and regex worker.
    results = [] if opt.get("status") else run_rolling(runnable, jobs)

    def failure_output(output, max_len=480):
        """gate-check.mjs:795-800. `js_trim`/`js_slice`, not `str.strip()`/Python slicing --
        the oracle's `.trim()`/`.slice()` operate on JS's whitespace set and UTF-16 code
        units, not Python's."""
        lines = [line for line in (js_trim(part) for part in re.split(r"\r?\n", str(output))) if line]
        if len(lines) <= 8:
            return js_slice(" | ".join(lines) or "(no output)", 0, max_len)
        summary = " | ".join(lines[:6] + ["..."] + lines[-2:])
        return js_slice(summary, 0, max_len)

    # gate-check.mjs:777-793: the RUN results -- PASS/FAIL per task, printed before anything
    # touches a ledger file.
    for result in results:
        fingerprint = result["outputFingerprint"]
        if result["ok"]:
            output_summary = "sha256=" + fingerprint["sha256"] + "; bytes=" + str(fingerprint["bytes"])
        else:
            output_summary = failure_output(result["output"])
        outcome = ("exit=" + ("none" if result["exitCode"] is None else str(result["exitCode"]))
                   + (" signal=" + result["signal"] if result["signal"] else "")
                   + "; EXPECT=" + ("matched" if result["matched"] else "not matched")
                   + "; output=" + output_summary)
        if result["ok"]:
            log("  PASS " + qualify(result["file"], result["gate"]["id"]) + ": " + str(result["gate"]["title"]))
            log("       " + outcome)
        else:
            log("  FAIL " + qualify(result["file"], result["gate"]["id"]) + ": " + str(result["gate"]["title"]))
            log("       " + ((result["error"] + "; ") if result["error"] else "") + outcome)

    def evidence_for(result):
        """gate-check.mjs:802-812. Order matters: the definition binding and the successful-
        output fingerprint sit ahead of machine-specific fields so MAX_AUTOMATIC_EVIDENCE_CHARS
        truncates only transcript detail, never the structural currentness or output identity."""
        def clean(value):
            return re.sub(r"[\r\n\t]+", " ", terminal_safe(value))
        fingerprint = result["outputFingerprint"]
        text = (automatic_evidence_prefix(result["definitionDigest"])
                + " exit=0; EXPECT=matched; output-sha256=" + fingerprint["sha256"]
                + "; output-bytes=" + str(fingerprint["bytes"]) + "; shell=" + clean(shell)
                + "; cwd=" + clean(result["cwd"]) + "; path=" + path_evidence)
        return js_slice(text, 0, MAX_AUTOMATIC_EVIDENCE_CHARS)

    def insert_or_update_evidence(doc, gate, value):
        """gate-check.mjs:814-823."""
        if gate["evidenceLine"] != -1:
            indent_match = re.match(r"^\s*", doc["lines"][gate["evidenceLine"]])
            indent = indent_match.group(0) if indent_match else "  "
            doc["lines"][gate["evidenceLine"]] = indent + "EVIDENCE: " + value
            return
        line = gate["line"] + 1
        while line < len(doc["lines"]) and re.match(r"^\s+(CHECK|EXPECT|EVIDENCE|CWD):", doc["lines"][line]):
            line += 1
        doc["lines"].insert(line, "  EVIDENCE: " + value)

    def result_key(file, gate_id):
        """gate-check.mjs:825: `resolve(file) + "\\0" + id`."""
        return js_resolve(file) + "\0" + gate_id

    # gate-check.mjs:826-858: rewrite EVIDENCE lines for this run's results under a per-file
    # lock, re-reading the ledger fresh so a concurrent edit is detected as staleness rather
    # than clobbered.
    stale_results = {}
    any_lock_write_failed = False
    for result in results:
        def rewrite(result=result):
            doc = parse_gates(read_ledger_file(result["file"]))
            if doc["errors"]:
                raise RuntimeError("fresh ledger became invalid: " + "; ".join(doc["errors"]))
            fresh = next((g for g in doc["gates"] if g["id"] == result["gate"]["id"]), None)
            if (fresh is None
                    or gate_definition_digest(fresh) != result["definitionDigest"]
                    or approval_oracle_signature(result["file"], fresh) != result["approvalSignature"]):
                stale_results[result_key(result["file"], result["gate"]["id"])] = \
                    qualify(result["file"], result["gate"]["id"])
                log("  STALE " + qualify(result["file"], result["gate"]["id"])
                    + ": definition or runtime approval oracle changed; result not written")
                return
            fresh_state = gate_state(fresh, doc["abandoned"])
            if fresh_state == "abandoned":
                return
            must_write_failure = (result["startingState"] == "stale-unmet"
                                   or (bool(opt.get("reverify")) and result["wasMet"])
                                   or fresh["checked"])
            if not result["ok"] and not must_write_failure:
                return
            if result["ok"]:
                doc["lines"][fresh["line"]] = re.sub(
                    r"^- \[( |x|X)\]", "- [x]", doc["lines"][fresh["line"]])
                insert_or_update_evidence(doc, fresh, evidence_for(result))
            else:
                doc["lines"][fresh["line"]] = re.sub(
                    r"^- \[(x|X)\]", "- [ ]", doc["lines"][fresh["line"]])
                insert_or_update_evidence(doc, fresh, "pending")
            write_atomic(result["file"], format_document(doc))

        try:
            with_file_lock(root, result["file"], rewrite)
        except Exception as exc:  # noqa: BLE001 -- the oracle's catch is equally wide
            # gate-check.mjs:855, and THE FIRST WRITE SITE IN THIS SWEEP -- every one before it
            # was a read. It was picked next precisely because the syscall constant looked most
            # likely to break here, and MEASURED it does not: node still says `open`, because
            # writeAtomic's failing call is the open of its TEMP file.
            #
            # MEASURED FOR A FAILURE AT THE TEMP OPEN, WHICH IS NOT THE WHOLE WRITE PATH.
            # write_atomic opens a temp, writes, fsyncs, then RENAMES; the fixture (an unwritable
            # gates directory) fails at the first step. A failure at the rename would plausibly
            # name `rename` and carry TWO paths, and no fixture here reaches it -- gate-check's
            # own pre-flight read rejects the shapes that would (a directory target, a symlink
            # loop) before any write begins. So the claim is "open, for a failure at the open",
            # not "open for every write failure" -- the same distinction that went wrong when
            # gate_lint's argument was reused at read_state.
            #
            # ELOOP cannot reach node_fs_message from ANY caller: write_atomic, called directly
            # on a symlink-loop target, answers `refusing to replace symlink <path>` in both
            # runtimes -- an authored error with no errno, so it takes the str() branch.
            # Three earlier versions of this note scoped that at one CALLER or another and each
            # added a fresh unmeasured mechanism claim while fixing the previous one's scope. One
            # measurement of the CALLEE answers every caller and was available throughout.
            #     oracle  ... : EACCES: permission denied, open '<file>.<pid>.<hex>.tmp'
            #     port    ... : [Errno 13] Permission denied: '<file>.<pid>.<hex>.tmp'
            # THE PATH IS THE TEMP FILE, NOT `result["file"]`, and that is the durable finding: a
            # differential row here can never compare raw, because the temp name embeds the pid
            # and 8 random bytes, so the two runtimes cannot produce equal text. Any row must
            # scrub `\.[0-9]+\.[0-9a-f]{16}\.tmp` first. Noted rather than left for whoever writes
            # that row to discover as a mystery failure.
            # `Exception`, not OSError, so getattr rather than a bare attribute read: the oracle's
            # catch is equally wide and a non-OSError arriving here has no errno.
            error("gate-check: cannot update " + result["file"] + ": "
                  + (node_fs_message(exc, "open")
                     if getattr(exc, "errno", None) is not None else str(exc)))
            any_lock_write_failed = True
    if any_lock_write_failed:
        sys.exit(2)

    # gate-check.mjs:861-895: reload every ledger fresh (this run's rewrites just landed) and
    # tally MET/UNMET/ABANDONED from that reloaded state -- never from the in-memory `results`.
    ledgers = [load_ledger(f) for f in target["files"]]
    total_met = 0
    total_unmet = 0
    total_abandoned = 0
    reverified = 0
    unmet_ids = []
    abandoned_ids = []
    final_states = {}
    for result in results:
        if (opt.get("reverify") and result["wasMet"]
                and result_key(result["file"], result["gate"]["id"]) not in stale_results):
            reverified += 1

    for ledger in ledgers:
        for gate in ledger["doc"]["gates"]:
            state = gate_state(gate, ledger["doc"]["abandoned"])
            final_states[result_key(ledger["file"], gate["id"])] = state
            if state == "abandoned":
                total_abandoned += 1
                abandoned_ids.append(qualify(ledger["file"], gate["id"]))
            elif state == "met":
                total_met += 1
            else:
                total_unmet += 1
                unmet_ids.append(qualify(ledger["file"], gate["id"]))
                if opt.get("status"):
                    reason = ("unchecked" if state == "unmet"
                              else "checked but EVIDENCE pending" if state == "unmet-no-evidence"
                              else "checked but automatic evidence is stale or unbound")
                    log("  UNMET " + qualify(ledger["file"], gate["id"]) + " (" + reason + "): "
                        + str(gate["title"]))
        log(js_basename(ledger["file"]) + ": " + str(len(ledger["doc"]["gates"])) + " gates")

    # gate-check.mjs:897-910: a scoped pipeline is complete only when both its ledgers and its
    # native dispatch waves are resolved.
    aggregate_dispatch = dispatch_status(root, scope)
    if aggregate_dispatch["errors"]:
        for dispatch_error in aggregate_dispatch["errors"]:
            error("gate-check: " + dispatch_error)
        sys.exit(2)
    total_abandoned += len(aggregate_dispatch["abandoned"])
    abandoned_ids.extend(aggregate_dispatch["abandoned"])
    if opt.get("status"):
        for blocker in aggregate_dispatch["blocking"]:
            log("  UNMET " + blocker)

    # gate-check.mjs:912-950: the final verdict -- extraUnmet folds back in results that were
    # never reverified (still trusted as met, but not actually re-checked this run) and stale
    # results discarded above, then the exit code is chosen from the aggregate.
    where = " [scope " + scope + "]" if scope else ""
    verify_note = (", reran: " + str(len(results)) + ", previously met reverified: " + str(reverified)
                   if opt.get("reverify") else "")
    unverified_met = [task for task in not_run if task["wasMet"]] if opt.get("reverify") else []
    extra_unmet = {}
    for task in unverified_met:
        key = result_key(task["file"], task["gate"]["id"])
        state = final_states.get(key)
        if state == "met" or key not in final_states:
            extra_unmet[key] = qualify(task["file"], task["gate"]["id"]) + " (reverify not run)"
    for key, label in stale_results.items():
        state = final_states.get(key)
        if state == "met" or key not in final_states:
            extra_unmet[key] = label + " (stale result discarded)"
    effective_unmet = total_unmet + len(extra_unmet) + len(aggregate_dispatch["blocking"])
    unmet_ids.extend(extra_unmet.values())
    unmet_ids.extend(aggregate_dispatch["blocking"])
    if approval_infrastructure_failures:
        error("gate-check: infrastructure failure prevented "
              + str(approval_infrastructure_failures) + " approval(s)")
        sys.exit(2)
    if effective_unmet == 0 and total_abandoned == 0:
        log("ALL MET (" + str(total_met) + " met" + verify_note + ")" + where)
        sys.exit(0)
    if total_abandoned:
        log("HANDOFF REQUIRED: " + str(total_abandoned) + " abandoned (met: "
            + str(max(0, total_met - len(extra_unmet)))
            + (", unmet: " + str(effective_unmet) if effective_unmet else "")
            + verify_note + ")" + where)
        log("  " + ", ".join(abandoned_ids[:12])
            + (", +" + str(len(abandoned_ids) - 12) + " more" if len(abandoned_ids) > 12 else ""))
    if effective_unmet:
        log("UNMET: " + str(effective_unmet) + " (met: " + str(max(0, total_met - len(extra_unmet)))
            + (", abandoned: " + str(total_abandoned) if total_abandoned else "")
            + verify_note + ")" + where)
        log("  " + ", ".join(unmet_ids[:12])
            + (", +" + str(len(unmet_ids) - 12) + " more" if len(unmet_ids) > 12 else ""))
    sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
