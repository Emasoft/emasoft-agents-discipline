"""Shared helpers for the Python ports of the agents-discipline scripts.

Port of the corresponding half of `lib/gates.mjs`. Grows one function at a time as each
script is ported; keeping the same module name means no later rename.
"""

import json
import os
import re
import stat as statmod

DEFAULT_STABLE_FILE_MAX_BYTES = 8 * 1024 * 1024


def _assert_regular_single_link(st, target, label, max_bytes):
    # A FIFO opened O_NONBLOCK returns a descriptor at once and then blocks forever on read,
    # which is the hang this assertion exists to turn into an error. nlink != 1 means the
    # bytes are reachable under another name, so "the file I checked" is not a single thing.
    if not statmod.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise OSError(f"{label} must be one unchanged regular single-link file: {target}")
    if st.st_size > max_bytes:
        raise OSError(f"{label} exceeds {max_bytes} bytes: {target}")


def read_stable_regular_file(path, max_bytes=None, label="file"):
    """Read a regular file under a size cap, refusing symlinks, FIFOs and mid-read changes.

    O_NOFOLLOW refuses a symlinked target outright; O_NONBLOCK keeps the open from blocking
    on a FIFO so the descriptor can be rejected by its type rather than waited on. The
    before/after stat pair makes a file that changes under the read an error instead of a
    silently half-old string.

    NOT PORTED YET: the oracle also takes a `root` and refuses a target whose realpath falls
    outside it (`pathIsInside`). No caller needs it yet -- the ledger passes no root -- but
    gate-check passes one for lease records, so this must land before that port, or the
    containment guarantee silently disappears at the call site that actually relies on it.
    """
    target = os.path.abspath(path)
    limit = DEFAULT_STABLE_FILE_MAX_BYTES if max_bytes is None else int(max_bytes)
    if limit < 1:
        raise OSError(f"{label} max_bytes must be a positive integer")

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as err:
        if err.errno == getattr(__import__("errno"), "ELOOP", None):
            raise OSError(f"{label} must be one unchanged regular single-link file: {target}") from err
        raise

    try:
        opened = os.fstat(fd)
        named = os.lstat(target)
        _assert_regular_single_link(opened, target, label, limit)
        # The descriptor and the NAME can already be different files: something may have
        # swapped the path between the open and now. Comparing the fd's identity against the
        # path's is what makes "the file I checked" and "the file at this path" one claim.
        if (opened.st_ino, opened.st_dev) != (named.st_ino, named.st_dev):
            raise OSError(f"{label} changed before it was read: {target}")
        canonical_before = os.path.realpath(target)

        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OSError(f"{label} exceeds {limit} bytes: {target}")

        after = os.fstat(fd)
        _assert_regular_single_link(after, target, label, limit)
        if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (
            opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns
        ):
            raise OSError(f"{label} changed while it was read: {target}")
        if os.path.realpath(target) != canonical_before:
            raise OSError(f"{label} changed canonical location while it was read: {target}")
        # errors="replace", NOT strict. A ledger is prose a human pasted into, so one byte of
        # it is routinely not UTF-8 -- a latin-1 accent, a smart quote out of a word processor.
        # Node's Buffer.toString("utf8") substitutes U+FFFD and carries on; a strict decode
        # raises instead, and the caller then reports "cannot read" (exit 2, "not a ledger")
        # on a file that is visibly a ledger, over a byte that is almost never in the row
        # under test. Measured: `caf\xe9` in a unit name turned a complete ledger into exit 2.
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


# Port of the gates.mjs parsing half: parse_gates, normalize_owns_glob and parse_regex.
# `$` in JS does not match before a trailing newline; Python's does -- every line-anchor
# below uses `\Z` instead of `$` so a line ending in whitespace-that-looks-like-a-newline
# cannot silently match one extra position. None of these patterns mix `\d`/`\b` with `\s`,
# so no ASCII-vs-Unicode divergence applies here (see gate_lint.py for one that does).
_GATE_RE = re.compile(r"^- \[( |x|X)\] (.*)\Z")
_ATTR_RE = re.compile(r"^(\s+)(CHECK|EXPECT|EVIDENCE|CWD):\s?(.*)\Z")
_UNINDENTED_ATTR_RE = re.compile(r"^(CHECK|EXPECT|EVIDENCE|CWD):\s?(.*)\Z")
_ABANDON_RE = re.compile(r"^ABANDON:\s*(\S*)\s*(.*)\Z")
_INDENTED_ABANDON_RE = re.compile(r"^\s+ABANDON:")
_OWNS_RE = re.compile(r"^OWNS:\s*(.*)\Z")
_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)\Z")
_FENCE_CLOSE_RE = re.compile(r"^( {0,3})(`+|~+)[ \t]*\Z")
_REGEX_RE = re.compile(r"^/([\s\S]*)/([a-z]*)\Z")
# A pattern author escapes an inner slash or has none. A literal path always
# carries one, so an unescaped inner slash marks the ambiguous reading.
_UNESCAPED_SLASH_RE = re.compile(r"(^|[^\\])/")
_ID_MATCH_RE = re.compile(r"^(\S+?):(?:\s+|\Z)")
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_HASH_OR_LIST_RE = re.compile(r"^#|^- ")
_TRAILING_COLON_RE = re.compile(r":\Z")

