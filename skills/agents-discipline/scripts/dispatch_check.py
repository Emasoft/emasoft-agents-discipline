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
from jsapi import force_utf8_streams, js_trim  # noqa: E402  # type: ignore[import-not-found]

# BEFORE anything can print -- see the function's docstring. This script's wave reports carry
# leaf ids and abandon reasons straight from dispatch.json, so a non-ASCII one reaches stdout.
force_utf8_streams()

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
        die(str(error))


if __name__ == "__main__":
    main(sys.argv[1:])
