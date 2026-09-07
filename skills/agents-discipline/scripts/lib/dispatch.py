"""Atomic host-dispatch wave state. Port of `lib/dispatch.mjs`.

The oracle is asynchronous (it awaits `withFileLock`); this is synchronous, which is the only
structural difference. Everything else is a transcription, with the JS-semantics divergences
routed through `jsapi` and marked at each site.

INTEROP, not just parity: during the migration a JS `gate-check` and this module read and write
the SAME `dispatch.json`, append to the same `status.log`, and contend on the same lock file. A
serialization or bounds difference here is a live interop bug, not a test failure -- which is why
the length checks below count UTF-16 code units and the state is re-keyed before it is dumped.
"""

import datetime
import errno
import json
import os
import re
import typing

from gates import (  # type: ignore[import-not-found]
    node_fs_message, append_status, read_stable_regular_file, scope_root, validate_scope_id,
    with_file_lock, write_atomic,
)
from jsapi import (  # type: ignore[import-not-found]
    js_entries, js_json_object, js_length, js_slice, js_trim, parse_date,
)

SCHEMA = 1
MAX_STATE_BYTES = 8 * 1024 * 1024
STATES = frozenset(("open", "sealed", "complete", "abandoned"))

_CONTROL = re.compile(r"[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069]")
# `[\sU+FEFF]`, not `\s`. MEASURED over every plausible whitespace code point: of the four where
# JS and Python disagree, three (U+0085, U+001C, U+001F) are replaced by the CONTROL pass before
# the collapse ever sees them, and exactly one -- U+FEFF -- reaches it. JS calls it whitespace,
# Python does not, and CONTROL does not cover it, so a diagnostic containing a BOM would collapse
# in one runtime and not the other.
_JS_SPACE = re.compile(r"[\s\ufeff]+")
# `.trim()` is jsapi.js_trim, NOT a regex here and NOT str.strip(). This comment used to claim
# ".trim() uses the same JS whitespace set, so one class does both" -- true of the class, and
# false of the code, because the two validators below called str.strip() and never touched it.
# The gap was a shipped defect: an abandon reason of one U+FEFF exited 2 in the oracle and 0
# here. A comment asserting a shared implementation is worth nothing while the callers each roll
# their own; the helper is now the only spelling of trim in this file.


class DispatchError(Exception):
    """The oracle's `fail()`.

    A distinct class rather than a bare Exception because `read_state` must distinguish a
    validation failure it should re-raise unchanged from any other error it wraps -- the oracle
    does that by testing `String(error.message).startsWith("invalid dispatch state:")`, which is
    a string test on a shared type. `str()` on this yields the message alone, with no class name
    and no "[Errno N]" prefix, so the two runtimes print the same diagnostic.
    """


def _fail(message) -> typing.NoReturn:
    # NoReturn is not decoration: without it a checker believes every `_valid_time` call can
    # return None, and the six `<` and max() comparisons below light up as optional-operand
    # errors. It also states the real contract -- _fail never falls through, which is what makes
    # the transcription of the oracle's `fail()` faithful.
    raise DispatchError(message)


def _empty_state():
    return {"schema": SCHEMA, "waves": {}}


def _safe_diagnostic(value):
    # `.slice(0, 500)` is UTF-16 code units, so js_slice rather than [:500]: an emoji-laden
    # message would otherwise be cut at a different point in each runtime. js_slice can return a
    # lone surrogate here, exactly as the oracle can.
    collapsed = _JS_SPACE.sub(" ", _CONTROL.sub(" ", str(value)))
    return js_slice(js_trim(collapsed), 0, 500)


def _valid_id(value, label):
    # `typeof value !== "string"` first, and this is the type check that makes gates.py's
    # documented `validate_scope_id` divergence for non-strings unreachable. Keep it.
    if not isinstance(value, str):
        _fail(label + " must be a string")
    error = validate_scope_id(value, label)
    if error:
        _fail(error)
    return value


def _valid_handle(value):
    if not isinstance(value, str):
        _fail("handle must be a string")
    handle = js_trim(value)
    # js_length, not len(): the oracle bounds this at 256 UTF-16 CODE UNITS. Measured, 200 emoji
    # are 400 units to JS and 200 code points to Python, so `len(...) > 256` would ACCEPT a handle
    # the oracle REJECTS -- and the handle is a wave's uniqueness key, so the two runtimes would
    # disagree about whether the same dispatch.json is valid.
    if not handle or js_length(handle) > 256 or _CONTROL.search(handle):
        _fail("handle must be printable, nonblank, and at most 256 characters")
    return handle