# JS RegExp flags mapped to Python's re flags, same table as regex-worker.mjs's port: `g`/`y`/`d`
# only affect stateful/indexed matching (irrelevant to a one-shot syntax check) and `u` is
# Python's default (Unicode) mode.
_JS_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


def parse_regex(expect):
    """Read an EXPECT value as `/source/flags` or a plain literal.

    Mirrors gates.mjs's (module-private) parseRegex: validates the regex compiles and
    flags whether an unescaped slash suggests the author meant a literal path.
    """
    text = str(expect)
    match = _REGEX_RE.match(text)
    if not match:
        return {"kind": "text", "value": text}
    source, flags = match.group(1), match.group(2)
    if len(source) > 1000:
        return {"error": "EXPECT regex is longer than 1000 characters"}
    try:
        # Matching happens in a disposable worker (regex_worker.py) so catastrophic
        # backtracking cannot hang the checker; this call only validates syntax.
        py_flags = re.ASCII
        for flag_char in flags:
            py_flags |= _JS_FLAG_MAP.get(flag_char, 0)
        re.compile(source, py_flags)
    except re.error as error:
        return {"error": "invalid EXPECT regex: " + str(error)}
    return {
        "kind": "regex",
        "source": source,
        "flags": flags,
        "pathLike": bool(_UNESCAPED_SLASH_RE.search(source)),
    }


def normalize_owns_glob(value):
    """Canonicalize an OWNS path: relative, backslash-free, no traversal, no implicit root."""
    raw = str(value or "").strip().replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]
    if not raw:
        return {"error": "OWNS path is blank"}
    if os.path.isabs(raw) or re.match(r"^[A-Za-z]:/", raw) or raw.startswith("//"):
        return {"error": "OWNS path must be relative: " + str(value)}
    parts = raw.split("/")
    if "\0" in raw or any(part == ".." for part in parts):
        return {"error": "OWNS path cannot contain traversal: " + str(value)}
    normalized = "/".join(part for part in parts if part not in ("", "."))
    if not normalized or normalized == ".":
        return {"error": "OWNS path cannot claim an implicit root"}
    return {"value": normalized}


