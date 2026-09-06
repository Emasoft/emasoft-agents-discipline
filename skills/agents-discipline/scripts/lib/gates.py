"""Shared helpers for the Python ports of the agents-discipline scripts.

Port of the corresponding half of `lib/gates.mjs`. Grows one function at a time as each
script is ported; keeping the same module name means no later rename.
"""

import errno
import hashlib
import json
import os
import random
import re
import stat as statmod
import sys
import time

AGENTS_DISCIPLINE_DIR = ".agents-discipline"
LOCK_DIR = os.path.join(AGENTS_DISCIPLINE_DIR, "locks")
DEFAULT_STABLE_FILE_MAX_BYTES = 8 * 1024 * 1024

# `\Z`, not `$`: JS `$` does not match before a trailing newline and Python's does, so `$` here
# would accept a scope id of "api\n" that the oracle rejects.
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# The oracle's message interpolates `String(SCOPE_RE)`, i.e. the JS regex LITERAL, so the
# diagnostic a caller prints contains that exact text. Spelling the Python pattern here instead
# would change a user-visible string; dispatch-tests compares these messages.
_SCOPE_RE_JS_SOURCE = "/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/"

_WINDOWS_TRANSIENT_FS_ERRORS = frozenset((errno.EACCES, errno.EBUSY, errno.EPERM))


def _err_code(error):
    """The oracle's `error.code`, which is the errno NAME rather than its prose.

    Same ladder, and for the same measured reason, as `process_tree._err_code`: `str()` on a
    two-arg OSError prepends a Python-only "[Errno N] ", and `.strerror` is different prose in
    every language. Duplicated rather than imported because these two modules are ported
    independently and neither should acquire a dependency on the other for one helper.
    """
    number = getattr(error, "errno", None)
    return ((errno.errorcode.get(number) if number is not None else None)
            or getattr(error, "strerror", None)
            or str(error))