def _valid_reason(value):
    if not isinstance(value, str):
        _fail("reason must be a string")
    reason = js_trim(value)
    if not reason or js_length(reason) > 500 or _CONTROL.search(reason):
        _fail("reason must be printable, nonblank, and at most 500 characters")
    return reason


def _valid_time(value, label):
    parsed = parse_date(value) if isinstance(value, str) else None
    if not value or parsed is None:
        _fail(label + " must be an ISO timestamp")
    return parsed


def validate_state(state):
    """Port of `validateState`. Mutates and returns `state`, as the oracle does."""
    # NO counterpart to the oracle's `record()` / `Object.assign(Object.create(null), ...)`, and
    # that is correct rather than an omission: those exist to strip the PROTOTYPE chain, because
    # `state.waves["toString"]` is otherwise a truthy inherited function and `if
    # (state.waves[waveId])` would refuse to open a wave named `toString` on a fresh state --
    # reachable, since "toString" matches the id pattern. A Python dict has no prototype, so
    # `.get("toString")` is None and the same answer falls out with no code. Recorded because the
    # next reader will look for the missing construct.
    if (not isinstance(state, dict) or isinstance(state, list) or state.get("schema") != SCHEMA
            or not isinstance(state.get("waves"), dict)):
        _fail("expected schema 1 with a waves object")

    # js_entries at all three iteration sites, not just at serialization. Wave and leaf ids may
    # begin with a digit, so "1" is a legal id and enumerates FIRST in JS whatever order it was
    # inserted. These loops decide WHICH error fires first on a state file with two problems, so
    # a plain dict iteration produces a different diagnostic for the same input.
    for wave_id, wave in js_entries(state["waves"]):
        _valid_id(wave_id, "wave")
        if (not isinstance(wave, dict) or wave.get("state") not in STATES
                or not isinstance(wave.get("leaves"), list) or not wave["leaves"]
                or not isinstance(wave.get("started"), dict)
                or not isinstance(wave.get("returned"), dict)):
            _fail("wave " + wave_id + " has an invalid shape")

        opened_at = _valid_time(wave.get("openedAt"), "wave " + wave_id + " openedAt")
        abandoned_at = None
        if wave["state"] == "abandoned":
            abandoned_at = _valid_time(wave.get("abandonedAt"), "wave " + wave_id + " abandonedAt")
            wave["reason"] = _valid_reason(wave.get("reason"))
        elif "abandonedAt" in wave or "reason" in wave:
            _fail(wave["state"] + " wave " + wave_id + " contains abandonment metadata")

        leaves = set()
        for leaf in wave["leaves"]:
            leaf_id = _valid_id(leaf, "leaf")
            if leaf_id in leaves:
                _fail("wave " + wave_id + " has duplicate leaf " + leaf_id)
            leaves.add(leaf_id)

        handles = set()
        start_times = {}
        latest_start = opened_at
        for leaf, start in js_entries(wave["started"]):
            if leaf not in leaves:
                _fail("wave " + wave_id + " started unknown leaf " + leaf)
            if not isinstance(start, dict):
                _fail("wave " + wave_id + " has invalid start for " + leaf)
            handle = _valid_handle(start.get("handle"))
            if handle in handles:
                _fail("wave " + wave_id + " reuses handle " + handle)
            handles.add(handle)
            at = _valid_time(start.get("at"), "wave " + wave_id + " start time for " + leaf)
            if at < opened_at:
                _fail("wave " + wave_id + " starts " + leaf + " before it opened")
            start_times[leaf] = at
            latest_start = max(latest_start, at)

        return_times = []
        for leaf, returned in js_entries(wave["returned"]):
            if leaf not in leaves or not wave["started"].get(leaf):
                _fail("wave " + wave_id + " returned unstarted leaf " + leaf)
            if not isinstance(returned, dict):
                _fail("wave " + wave_id + " has invalid return for " + leaf)
            at = _valid_time(returned.get("at"), "wave " + wave_id + " return time for " + leaf)
            if at < start_times[leaf]:
                _fail("wave " + wave_id + " returns " + leaf + " before it started")
            return_times.append(at)

        all_started = len(wave["started"]) == len(wave["leaves"])
        all_returned = len(wave["returned"]) == len(wave["leaves"])
        if wave["state"] == "open" and wave["returned"]:
            _fail("open wave " + wave_id + " contains returns")
        if wave["state"] not in ("open", "abandoned") and not all_started:
            _fail(wave["state"] + " wave " + wave_id + " is missing starts")
        if wave["state"] == "complete" and not all_returned:
            _fail("complete wave " + wave_id + " is missing returns")
        if wave["state"] == "sealed" and all_returned:
            _fail("sealed wave " + wave_id + " should be complete")

        needs_seal = (wave["state"] in ("sealed", "complete")
                      or (wave["state"] == "abandoned" and "sealedAt" in wave))
        sealed_at = None
        if needs_seal:
            sealed_at = _valid_time(wave.get("sealedAt"), "wave " + wave_id + " sealedAt")
            if not all_started:
                _fail(wave["state"] + " wave " + wave_id + " is missing starts after sealing")
            if sealed_at < latest_start:
                _fail("wave " + wave_id + " was sealed before its final start")
        elif "sealedAt" in wave:
            _fail(wave["state"] + " wave " + wave_id + " contains seal metadata")

        if return_times:
            if sealed_at is None:
                _fail(wave["state"] + " wave " + wave_id + " contains returns without being sealed")
            if any(at < sealed_at for at in return_times):
                _fail("wave " + wave_id + " contains a return before sealing")

        if wave["state"] == "complete":
            completed_at = _valid_time(wave.get("completedAt"), "wave " + wave_id + " completedAt")
            # `Math.max(sealedAt, ...returnTimes)`. The fixed first argument is what keeps this
            # from being `max([])`, which RAISES in Python where Math.max() is -Infinity.
            if sealed_at is None:
                # Unreachable: needs_seal is true whenever state == "complete", so sealedAt was
                # validated above. Stated rather than assumed -- a checker cannot follow that
                # invariant, and substituting a silent fallback here would mask a real break in
                # it rather than surface one.
                _fail("complete wave " + wave_id + " is missing seal metadata")
            if completed_at < max([sealed_at] + return_times):
                _fail("wave " + wave_id + " completed before its final return")
        elif "completedAt" in wave:
            _fail(wave["state"] + " wave " + wave_id + " contains completion metadata")

        if wave["state"] == "abandoned":
            if all_returned:
                _fail("abandoned wave " + wave_id + " already has every return and must be complete")
            # The oracle's `sealedAt || openedAt` is deliberate and load-bearing: sealed_at is
            # None for an abandoned wave that was never sealed, and passing that to max() is a
            # TypeError in Python where JS would coerce.
            floor = max([opened_at, latest_start, sealed_at if sealed_at else opened_at] + return_times)
            if abandoned_at < floor:
                _fail("wave " + wave_id + " was abandoned before its latest transition")
    return state