# The checker consumes this exact result shape. Diagnostics are returned together so
# callers can report all malformed input in one pass.
def parse_gates(text, options=None):
    options = options or {}
    source = str(text)
    eol = "\r\n" if "\r\n" in source else "\n"
    final_newline = source.endswith("\n")
    lines = re.split(r"\r?\n", source)
    gates = []
    abandoned = {}
    owns = []
    errors = []
    # Parse-time only, keyed by gate index. Deliberately NOT a field on the gate dicts: see
    # the note where it is populated.
    seen_attrs = {}
    warnings = []
    ids = {}
    current = None
    seen_gate = False
    fence = None

    for index, line in enumerate(lines):
        if fence is not None:
            close = _FENCE_CLOSE_RE.match(line)
            if close and close.group(2)[0] == fence["character"] and len(close.group(2)) >= fence["length"]:
                fence = None
            continue
        fence_match = _FENCE_OPEN_RE.match(line)
        if fence_match and not (fence_match.group(2)[0] == "`" and "`" in fence_match.group(3)):
            fence = {"character": fence_match.group(2)[0], "length": len(fence_match.group(2))}
            continue

        gate_match = _GATE_RE.match(line)
        if gate_match:
            seen_gate = True
            raw_title = gate_match.group(2).strip()
            id_match = _ID_MATCH_RE.match(raw_title)
            gate_id = id_match.group(1) if id_match else "L" + str(index + 1)
            title = raw_title[len(id_match.group(0)):].strip() if id_match else raw_title
            current = {
                "line": index,
                "checked": gate_match.group(1).lower() == "x",
                "id": gate_id,
                "title": title,
                "check": None,
                "expect": None,
                "evidence": None,
                "evidenceLine": -1,
                "cwd": None,
            }
            gates.append(current)
            # OUTSIDE the gate dict, keyed by its index. gates.mjs tracks "which attrs seen"
            # in a WeakMap, so its gate objects carry exactly the fields above; an earlier
            # port put the set INSIDE the dict, which added a key the oracle has no
            # counterpart for -- and a set, which json.dumps cannot encode at all. Any
            # consumer that serialised or iterated a gate would have crashed or diverged, and
            # gate-check writes receipts. The comparison driver was stripping the key, which
            # hid the shape difference instead of removing it.
            seen_attrs[len(gates) - 1] = set()
            if not id_match:
                errors.append("line " + str(index + 1) + ": gate needs an explicit ID followed by a colon")
            elif not _VALID_ID_RE.match(gate_id):
                errors.append("line " + str(index + 1) + ": invalid gate id " + gate_id)
            if not title:
                errors.append("line " + str(index + 1) + ": gate outcome is blank")
            if gate_id in ids:
                errors.append("line " + str(index + 1) + ": duplicate gate id " + gate_id +
                               " (first declared on line " + str(ids[gate_id]) + ")")
            else:
                ids[gate_id] = index + 1
            continue

        # Attributes must be indented and ABANDON must not be, so the two rules point
        # opposite ways. Diagnose the indented abandonment rather than ignoring it, or
        # the author's honest exit fails with no explanation.
        if _INDENTED_ABANDON_RE.match(line):
            errors.append("line " + str(index + 1) +
                           ": indented ABANDON is not applied; start ABANDON at column 1")
            current = None
            continue

        unindented = _UNINDENTED_ATTR_RE.match(line)
        if unindented:
            errors.append("line " + str(index + 1) + ": unindented " + unindented.group(1) +
                           " is not attached to a gate; indent attribute lines with spaces")
            current = None
            continue

        any_attr = _ATTR_RE.match(line)
        if any_attr and current is None:
            errors.append("line " + str(index + 1) + ": orphan " + any_attr.group(2) + " is not attached to a gate")
            continue
        # `and current is not None` inline rather than folded into a nullable local: the
        # guard is identical either way at runtime, but a type checker cannot narrow `current`
        # through the indirection and reports six false subscript errors inside this block.
        # A checker that cries wolf here is one nobody reads on the day it is right.
        if any_attr is not None and current is not None:
            attr_match = any_attr
            key = attr_match.group(2).lower()
            value = attr_match.group(3).strip()
            current_seen = seen_attrs[len(gates) - 1]
            if key in current_seen:
                errors.append("line " + str(index + 1) + ": duplicate " + attr_match.group(2) +
                               " for gate " + current["id"])
            current_seen.add(key)
            if key == "evidence":
                current["evidence"] = value
                current["evidenceLine"] = index
            else:
                current[key] = value
            continue

        abandon_match = _ABANDON_RE.match(line)
        if abandon_match:
            abandon_id = _TRAILING_COLON_RE.sub("", abandon_match.group(1))
            reason = abandon_match.group(2).strip()
            if not abandon_id:
                errors.append("line " + str(index + 1) + ": ABANDON needs a gate id and reason")
            elif not reason:
                errors.append("line " + str(index + 1) + ": ABANDON " + abandon_id + " needs a non-blank reason")
            elif abandon_id in abandoned:
                errors.append("line " + str(index + 1) + ": duplicate ABANDON for " + abandon_id)
            else:
                abandoned[abandon_id] = reason
            current = None
            continue

        owns_match = _OWNS_RE.match(line)
        if owns_match:
            if seen_gate:
                errors.append("line " + str(index + 1) + ": OWNS must appear before the first gate")
                current = None
                continue
            declared = [item.strip() for item in owns_match.group(1).split(",") if item.strip()]
            if not declared:
                errors.append("line " + str(index + 1) + ": OWNS declares no paths")
            for item in declared:
                normalized = normalize_owns_glob(item)
                if "error" in normalized:
                    errors.append("line " + str(index + 1) + ": " + normalized["error"])
                else:
                    owns.append(normalized["value"])
            continue

        if _HASH_OR_LIST_RE.match(line):
            current = None

    if fence is not None:
        errors.append("unclosed fenced block")

    for gate in gates:
        has_check = gate["check"] is not None and gate["check"] != ""
        has_expect = gate["expect"] is not None and gate["expect"] != ""
        if has_check != has_expect:
            errors.append("gate " + gate["id"] + ": runnable gates require both non-blank CHECK and EXPECT")
        if gate["check"] == "" or gate["expect"] == "":
            errors.append("gate " + gate["id"] + ": CHECK and EXPECT cannot be blank")
        if has_expect:
            parsed = parse_regex(gate["expect"])
            if "error" in parsed:
                errors.append("gate " + gate["id"] + ": " + parsed["error"])
            elif parsed.get("pathLike"):
                # Warn rather than reject: the pattern reading may be intended, and a
                # literal path cannot be expressed once the wrapping slashes sniff.
                warnings.append("gate " + gate["id"] + ": EXPECT " + json.dumps(gate["expect"]) +
                                 " is read as a regular expression, so its dots and other metacharacters" +
                                 " are wildcards. Escape the inner slashes to keep the pattern, or drop" +
                                 " the wrapping slashes to match a literal substring.")
            gate["expectation"] = parsed
        else:
            gate["expectation"] = None

    for abandoned_id in abandoned:
        if abandoned_id not in ids:
            errors.append("ABANDON references unknown gate " + abandoned_id)
    if options.get("requireGates") is not False and not gates:
        errors.append("ledger contains zero live gates")

    return {
        "lines": lines, "eol": eol, "finalNewline": final_newline, "gates": gates,
        "abandoned": abandoned, "owns": owns, "errors": errors, "warnings": warnings,
    }