def _write_all(fd, data):
    """os.write until the buffer is drained, because a single call may write FEWER bytes.

    Node's `writeFileSync(fd, ...)` loops internally; `os.write` returns a count and Python does
    not check it. The consequence is not a wrong message, it is data loss: on a short write
    write_atomic would fsync a TRUNCATED file and then os.replace it into position, so a
    half-written dispatch.json passes every guard downstream and reads back as valid JSON or
    as corruption depending on where the cut fell. Short writes are rare on a regular file and
    routine at a disk-full or size-limit boundary, which is exactly when a state file matters.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        # A 0 return makes no progress and `view[0:]` is the same length, so the loop would spin
        # forever. POSIX makes that essentially unreachable for a non-empty write on a regular
        # file, and the oracle's internal loop has the same shape -- but the governing plan names
        # "no infinite loops" as a hard constraint, and every other loop in this port is bounded
        # by an explicit deadline. One line to make this one bounded too.
        if written <= 0:
            raise OSError("write made no progress on fd " + str(fd))
        view = view[written:]


def _is_transient_windows_fs_error(error):
    return sys.platform == "win32" and getattr(error, "errno", None) in _WINDOWS_TRANSIENT_FS_ERRORS


def _path_is_inside(parent, child):
    # `str(...)` around each side is not decoration: these parameters are untyped, so a checker
    # resolves os.path.relpath to its BYTES overload and then flags the str comparisons below.
    try:
        rel = os.path.relpath(os.path.abspath(str(child)), os.path.abspath(str(parent)))
    except ValueError:
        # Windows, different drives. `path.relative` returns an ABSOLUTE path there, which the
        # oracle's `!isAbsolute(rel)` then rejects; Python raises instead of returning one, so
        # the same answer has to be produced from the exception.
        return False
    return rel == os.curdir or (not rel.startswith(os.pardir + os.sep) and rel != os.pardir
                                and not os.path.isabs(rel))


def sha256(value):
    # DOCUMENTED DIVERGENCE, left as-is deliberately. Node's createHash().update(string)
    # substitutes U+FFFD for a lone surrogate; this strict encode raises. A Python str carries
    # lone surrogates only from a surrogateescape decode -- on Linux, a repository path whose
    # bytes are not valid UTF-8. There the oracle proceeds and hashes a LOSSY key, so two
    # different paths can share one lock; this refuses instead. That is the safer answer, and no
    # spelling of errors= reproduces Node's one-U+FFFD-per-surrogate anyway ("replace" gives
    # "?", and a surrogatepass round trip gives three). Fixing it would trade a fail-closed
    # difference for a wrong one.
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _assert_regular_single_link(st, target, label, max_bytes):
    # A FIFO opened O_NONBLOCK returns a descriptor at once and then blocks forever on read,
    # which is the hang this assertion exists to turn into an error. nlink != 1 means the
    # bytes are reachable under another name, so "the file I checked" is not a single thing.
    if not statmod.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise OSError(f"{label} must be one unchanged regular single-link file: {target}")
    if st.st_size > max_bytes:
        raise OSError(f"{label} exceeds {max_bytes} bytes: {target}")


def read_stable_regular_file(path, max_bytes=None, label="file", root=None):
    """Read a regular file under a size cap, refusing symlinks, FIFOs and mid-read changes.

    O_NOFOLLOW refuses a symlinked target outright; O_NONBLOCK keeps the open from blocking
    on a FIFO so the descriptor can be rejected by its type rather than waited on. The
    before/after stat pair makes a file that changes under the read an error instead of a
    silently half-old string.

    `root`, when given, refuses a target whose canonical path falls outside it. This is the
    guarantee the ledger never asked for and dispatch state does: `dispatch.mjs` passes a root
    for every read, so a `.agents-discipline/<scope>/dispatch.json` symlinked out of the
    repository is rejected rather than followed.
    """
    target = os.path.abspath(path)
    limit = DEFAULT_STABLE_FILE_MAX_BYTES if max_bytes is None else int(max_bytes)
    if limit < 1:
        raise OSError(f"{label} max_bytes must be a positive integer")

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise OSError(f"{label} must be one unchanged regular single-link file: {target}") from err
        if err.errno == errno.ENOENT:
            # The oracle's ENOENT PROBE, which this port shipped without. If the name is back by
            # the time we look, the file was swapped between the open and now, and that is an
            # error rather than an absence -- so it must NOT surface as ENOENT.
            #
            # The distinction is load-bearing downstream, not cosmetic: dispatch's readState does
            # `if (error.code === "ENOENT") return emptyState()`. Re-raising the raw ENOENT here
            # makes a Python readState return an EMPTY WAVE SET on the very interleaving the
            # oracle refuses — opposite outcomes on the same race, in the function dispatch.py is
            # about to be built on.
            try:
                named = os.lstat(target)
            except OSError as probe_error:
                if probe_error.errno == errno.ENOENT:
                    raise err from None          # a genuine absence: the oracle's `throw error`
                raise
            _assert_regular_single_link(named, target, label, limit)
            raise OSError(f"{label} appeared after its open reported it missing: {target}") from err
        # ENOENT is deliberately the ONLY errno exposed unchanged, and only above: callers that
        # permit a missing file can tell absence at open from a later race.
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
        # strict=True on BOTH realpath calls. The oracle uses `realpathSync`, which RAISES when
        # the name is gone; Python's default realpath resolves as far as it can and returns a
        # path for a target that no longer exists. Without strict, a name unlinked mid-read
        # resolves to the same string before and after, so the "changed canonical location"
        # check below passes on a file whose name the oracle would have errored on.
        # `root is None` is the oracle's `root === undefined`, NOT its `null`. Measured: an
        # explicit null makes the oracle take the non-null branch and resolve(null) throws a
        # TypeError. Python has no second empty value to map that onto -- None IS how a
        # keyword argument says "not supplied" -- so there is no caller that can express the
        # throwing case, and making None throw would break the default path instead.
        canonical_root = None if root is None else os.path.realpath(os.path.abspath(root), strict=True)
        canonical_before = os.path.realpath(target, strict=True)
        if canonical_root is not None and not _path_is_inside(canonical_root, canonical_before):
            raise OSError(f"{label} resolves outside the allowed root: {target}")

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
        if os.path.realpath(target, strict=True) != canonical_before:
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
# so the re.ASCII trap does not apply here (see gate_lint.py for one that does). That is a
# narrower statement than "no ASCII-vs-Unicode divergence": JS `\s` and Python `\s` are
# DIFFERENT SETS independently of any flag -- `\ufeff` is whitespace to JS and not to Python,
# `\x1c`-`\x1f` are whitespace to Python and not to JS. Both reach `_ATTR_RE`'s `^(\s+)`
# indent test and, by complement, `_ID_MATCH_RE`'s `^(\S+?):`, where a `\ufeff` inside an id
# lands IN the id here and TERMINATES it there. Low likelihood, real mechanism, not covered.
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


# ---------------------------------------------------------------------------------------------
# Scope resolution, durable writes, and the file lock. Port of the gates.mjs half that
# lib/dispatch.mjs imports: validateScopeId, scopeRoot, writeAtomic, withFileLock, appendStatus.
# ---------------------------------------------------------------------------------------------


def validate_scope_id(value, label="scope"):
    """Return an error STRING, or None when the id is acceptable. The oracle returns null."""
    # `String(value || "")`: JS coerces every falsy value -- including 0 -- to "" before the
    # test, so a caller passing 0 gets the id error rather than "0".
    #
    # KNOWN, BOUNDED divergence for a non-string TRUTHY value: `String([0])` is "0", a VALID
    # id, where `str([0])` is "[0]", invalid. Not emulated, because it is unreachable rather
    # than merely unused -- `dispatch.mjs`'s validId rejects `typeof value !== "string"` BEFORE
    # calling this, and every other caller passes a parsed string. Reproducing Array.prototype
    # .join and "[object Object]" to serve a caller the type check forbids would be building
    # for a scenario the codebase prevents. If a future caller does pass one, it must type-check
    # first, as the oracle's own callers do.
    scope_id = str(value) if value else ""
    if not _SCOPE_RE.match(scope_id) or scope_id in (".", ".."):
        return label + " must match " + _SCOPE_RE_JS_SOURCE + " and cannot be . or .."
    return None


def scope_root(root, scope):
    # normpath, because `path.join` NORMALIZES and os.path.join does not: the oracle's
    # join("a//", ".agents-discipline", "x") is "a/.agents-discipline/x" where Python's keeps
    # the doubled separator. Callers pass an already-resolved root, so this only matters for a
    # caller that does not -- which is exactly the case a silent divergence would hide.
    return os.path.normpath(os.path.join(root, AGENTS_DISCIPLINE_DIR, scope))


def status_log_path(root, scope):
    if scope:
        return os.path.join(scope_root(root, scope), "status.log")
    # normpath here too. The scoped branch inherits it from scope_root, and this one was left
    # with a bare join -- so it reproduced exactly the divergence the comment in scope_root
    # describes: "a//" gave "a//agents-discipline-status.log" against the oracle's
    # "a/agents-discipline-status.log". gate-check uses this bare form.
    return os.path.normpath(os.path.join(root, "agents-discipline-status.log"))


def _mkdirs(path, mode=None):
    """Create `path` and its missing ancestors, applying `mode` to EVERY directory created.

    os.makedirs applies `mode` only to the final component and leaves intermediates at the
    umask default; Node's recursive mkdirSync applies it to all of them. The tree created here
    is `.agents-discipline/<scope>/`, so taking Python's behaviour would leave the coordination
    directory itself world-readable while its leaf claimed 0700 -- a weaker guarantee than the
    oracle's, in the one place the mode argument exists to provide it.
    """
    if mode is None:
        os.makedirs(path, exist_ok=True)
        return
    missing = []
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, mode)
        except FileExistsError:
            # Concurrent creation by a peer is fine; a FILE at this name is not, and os.mkdir
            # raises the same FileExistsError for both. Swallowing it unconditionally made the
            # function return silently where the oracle's mkdirSync raises EEXIST, and left
            # failing-closed to whatever each caller happened to do next -- true of all three
            # call sites today, and a trap for a fourth. Re-raising the ORIGINAL error keeps the
            # oracle's error class as well as its outcome.
            if not os.path.isdir(directory):
                raise


def _assert_real_directory(path, message):
    info = os.lstat(path)
    if statmod.S_ISLNK(info.st_mode) or not statmod.S_ISDIR(info.st_mode):
        raise OSError(message)


def _assert_safe_state_path(root, target):
    state_root = os.path.join(os.path.abspath(root), AGENTS_DISCIPLINE_DIR)
    # os.path.exists FOLLOWS symlinks, as the oracle's existsSync does: a dangling link is
    # "absent" to both and is left for the mkdir below to fail on, while a link to a real
    # directory is "present" and is caught by the lstat.
    if os.path.exists(state_root):
        _assert_real_directory(
            state_root, state_root + " must be a real directory, not a link or file")
    parent = os.path.dirname(target)
    _mkdirs(parent, 0o700)
    _assert_real_directory(parent, parent + " must be a real directory")


def _replace_atomic(temp, target):
    deadline = time.monotonic() + 2.0
    delay = 0.005
    while True:
        try:
            os.replace(temp, target)
            return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if not _is_transient_windows_fs_error(error) or remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.1)


def write_atomic(file, text, root=None):
    target = os.path.abspath(file)
    if root:
        _assert_safe_state_path(root, target)
    else:
        parent = os.path.dirname(target)
        # No mode: the oracle's non-root branch calls mkdirSync WITHOUT one, so this tree gets
        # the umask default. Handing it 0o700 here would be a gratuitous divergence in the
        # branch that writes ordinary repository files, not coordination state.
        _mkdirs(parent)
        _assert_real_directory(parent, parent + " must be a real directory")
    try:
        if statmod.S_ISLNK(os.lstat(target).st_mode):
            raise OSError("refusing to replace symlink " + target)
    except FileNotFoundError:
        pass

    temp = ""
    fd = None
    for _ in range(8):
        temp = target + "." + str(os.getpid()) + "." + os.urandom(8).hex() + ".tmp"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    if fd is None:
        raise OSError("could not create a unique temporary file for " + target)
    try:
        # Strict encode, unlike the READ side's errors="replace". Node's writeFileSync(fd, s,
        # "utf8") substitutes U+FFFD for a lone surrogate; a Python str carrying one can only
        # come from a surrogateescape decode, which nothing in this port performs, so a strict
        # failure here means a caller built an unencodable string and should hear about it.
        _write_all(fd, str(text).encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None
        _replace_atomic(temp, target)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass  # renamed or absent


def _lock_directory(root):
    # The PHYSICAL root, so lexical aliases of one repository share one lock namespace.
    # strict=True: the oracle's realpathSync raises when the root is missing, and the root must
    # exist for any agents-discipline operation. Python's default realpath would invent a path
    # for a missing root and hand every caller a private lock namespace under it -- two agents
    # would then both "hold" the same lock.
    canonical_root = os.path.realpath(os.path.abspath(root), strict=True)
    directory = os.path.join(canonical_root, LOCK_DIR)
    _assert_safe_state_path(canonical_root, directory)
    _mkdirs(directory, 0o700)
    _assert_real_directory(directory, directory + " must be a real directory")
    return directory


def _canonical_lock_target(target):
    """Canonicalize the nearest existing ancestor and rebuild the missing suffix.

    Lock targets are often not files yet (dispatch state, the lease registry). The final named
    component is never resolved: atomic replacement, or a rejected final symlink, must not
    change which lock protects that path.
    """
    absolute = os.path.abspath(target)
    current = os.path.dirname(absolute)
    suffix = [os.path.basename(absolute)]
    while True:
        try:
            canonical = os.path.realpath(current, strict=True)
            return os.path.abspath(os.path.join(canonical, *suffix))
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            parent = os.path.dirname(current)
            if parent == current:
                raise
            suffix.insert(0, os.path.basename(current))
            current = parent


def with_file_lock(root, target, fn, timeout_ms=30000):
    """Run `fn` while holding an exclusive lock keyed by `target`'s canonical path.

    The oracle is async and awaits `fn`; this is synchronous, which is the whole shape
    difference. Deadlines use time.monotonic() rather than the oracle's Date.now(): a wall-clock
    step during a 30s wait would otherwise expire or extend the lock wait.
    """
    directory = _lock_directory(root)
    lock_target = _canonical_lock_target(target)
    lock = os.path.join(directory, sha256(lock_target)[:24] + ".filelock")
    deadline = time.monotonic() + timeout_ms / 1000.0
    token = os.urandom(16).hex()

    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except OSError as error:
            # On Windows a file held by another process surfaces as EPERM/EACCES/EBUSY rather
            # than EEXIST. Only those platform-specific sharing errors count as contention.
            if error.errno != errno.EEXIST and not _is_transient_windows_fs_error(error):
                raise
            # Never unlink a lock observed by path: between the stat and the unlink its owner
            # can release and a successor acquire the same name (the classic ABA race).
            # Missing-after-EEXIST simply means retry; a crashed owner's lock fails closed at
            # the timeout and a human removes it after reading its JSON metadata.
            missing = False
            try:
                os.stat(lock)
            except OSError as stat_error:
                if stat_error.errno == errno.ENOENT:
                    missing = True
                elif not _is_transient_windows_fs_error(stat_error):
                    raise
            if time.monotonic() >= deadline:
                raise OSError("timed out waiting for lock on " + str(target) +
                              " (last filesystem error: " + _err_code(error) + ")")
            if missing and error.errno == errno.EEXIST:
                continue
            time.sleep((15 + random.randrange(25)) / 1000.0)

    identified = False
    try:
        # separators + ensure_ascii=False reproduce JSON.stringify byte for byte; the `target`
        # field is a filesystem path, so a non-ASCII repository name would otherwise be written
        # \u-escaped by one implementation and literally by the other.
        # _write_all here too, and this site is the least obvious of the three: a short write
        # leaves the lock metadata as truncated JSON, the release loop's json.load raises
        # ValueError, that arm breaks WITHOUT unlinking, and the lock is held until a human
        # removes it. A partial write of an ownership record is worse than no record.
        _write_all(fd, json.dumps(
            {"token": token, "pid": os.getpid(), "target": lock_target,
             "at": int(time.time() * 1000)},
            separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        identified = True
    except OSError:
        pass  # leave for manual cleanup rather than risk deleting a successor

    try:
        return fn()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        if identified:
            release_deadline = time.monotonic() + 2.0
            delay = 0.005
            while True:
                try:
                    with open(lock, "r", encoding="utf-8") as handle:
                        current = json.load(handle)
                    # isinstance, because this runs inside a `finally`. The oracle reads
                    # `current.token` off whatever JSON.parse returned, and JS property access on
                    # an array or a number is `undefined` -- unequal to the token, so it declines
                    # to unlink and breaks. `.get` exists only on a dict, so well-formed but
                    # non-object JSON raised AttributeError from the finally and REPLACED the
                    # exception fn() was propagating. Measured on the same script both sides:
                    # the oracle surfaced "invalid dispatch state: boom" and the port surfaced
                    # "'list' object has no attribute 'get'" -- and dispatch's readState decides
                    # whether to re-wrap by testing for exactly that "invalid dispatch state:"
                    # prefix, so the port would have destroyed the diagnostic it branches on.
                    # (Both runtimes leave the lock file behind here, by design: neither unlinks
                    # a lock it cannot prove it owns.)
                    if isinstance(current, dict) and current.get("token") == token:
                        os.unlink(lock)
                    break
                except OSError as error:
                    if error.errno == errno.ENOENT:
                        break
                    remaining = release_deadline - time.monotonic()
                    if not _is_transient_windows_fs_error(error) or remaining <= 0:
                        break
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, 0.1)
                except ValueError:
                    # Unparseable lock metadata. The oracle's single catch covers its JSON.parse
                    # too, and reaches the same non-transient `break`: leave the file alone.
                    break


def append_status(root, scope, line):
    path = status_log_path(root, scope)
    _assert_safe_state_path(root, path)
    fd = None
    try:
        try:
            before = os.lstat(path)
            if (not statmod.S_ISREG(before.st_mode) or statmod.S_ISLNK(before.st_mode)
                    or before.st_nlink != 1):
                raise OSError("refusing non-file or linked status log " + path)
        except FileNotFoundError:
            pass
        # Open first, but write only after checking the descriptor against a fresh named-entry
        # stat. That rejects persistent links and ordinary replacements. SECURITY.md documents
        # the concurrent Windows path-control boundary this does not close.
        #
        # NOT PORTED, deliberately: the oracle's statCurrentNamedFile opens a SECOND descriptor
        # on Windows and compares fstat to fstat, because affected Node/libuv builds return
        # non-comparable `dev` fields from path-stat versus descriptor-stat. That is a defect of
        # the oracle's runtime, not of this one -- CPython's os.stat and os.fstat take st_dev
        # and st_ino from the same source on every supported platform -- so the direct
        # comparison below IS the check the secondary descriptor exists to approximate. The
        # POSIX branch the oracle actually runs does exactly this, too.
        no_follow = 0 if sys.platform == "win32" else getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_NONBLOCK", 0) | no_follow, 0o600)
        opened = os.fstat(fd)
        named = os.lstat(path)
        # The NAMED entry's own type/link failure carries assertRegularSingleLink's message, not
        # "refusing non-file or replaced". In the oracle that check lives inside
        # statCurrentNamedFile and throws before line 179 is reached, so the two conditions
        # produce two different strings; folding them into one lost that. Verified by calling the
        # oracle's helper with appendStatus's own label on a hard-linked file: it says "status log
        # must be one unchanged regular single-link file: <path>". Only reachable when the name
        # changes BETWEEN the first lstat and here -- the race these checks exist for -- which is
        # why no driver row covers it and why the evidence is the helper's output, not a run.
        _assert_regular_single_link(named, path, "status log", float("inf"))
        if (not statmod.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_ino, opened.st_dev) != (named.st_ino, named.st_dev)):
            raise OSError("refusing non-file or replaced status log " + path)
        _write_all(fd, (re.sub(r"[\r\n]+", " ", str(line)) + "\n").encode("utf-8"))
        os.fsync(fd)
        return path
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