def dispatch_state_path(root, scope):
    return os.path.join(scope_root(os.path.abspath(root), _valid_id(scope, "scope")),
                        "dispatch.json")


def read_state(root, path):
    try:
        text = read_stable_regular_file(path, root=os.path.abspath(root),
                                        max_bytes=MAX_STATE_BYTES, label="dispatch state")
        return validate_state(json.loads(text))
    except OSError as error:
        # ENOENT means "no waves yet", which is why gates.py had to grow the oracle's ENOENT
        # PROBE: without it, a file SWAPPED between open and stat also arrived here as ENOENT and
        # this branch would have returned an empty wave set on a race the oracle refuses.
        if error.errno == errno.ENOENT:
            return _empty_state()
        # NOT _err_code(error), and not a bare str() either. The two error classes arriving here
        # need OPPOSITE treatment, which is why one expression cannot be picked by class:
        #   AUTHORED OSError (the size cap, the containment refusal) -- errno is None, and str()
        #     is the bare message the oracle throws verbatim. Exact match.
        #   GENUINE SYSCALL ERROR -- the oracle interpolates node's `error.message`, which
        #     str() does not reproduce and _err_code does not either. MEASURED on an unreadable
        #     dispatch.json:
        #         oracle  invalid dispatch state: EACCES: permission denied, open '<path>'
        #         port    invalid dispatch state: [Errno 13] Permission denied: '<path>'
        # THIS COMMENT USED TO CALL THAT DIVERGENCE OUT OF SCOPE -- "three different strings,
        # none reproducing the oracle", deferring to the drivers' errno-NAME comparison. Honest
        # when written, and wrong now: node_fs_message reproduces node's shape exactly for this
        # class, and 9071a84 (gate-lint), ledger_check.py:96 and this line are three instances of
        # one defect that were each found separately because the first fix was not swept. A
        # deferral whose premise was "nothing can reproduce it" has to be revisited the day
        # something can, and nothing re-reads a deferral on its own.
        # `open` IS THE SYSCALL, but NOT for the reason gate_lint's site gives -- that argument
        # does not transfer and reusing it was wrong. gate_lint's try wraps one call and passes NO
        # root, so the open is genuinely the only syscall that can raise to it. THIS try passes
        # `root=`, which makes read_stable_regular_file realpath(strict=True) the ROOT after the
        # open, and that CAN raise ENOENT/EACCES naming lstat. MEASURED that the vector is real:
        # calling read_state directly with a symlink-loop root and an unrelated readable target
        # produces `ELOOP: too many levels of symbolic links` from that stage.
        # WHAT ACTUALLY CLOSES IT IS THE CALL GRAPH, not the helper: all three read_state callers
        # pass `dispatch_state_path(root, scope)`, which is BUILT from the same root, so the
        # target is always the root's descendant. If the open succeeded, the root resolved. The
        # probe above only reached that stage because it handed the function a root the target
        # does not live under -- a shape no caller can produce. Same residual as gate_lint's, and
        # no larger: a concurrent mutation between the open and the realpath.
        # (json.loads raises ValueError and validate_state raises DispatchError; neither is an
        # OSError, so the wider try adds no other errno-bearing path.)
        raise DispatchError(
            "invalid dispatch state: "
            + (node_fs_message(error, "open") if error.errno is not None else str(error))
        ) from error
    except DispatchError as error:
        # The oracle re-raises ONLY when the message ALREADY carries the prefix, and wraps
        # everything else -- including a fresh validate_state failure, whose message never does.
        # Passing DispatchError straight through was wrong: measured, the oracle reports
        # "invalid dispatch state for scope bad: invalid dispatch state: expected schema 1 ..."
        # and the port dropped the inner prefix on all ten malformed-state rows. The doubling
        # looks redundant but it IS the oracle's output, and dispatch-check prints it verbatim.
        if str(error).startswith("invalid dispatch state:"):
            raise
        raise DispatchError("invalid dispatch state: " + str(error)) from error
    except (ValueError, TypeError, KeyError) as error:
        raise DispatchError("invalid dispatch state: " + str(error)) from error


