#!/usr/bin/env python3
"""Records and checks the all-starts-before-wait dispatch contract. Port of dispatch-check.mjs.

terminal_safe is DUPLICATED here rather than imported, which matches the oracle: gate-lint.mjs
and dispatch-check.mjs each carry their own copy, behaviourally identical but with different
defaults (1024 vs 500 bytes). The port could not share one anyway -- gate_lint.py has no
__main__ guard, so importing it runs the CLI and exits.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from dispatch import (  # noqa: E402  # type: ignore[import-not-found]
    DispatchError, get_dispatch_wave, update_dispatch,
)
from gates import node_fs_message  # noqa: E402  # type: ignore[import-not-found]
from jsapi import (  # noqa: E402  # type: ignore[import-not-found]
    force_utf8_streams, js_trim, normalize_argv)

# BEFORE anything can print -- see the function's docstring.
#
# UNLIKE the other three entry points, this one is PROPHYLACTIC and says so. gate_check,
# gate_lint and ledger_check each have a MEASURED non-ASCII vector that degraded or crashed under
# an ASCII stdout. For dispatch_check I looked and did not find one: leaf ids are ASCII by
# validation (`/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/`, and both runtimes reject a non-ASCII leaf
# with the identical message), and a non-ASCII abandon reason did not reach the `status` report
# in either runtime. The call is here because every ported CLI should differ from node in the
# same way -- none -- and because a vector I could not construct today is not a vector that does
# not exist. Stated as prophylaxis rather than smuggled in under the others' evidence: the first
# version of this comment asserted a reason-and-leaf-id vector that I had not measured, which is
# the exact habit the rest of this port's comments exist to correct.
force_utf8_streams()

# The vector the comment above says it could not construct EXISTS, and it is on this CLI. An
# abandon reason carrying one bad argv byte: the oracle exits 0 and writes U+FFFD, the port died
# with UnicodeEncodeError inside write_atomic -- a crash MID-TRANSACTION on state both runtimes
# read. The cause is upstream of every write: CPython and node decode argv differently. Fixed at
# the boundary, so no destination downstream can receive a surrogate from an argument.
normalize_argv()

COMMANDS = ("open", "start", "seal", "return", "abandon", "status")
OPTIONS = ("--root", "--scope", "--wave", "--leaf", "--handle", "--reason")
TRUNCATION_MARKER = "...[truncated]"
import re  # noqa: E402
_UNSAFE_TERMINAL = re.compile(r"[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069]")


def usage():
    return "\n".join([
        "Usage:",
        "  dispatch-check.mjs open --scope ID --wave ID --leaf ID [--leaf ID ...] [--root PATH]",
        "  dispatch-check.mjs start --scope ID --wave ID --leaf ID --handle OPAQUE_ID [--root PATH]",
        "  dispatch-check.mjs seal --scope ID --wave ID [--root PATH]",
        "  dispatch-check.mjs return --scope ID --wave ID --leaf ID [--root PATH]",
        "  dispatch-check.mjs abandon --scope ID --wave ID --reason TEXT [--root PATH]",
        "  dispatch-check.mjs status --scope ID --wave ID [--root PATH]",
    ])


def terminal_safe(value, max_bytes=500):
    """Escape terminal-unsafe characters and truncate on a BYTE budget.

    `for (const character of String(value))` iterates JS strings by CODE POINT, so a plain
    Python `for c in str(value)` matches -- this is the one place the UTF-16 code-unit rule does
    NOT apply, because the string iterator is the code-point one.
    """
    pieces = []
    sizes = []
    total = 0
    truncated = False
    for character in str(value):
        piece = character
        if _UNSAFE_TERMINAL.search(character):
            code = ord(character)
            piece = ("\\x" + format(code, "02x")) if code <= 0xFF else ("\\u" + format(code, "04x"))
        elif 0xD800 <= ord(character) <= 0xDFFF:
            # A LONE SURROGATE. Same defect and same fix as gate_lint.py's terminal_safe -- see
            # that copy for the measurement. It is not matched by _UNSAFE_TERMINAL and is not
            # UTF-8-encodable, so the size computation below raised. node substitutes U+FFFD and counts
            # it as 3 bytes (measured), so chr(0xFFFD) reproduces both the text and the budget.
            piece = chr(0xFFFD)
        size = len(piece.encode("utf-8"))
        if total + size > max_bytes:
            truncated = True
            break
        pieces.append(piece)
        sizes.append(size)
        total += size
    if not truncated:
        return "".join(pieces)
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    while pieces and total + marker_bytes > max_bytes:
        pieces.pop()
        total -= sizes.pop()
    return "".join(pieces) + TRUNCATION_MARKER


def die(message, show_usage=False):
    print("agents-discipline dispatch: " + terminal_safe(message), file=sys.stderr)
    if show_usage:
        print(usage(), file=sys.stderr)
    sys.exit(2)


def summary(wave, wave_id):
    started = len(wave["started"])
    returned = len(wave["returned"])
    total = len(wave["leaves"])
    if wave["state"] == "complete":
        return "COMPLETE " + wave_id + " (" + str(returned) + "/" + str(total) + " returned)"
    if wave["state"] == "abandoned":
        return ("ABANDONED " + wave_id + " (" + str(started) + "/" + str(total) +
                " started, " + str(returned) + "/" + str(total) + " returned): " +
                terminal_safe(wave["reason"]))
    return (wave["state"].upper() + " " + wave_id + " (" + str(started) + "/" + str(total) +
            " started, " + str(returned) + "/" + str(total) + " returned)")


def main(argv):
    args = list(argv)
    if not args or args[0] in ("--help", "-h"):
        print(usage())
        sys.exit(0 if args else 2)

    command = args.pop(0)
    if command not in COMMANDS:
        die("unknown command " + command, True)

    options = {"root": os.getcwd(), "scope": None, "wave": None, "leaves": [],
               "handle": None, "reason": None}
    single = set()
    while args:
        option = args.pop(0)
        if option not in OPTIONS:
            die("unknown option " + option)
        if not args or args[0].startswith("--"):
            die(option + " requires a value")
        value = args.pop(0)
        if option == "--leaf":
            options["leaves"].append(value)
        else:
            if option in single:
                die(option + " may be provided only once")
            single.add(option)
            options[option[2:]] = value

    if not options["scope"]:
        die("--scope is required")
    if not options["wave"]:
        die("--wave is required")
    options["root"] = os.path.abspath(options["root"])

    if command == "open":
        if not options["leaves"]:
            die("open requires at least one --leaf")
        if options["handle"] is not None or options["reason"] is not None:
            die("open does not accept --handle or --reason")
    elif command == "start":
        if len(options["leaves"]) != 1:
            die("start requires exactly one --leaf")
        if options["handle"] is None:
            die("start requires --handle")
        if options["reason"] is not None:
            die("start does not accept --reason")
    elif command == "return":
        if len(options["leaves"]) != 1:
            die("return requires exactly one --leaf")
        if options["handle"] is not None or options["reason"] is not None:
            die("return does not accept --handle or --reason")
    elif command == "abandon":
        if options["leaves"] or options["handle"] is not None:
            die("abandon does not accept --leaf or --handle")
        # js_trim, not .strip(): the oracle's `!options.reason.trim()` rejects a reason of one
        # U+FEFF and .strip() does not, so this guard let an abandon through that the oracle
        # refused -- exit 0 against exit 2, with the BOM written into the shared dispatch.json.
        if options["reason"] is None or not js_trim(options["reason"]):
            die("abandon requires --reason")
    elif options["leaves"] or options["handle"] is not None or options["reason"] is not None:
        die(command + " does not accept --leaf, --handle, or --reason")

    try:
        if command == "status":
            wave = get_dispatch_wave(options["root"], options["scope"], options["wave"])
            print(summary(wave, options["wave"]))
            sys.exit(0 if wave["state"] == "complete" else 1)

        result = update_dispatch(options["root"], {
            "action": command,
            "scope": options["scope"],
            "wave": options["wave"],
            "leaves": options["leaves"],
            "leaf": options["leaves"][0] if options["leaves"] else None,
            "handle": options["handle"],
            "reason": options["reason"],
        })
        wave, log_warning = result["wave"], result["logWarning"]
        if log_warning:
            print("agents-discipline dispatch: warning: " + terminal_safe(log_warning),
                  file=sys.stderr)
        started = len(wave["started"])
        returned = len(wave["returned"])
        total = len(wave["leaves"])
        if command == "open":
            print("OPEN " + options["wave"] + " (0/" + str(total) + " started, 0/" +
                  str(total) + " returned)")
        elif command == "start":
            print("STARTED " + options["wave"] + " " + options["leaves"][0] + " (" +
                  str(started) + "/" + str(total) + " started)")
        elif command == "seal":
            print("SEALED " + options["wave"] + " (" + str(started) + "/" + str(total) +
                  " started)")
        elif command == "abandon":
            print(summary(wave, options["wave"]))
        elif wave["state"] == "complete":
            print("COMPLETE " + options["wave"] + " (" + str(returned) + "/" + str(total) +
                  " returned)")
        else:
            print("RETURNED " + options["wave"] + " " + options["leaves"][0] + " (" +
                  str(returned) + "/" + str(total) + " returned)")
    except (DispatchError, OSError) as error:
        # The oracle catches EVERY throw here and reports `error.message`. Narrowed to the two
        # classes this can actually raise, so a genuine bug in the port surfaces as a traceback
        # rather than being reported as a user-facing validation failure with exit 2.
        #
        # THE OSError HALF IS PROPHYLAXIS, and saying so is the point. The two classes need
        # OPPOSITE treatment -- a DispatchError already CARRIES the oracle's own message text,
        # while an OSError is an fs error whose node shape str() does not reproduce -- and the
        # `errno is not None` guard is what lets one expression serve both.
        #
        # BUT NO MEASURED VECTOR REACHES THE OSError BRANCH HERE. The obvious one (an unreadable
        # dispatch.json) is WRAPPED by read_state into a DispatchError before it can arrive; that
        # is where the real fix went, and putting the guard here first is what revealed it -- the
        # row still diverged after the edit. What is left is the paths read_state does not wrap:
        # write_atomic and with_file_lock. An uncaught UnicodeEncodeError from write_atomic HAS
        # been observed escaping update_dispatch to this handler, so exceptions do get out
        # unwrapped; whether an errno-bearing OSError specifically does is UNMEASURED.
        # Labelled rather than left to read as covered, which is the standard argv-diff.sh's
        # own prophylaxis block set one commit earlier and this site did not meet.
        die(node_fs_message(error, "open")
            if getattr(error, "errno", None) is not None else str(error))


if __name__ == "__main__":
    main(sys.argv[1:])