def get_dispatch_wave(root, scope, wave_id):
    wave = _valid_id(wave_id, "wave")
    state = read_state(root, dispatch_state_path(root, scope))
    if not state["waves"].get(wave):
        _fail("unknown wave " + wave)
    return state["waves"][wave]


def dispatch_issues(root, scope):
    return dispatch_status(root, scope)["blocking"]


def dispatch_status(root, scope):
    if not scope:
        return {"blocking": [], "abandoned": [], "resolved": [], "errors": []}
    try:
        state = read_state(root, dispatch_state_path(root, scope))
    except Exception as error:  # noqa: BLE001 -- the oracle's catch is equally wide
        message = _safe_diagnostic(str(error))
        return {
            "blocking": ["dispatch:PARSE invalid dispatch state"],
            "abandoned": [],
            "resolved": ["dispatch:PARSE=invalid"],
            "errors": ["invalid dispatch state for scope " + scope + ": " + message],
        }

    result = {"blocking": [], "abandoned": [], "resolved": [], "errors": []}
    # `.sort(([left], [right]) => left.localeCompare(right))` -- ICU collation, not code point
    # order, so this goes through locale_compare_key rather than sorted()'s default.
    from jsapi import locale_compare_key  # type: ignore[import-not-found]
    # js_entries as the INPUT, not .items(): both sorts are stable, so the input order is the
    # tie-break. Ties are believed impossible over the id charset, but that belief rests on a
    # hand-built collation key with a documented non-ICU fallback -- one word removes the
    # dependency on the argument holding.
    for wave_id, wave in sorted(js_entries(state["waves"]),
                                key=lambda kv: locale_compare_key(kv[0])):
        started = len(wave["started"])
        returned = len(wave["returned"])
        total = len(wave["leaves"])
        result["resolved"].append(
            "dispatch:" + wave_id + "=" + wave["state"] + ";started=" + str(started) + "/" +
            str(total) + ";returned=" + str(returned) + "/" + str(total))
        if wave["state"] == "complete":
            continue
        if wave["state"] == "abandoned":
            result["abandoned"].append("dispatch:" + wave_id)
        elif wave["state"] == "open":
            result["blocking"].append(
                "dispatch:" + wave_id + " open (" + str(started) + "/" + str(total) + " started)")
        else:
            result["blocking"].append(
                "dispatch:" + wave_id + " sealed (" + str(returned) + "/" + str(total) +
                " returned)")
    return result


def update_dispatch(root, spec):
    scope = _valid_id(spec.get("scope"), "scope")
    wave_id = _valid_id(spec.get("wave"), "wave")
    path = dispatch_state_path(root, scope)
    event = {"text": ""}

    def transaction():
        state = read_state(root, path)
        stamp = spec.get("now") or _iso_now()
        _valid_time(stamp, "timestamp")
        action = spec.get("action")

        if action == "open":
            if state["waves"].get(wave_id):
                _fail("wave " + wave_id + " already exists")
            leaves = [_valid_id(leaf, "leaf") for leaf in (spec.get("leaves") or [])]
            if not leaves:
                _fail("open requires at least one --leaf")
            seen = set()
            for leaf in leaves:
                if leaf in seen:
                    _fail("duplicate leaf " + leaf)
                seen.add(leaf)
            state["waves"][wave_id] = {"leaves": leaves, "state": "open", "openedAt": stamp,
                                       "started": {}, "returned": {}}
            event["text"] = "dispatch " + wave_id + " opened: " + ", ".join(leaves)
        else:
            current = state["waves"].get(wave_id)
            if not current:
                _fail("unknown wave " + wave_id)
            if action == "abandon":
                if current["state"] in ("complete", "abandoned"):
                    _fail("wave " + wave_id + " is " + current["state"] +
                          "; abandon requires an open or sealed wave")
                current["state"] = "abandoned"
                current["reason"] = _valid_reason(spec.get("reason"))
                current["abandonedAt"] = stamp
                event["text"] = "dispatch " + wave_id + " abandoned: " + current["reason"]
            elif action == "start":
                leaf = _valid_id(spec.get("leaf"), "leaf")
                handle = _valid_handle(spec.get("handle"))
                if current["state"] != "open":
                    _fail("wave " + wave_id + " is " + current["state"] +
                          "; start requires an open wave")
                if leaf not in current["leaves"]:
                    _fail("unknown leaf " + leaf + " in wave " + wave_id)
                if current["started"].get(leaf):
                    _fail("leaf " + leaf + " already started")
                owner = next((name for name, start in js_entries(current["started"])
                              if start.get("handle") == handle), None)
                if owner:
                    _fail("handle is already assigned to " + owner)
                current["started"][leaf] = {"handle": handle, "at": stamp}
                event["text"] = "dispatch " + wave_id + " started " + leaf + " as " + handle
            elif action == "seal":
                if current["state"] != "open":
                    _fail("wave " + wave_id + " is " + current["state"] +
                          "; seal requires an open wave")
                missing = [leaf for leaf in current["leaves"] if not current["started"].get(leaf)]
                if missing:
                    _fail("cannot seal " + wave_id + ": missing starts for " + ", ".join(missing))
                current["state"] = "sealed"
                current["sealedAt"] = stamp
                event["text"] = "dispatch " + wave_id + " sealed"
            elif action == "return":
                leaf = _valid_id(spec.get("leaf"), "leaf")
                if current["state"] != "sealed":
                    _fail("return requires a sealed wave; " + wave_id + " is " + current["state"])
                if leaf not in current["leaves"]:
                    _fail("unknown leaf " + leaf + " in wave " + wave_id)
                if current["returned"].get(leaf):
                    _fail("leaf " + leaf + " already returned")
                current["returned"][leaf] = {"at": stamp}
                if len(current["returned"]) == len(current["leaves"]):
                    current["state"] = "complete"
                    current["completedAt"] = stamp
                event["text"] = "dispatch " + wave_id + " returned " + leaf
            else:
                _fail("unknown dispatch action " + str(action))

        validate_state(state)
        # js_json_object, so the file's KEY ORDER matches the oracle's byte for byte. A wave
        # named "1" enumerates first in JS whatever order it was inserted, and both runtimes
        # write this same file during the migration.
        # A bare json.dumps here, NOT gates._js_json_text. THE EXEMPTION STANDS, BUT ITS ORIGINAL
        # REASONING WAS WRONG ON BOTH HALVES AND THAT COST TWO SHIPPED CRASHES. It claimed "every
        # string in `state` is an id or a handle, and both gates are charset-closed":
        #   * `reason` is neither -- it is FREE TEXT, gated only by _valid_reason's control-char
        #     and length checks. `--reason <one invalid argv byte>` exited 0 in the oracle and
        #     died here with UnicodeEncodeError, mid-transaction.
        #   * `handle` is free text too, and the validator this comment NAMED IS NOT THE ONE THAT
        #     RUNS. It said str.isprintable() (False for every surrogate -- true, and irrelevant);
        #     _valid_handle uses `_CONTROL.search(handle)`, and surrogates are outside that class.
        #     So the handle crashed here as well, at the same line, unlooked-for because this
        #     comment said it could not.
        # A comment that names a validator has to name the one the code CALLS. This one would
        # have told anyone who checked that the handle path was safe -- which is a hazard whether
        # or not it is why the second crash site went unlooked-for (no test drove `--handle` with
        # bad bytes either, and that is the duller and likelier explanation).
        #
        # AND THE FIX IS NOT TO ADD A SURROGATE GATE to _valid_handle or _valid_reason. MEASURED
        # rather than inferred from node's decode, because "which validator runs" is exactly what
        # this comment got wrong twice: the ORACLE accepts a literal U+FFFD handle -- exit 0,
        # `"handle": "h<U+FFFD>nd"` written -- so a port-only surrogate rejection would be a FRESH
        # divergence dressed as hardening. What actually makes this line safe now is upstream and
        # unconditional: jsapi.normalize_argv() runs before any parsing in all four CLIs, so an
        # argv value carrying invalid UTF-8 arrives as U+FFFD (node's own spelling) and no lone
        # surrogate can reach `state` from a command line at all. Covered by tests/argv-diff.sh.
        #
        # THE READ-BACK PATH IS NOT A SECOND HOLE, and it is the first place to look because
        # `state` is loaded from this same file, mutated and rewritten: a bad byte sitting in
        # dispatch.json flows through this line on the NEXT transaction. It is closed by the
        # reader, on both sides -- read_stable_regular_file decodes `errors="replace"` (gates.py
        # :395) and node's Buffer.toString("utf8") substitutes identically, so a raw byte in the
        # file is U+FFFD by the time it is parsed, never a surrogate. Verified by injecting one
        # and reading it back.
        # SO THE RESIDUAL IS NARROW AND WORTH STATING AS SUCH: a free-text value reaching `state`
        # from a source that is NEITHER argv NOR that reader -- an env var, os.listdir, anything
        # CPython decodes with surrogateescape. There is no such field today.
        # The ONLY reason not to route this through _js_json_text is the one that survives:
        # JSON.stringify ESCAPES a lone surrogate to six ASCII characters while the oracle here
        # writes the CHARACTER -- measured, so the helper would trade a crash for a silent byte
        # divergence in the file both runtimes read.
        write_atomic(path, json.dumps(js_json_object(state), indent=2, ensure_ascii=False) + "\n",
                     root=root)
        return state["waves"][wave_id]

    wave = with_file_lock(root, path, transaction)

    log_warning = ""
    try:
        append_status(root, scope, _iso_now() + " " + event["text"])
    except Exception as error:  # noqa: BLE001 -- matches the oracle's catch
        log_warning = ("state transition committed; audit status append was skipped: " +
                       _safe_diagnostic(str(error) or error))
    return {"wave": wave, "logWarning": log_warning}


def _iso_now():
    """`new Date().toISOString()` -- UTC, exactly three fractional digits, trailing Z.

    ONE `now()` call. The first draft called it twice, taking the seconds from a UTC clock and
    the milliseconds from a second, LOCAL-time call -- so the fraction came from a different
    instant than the rest of the stamp, and on a sub-millisecond boundary from a different
    second entirely. isoformat(timespec="milliseconds") gives the same shape without the seam,
    and `+00:00` becomes `Z` to match toISOString exactly.
    """
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
