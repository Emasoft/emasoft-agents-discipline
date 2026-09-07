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

# `.trim()` is NOT `str.strip()`: the sets differ in both directions (U+FEFF one way, the five
# separators U+001C-U+001F and U+0085 the other). Every `.trim()` in the gates.mjs half is ported
# through this helper rather than str.strip(); see jsapi.js_trim for the measured sets.
from jsapi import (  # noqa: E402  # type: ignore[import-not-found]
    js_length, js_slice, js_sort_key, js_string, js_trim, js_truthy,
)

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


def write_all(fd, data):
    """os.write until the buffer is drained, because a single call may write FEWER bytes.

    PUBLIC for the FOURTH time on this lesson, after node_fs_message, mkdirs and node_call --
    and this one cost more than a message. gate_check's record_approval wrote its lock payload
    with a RAW os.write, so a short write truncated the file the oracle's writeFileSync
    (gate-check.mjs:494) would have written in full. The underscore is the only reason a
    neighbour reached for the primitive instead of the helper written to prevent exactly that.

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


_MISSING = object()


def _js_is_integer(value):
    """`Number.isInteger` — TRUE for 5.0, because JS has one number type.

    `isinstance(5.0, int)` is False, so a plain isinstance check would reject a maxBytes the
    oracle accepts. Infinity and NaN are both non-integers here, matching JS.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf")) and float(value).is_integer()


def _js_to_number(value):
    """`Number(x)` for the shapes an options dict can carry. NaN stands in for JS NaN.

    NOT jsapi._js_number, which goes the OTHER way (a number to JS's String() spelling).
    Same two words, opposite direction -- hence the distinct name.
    """
    if value is None:
        return 0            # Number(null) === 0, and JSON null decodes to None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return value
    try:
        text = js_trim(str(value))
        # int when integral: JS has ONE number type, so `${10}` prints "10". float("10") is
        # 10.0, which passed validation and then rendered as "file exceeds 10.0 bytes" where
        # the oracle says "10 bytes". Measured on maxBytes "1" against a 2-byte file.
        number = 0.0 if text == "" else float(text)
        return int(number) if number.is_integer() else number
    except ValueError:
        # Node's Number() also accepts hex ("0x10" -> 16) where float() raises; unreachable
        # here (no caller passes one) and left as NaN, which fails the bound closed.
        return float("nan")


def _same_snapshot(left, right):
    return (same_file_identity(left, right) and left.st_size == right.st_size
            and left.st_mtime_ns == right.st_mtime_ns and left.st_ctime_ns == right.st_ctime_ns)


_NODE_MESSAGE_TYPES = {}


_KEEP_FILENAME = object()


def _node_message_error(error, syscall, filename=_KEEP_FILENAME) -> OSError:
    """Same OSError SUBCLASS and errno as `error`, but str() gives node's message.

    Annotated `-> OSError` at the ROOT rather than at each consumer: the subclass is built by a
    dynamic `type(...)` call, so the checker sees an opaque `_` and reads every `raise` of it --
    here, in node_call, _node_lstat and mkdirs -- as raising a non-exception. One annotation
    plus one suppression at the return below settles all of them; annotating the consumers
    instead just moves the same complaint outward, one copy per hop.

    Three things have to hold at once and no plain construction gives all three:
      * `except FileNotFoundError` must still match  -> keep the subclass
      * `.errno` must survive                        -> this module branches on it in ~10 places
      * str() must be node's sentence                -> and NOT Python's "[Errno N] ..." prefix
    Measured, both simpler forms fail: a single-arg OSError(msg) drops the type and errno, and
    assigning .errno afterwards makes str() switch to the two-arg rendering, which formats
    from .strerror -- so the message is silently replaced by "[Errno 2] None: '<path>'".
    A per-base subclass overriding __str__ is what satisfies all three; it is cached because
    building a class per raised error would be a slow, unequal object every time.
    """
    base = type(error)
    subclass = _NODE_MESSAGE_TYPES.get(base)
    if subclass is None:
        # base.__name__, NOT "Node"+name: an invented name leaks into tracebacks as
        # `gates.NodeFileNotFoundError`, which a reader then greps for and finds only a
        # type() call. getattr with a fallback, NOT self._node_message: the attribute is
        # assigned AFTER construction, so copy.copy(e) -- which calls subclass(*e.args) --
        # would build an instance whose __str__ raises AttributeError. An exception whose
        # str() raises is the worst possible failure in a logging path.
        # `base.__str__` resolves through base.__mro__, NOT the metaclass, so this is the
        # UNBOUND OSError.__str__ and passing self is correct. The checker models it as already
        # bound to the class and so reads the argument as one too many; suppressed rather than
        # rewritten, because every rewrite that satisfies it (super(), object.__str__) changes
        # which __str__ actually runs.
        subclass = type(base.__name__, (base,), {
            "__str__": lambda self: getattr(self, "_node_message", None)
            or base.__str__(self)})  # pyright: ignore[reportCallIssue]
        _NODE_MESSAGE_TYPES[base] = subclass
    rebuilt = subclass(error.errno, error.strerror)
    # `filename` OVERRIDES rather than mutating the caller's error, which is also the `from`
    # cause: a mutated cause shows a path the OS never reported, destroying the one thing a raw
    # cause is good for. The message is then computed from `rebuilt`, not `error`, so the
    # override reaches the text too -- rebuilt carries no _node_message yet, so node_fs_message
    # takes its normal path.
    # A SENTINEL, not None: None is a REAL filename here (an fd-based failure -- fstat, read --
    # has no path in either runtime, and node_fs_message renders that as a bare ", fstat").
    rebuilt.filename = error.filename if filename is _KEEP_FILENAME else filename
    # filename2 carries the DESTINATION of a two-path call (rename/link/symlink) and must survive
    # the rebuild, or node_fs_message loses the `-> '<dest>'` clause it now emits. Not covered by
    # the `filename` override: that override exists for mkdir, which has no destination.
    rebuilt.filename2 = getattr(error, "filename2", None)
    rebuilt._node_message = node_fs_message(rebuilt, syscall)
    # `base` IS type(error) and error is an OSError, so every subclass built here derives from
    # OSError -- a fact the dynamic type() call hides from the checker but not from the runtime.
    return rebuilt  # pyright: ignore[reportReturnType]


def node_call(syscall, call, *args, **kwargs):
    """Run ONE fs syscall, re-raising with node's message shape for THAT syscall.

    PUBLIC for the third time on the same lesson, after node_fs_message and mkdirs: gate_check's
    read_approval_file is a SECOND multi-syscall reader with the exact shape this fixes, and it
    SHOULD NOT import a name marked private. Not "could not" -- Python does not enforce the
    underscore, `from gates import _node_call` would have worked, and stating a convention as a
    technical constraint is the same overclaim this module keeps correcting. The convention plus
    a lesson now paid three times is the stronger argument anyway: a helper the neighbours are
    told not to import is one they re-implement, or -- here -- simply go without.

    read_stable_regular_file makes seven more syscalls after its open, and the oracle wraps
    NONE of them: gates.mjs:152-181 lets fstatSync, lstatSync, realpathSync and readSync throw
    RAW, so each failure carries its OWN syscall token out to the caller. Every reader's call
    site hardcodes one constant instead -- `open` at seven sites across three modules -- because
    a caller cannot know which of the eight syscalls failed. Attaching the token where it IS
    known is the only fix that does not ask every caller to guess; node_fs_message then prefers
    the attached message over the constant it was passed.

    MEASURED, node against CPython: an fd-based failure (fstat, read, fsync) carries NO path in
    EITHER runtime, which node_fs_message's suffix branch already renders as a bare ", fstat";
    a path-based one (open, lstat) names the same path in both. So for these the token was the
    only thing missing.

    ONE CONSTANT PER CALL SITE IS ONLY VALID WHEN THE CALL MAKES ONE SYSCALL, which is the
    limit this helper does not enforce and cannot. `realpath` broke it -- node walks the path
    and reports whichever step failed, so its token varies BY ERRNO -- and it now has its own
    `_node_realpath` with the measurements. Before wrapping a new call here, check that node
    reports one syscall for it across errnos; "I measured one errno" is not that check. The
    first version of this docstring said MEASURED on the strength of exactly one, and MEASURED
    is the word that stops the next reader looking.
    """
    try:
        return call(*args, **kwargs)
    except OSError as error:
        # AN ERRNO-LESS OSError IS ONE THE PORT AUTHORED, and its text is already the message the
        # oracle prints -- so attaching a node shape to it DESTROYS that text. This is the same
        # `errno is not None` guard every call site carries; node_call bypassed it, and wrapping
        # write_all (which raises a bare OSError on a zero-progress write) made it reachable.
        # MEASURED, before the guard:
        #     unwrapped  write made no progress on fd 7
        #     wrapped    [Errno None] None: None -> None: [Errno None] None: None -> None, write
        # The doubling this module documents elsewhere, with the original text gone entirely.
        # THE TEST IS SHAPE, THE ARGUMENT IS PROVENANCE, and they coincide only because every
        # OSError this module authors is SINGLE-ARG -- `OSError(f"...")` leaves errno None. An
        # `OSError(errno.EIO, "authored")` would be authored AND carry an errno, and would be
        # reshaped; that is correct (a caller cannot tell it from a syscall's) but it is not what
        # "authored" alone implies, so the coincidence is worth naming rather than relying on.
        # `is None`, NOT `not error.errno`: errno 0 is falsy but PRESENT, and a truthiness test
        # would send it down the verbatim path on a premise about absence.
        if error.errno is None:
            raise
        raise _node_message_error(error, syscall) from error


def _node_mkdir_error(error, path) -> OSError:
    """A failed directory creation, wearing node's shape -- which names a DIFFERENT path.

    The `-> OSError` is load-bearing for the checker, not decoration: _node_message_error builds
    its class with a dynamic `type(...)` call, so through one more hop the inferred return
    degrades to an opaque `_` and every `raise` of it reads as "not an exception". Annotating
    the boundary restores what the code always did.

    Node's recursive `mkdirSync` reports the directory it was ASKED for; `os.makedirs` reports
    the first ancestor it could not create. MEASURED at depth 3, which discriminates "the
    original argument" from "the deepest one attempted" (depth 1 cannot tell them apart):

        mkdir <W>/a/b/c, <W> unwritable
        node   -> EACCES: permission denied, mkdir '<W>/a/b/c'
        python ->                            filename '<W>/a'

    So the syscall token alone is not enough here: the path has to be overridden too, or the
    message names a directory the caller never asked about.

    THE OVERRIDE IS A DATA CHANGE, not only a message change: the returned error's `.filename`
    is the directory ASKED FOR, no longer the one that actually failed. Measured that nothing
    reads it today -- `node_fs_message` is the only consumer of `.filename` in the port -- but a
    future caller wanting the failing path must take it from the `__cause__`, which is left
    unmutated precisely so it still carries what the OS said.
    """
    return _node_message_error(error, "mkdir", filename=os.path.abspath(path))


def _node_realpath(target):
    """os.path.realpath(strict=True), re-raising with node's shape -- whose syscall VARIES.

    A single constant is provably wrong here, and the first version of this code used one.
    node's realpathSync is a WALK, so the syscall it reports is whichever step failed. MEASURED
    on macOS, five shapes, and they do not agree:

        ELOOP (symlink loop)          -> stat
        EACCES (unsearchable parent)  -> lstat
        ENOTDIR (file as a component) -> lstat
        ENOENT (missing component)    -> lstat
        ENOENT (dangling symlink)     -> stat   AND node names the LINK, python the TARGET

    ELOOP is exactly the errno this module cares most about -- read_stable_regular_file has a
    dedicated branch for it -- so the flat `lstat` was wrong in the one case most likely to be
    hit.

    THE ERRNO IS A PROXY, NOT THE MECHANISM, and saying so is the point: realpath lstats each
    COMPONENT and stats a RESOLVED LINK TARGET, so the token follows the WALK STEP that failed.
    ELOOP and the dangling link both report `stat` because both fail at target resolution, not
    because of anything about their codes. The map below fits the five MEASURED samples.

    INFERRED, NOT MEASURED -- flagged separately because the five lines above were, and a reader
    cannot otherwise tell which is which: an EACCES on a link TARGET should report `stat` where
    this returns `lstat`. That follows from the walk-step model, which is itself inferred from
    two `stat` results; no such case was ever forced. Keying on the real variable means
    re-walking the path as libuv does -- so this stays a proxy, deliberately.

    UNCLOSED, and stated rather than buried: the DANGLING-SYMLINK case still diverges twice
    over. It is ENOENT, so this returns `lstat` where node says `stat`; and node reports the
    link while `os.path.realpath` reports the target it could not resolve. Telling it apart from
    a plain missing component means re-walking the path the way libuv does, which is the
    runtime's own algorithm rather than a rule -- the same reason the JSON and regex parser
    messages are accepted divergences. Unreachable through any CLI today: every caller reaches
    realpath only AFTER an O_NOFOLLOW open of the same path succeeded, which a dangling link
    cannot survive.
    """
    try:
        return os.path.realpath(target, strict=True)
    except OSError as error:
        raise _node_message_error(
            error, "stat" if error.errno == errno.ELOOP else "lstat") from error


def _node_lstat(target):
    """os.lstat, re-raising with node's message shape.

    stat_current_named_file does NOT catch this error -- it propagates to the caller, so its
    text is part of the function's observable surface. Measured: a missing path gives
    "ENOENT: no such file or directory, lstat '<p>'" from the oracle against Python's
    "[Errno 2] No such file or directory: '<p>'". Caught by the driver rows for this function,
    which is the whole reason they dump the MESSAGE rather than just the outcome.
    """
    try:
        return os.lstat(target)
    except OSError as error:
        # Rebuild the SAME subclass with a single argument, then restore errno/filename by
        # assignment. A plain `OSError(msg)` lost both the FileNotFoundError type and .errno,
        # and this module branches on those in ten places -- matching node's message text is
        # not worth breaking `except FileNotFoundError` for the caller. Single-arg (not
        # two-arg) because a two-arg OSError renders str() as "[Errno 2] <msg>", putting back
        # the Python-only prefix this reconstruction exists to remove.
        raise _node_message_error(error, "lstat") from error


def stat_current_named_file(path, options=None):
    """lstat a path, asserting it is one regular single-link file within a size cap.

    On Windows the oracle additionally brackets a second, non-creating descriptor open,
    because path-stat and descriptor-stat used different implementations in affected libuv
    builds and their `dev` fields are not comparable. That branch is ported but is
    **UNWITNESSED** — nothing in this repository runs on Windows, so no test here can execute
    it. It is kept rather than dropped because it is a TOCTOU identity guard: omitting it
    would silently weaken the check on the one platform it exists for.
    """
    options = options or {}
    target = os.path.abspath(path)
    kind = js_string(options["label"] if js_truthy(options.get("label")) else "file")

    raw_limit = options.get("maxBytes", _MISSING)
    # `undefined` and `null` are DIFFERENT to the oracle: absent means Infinity, an explicit
    # null is Number(null) === 0 and fails the bound. Python collapses both to None, so the
    # distinction has to come from a sentinel or the port silently accepts a null maxBytes.
    limit = float("inf") if raw_limit is _MISSING else _js_to_number(raw_limit)
    if not (limit == float("inf") or (_js_is_integer(limit) and limit >= 1)):
        raise OSError(kind + " maxBytes must be a positive integer")

    raw_flags = options.get("openFlags", _MISSING)
    access_flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)) if raw_flags is _MISSING \
        else _js_to_number(raw_flags)
    mutating_flags = getattr(os, "O_CREAT", 0) | getattr(os, "O_TRUNC", 0)
    if not _js_is_integer(access_flags) or (int(access_flags) & mutating_flags) != 0:
        raise OSError(kind + " identity descriptor must use non-creating, non-truncating flags")

    before = _node_lstat(target)
    _assert_regular_single_link(before, target, kind, limit)
    if sys.platform != "win32":
        return before

    # Everything below is Windows-only. A checker pinned to this machine's platform narrows
    # sys.platform and reports it structurally unreachable -- which is TRUE here and exactly
    # why the branch is marked unwitnessed above. Suppressed rather than deleted: it is a
    # TOCTOU identity guard, and the platform it guards is one no test here can run.
    fd = None  # pyright: ignore[reportUnreachable]
    try:
        fd = os.open(target, int(access_flags))
        current = os.fstat(fd)
        _assert_regular_single_link(current, target, kind, limit)
        after = _node_lstat(target)
        _assert_regular_single_link(after, target, kind, limit)
        after_current = os.fstat(fd)
        _assert_regular_single_link(after_current, target, kind, limit)
        # Two results from the SAME path-stat implementation, so identity and snapshot stay
        # comparable even on affected builds -- this brackets the secondary open without ever
        # comparing a path-stat `dev` to a descriptor-stat `dev`.
        named_entry_unchanged = (same_file_identity(before, after)
                                 if options.get("stableSnapshot") is False
                                 else _same_snapshot(before, after))
        if not named_entry_unchanged:
            raise OSError(kind + " changed while its named identity was checked: " + target)
        # The lstat pair alone cannot see an A -> B -> A swap around the descriptor open;
        # exact inode equality supplies the comparable named-to-handle field.
        if before.st_ino != current.st_ino or after.st_ino != current.st_ino:
            raise OSError(kind + " descriptor does not identify its guarded name: " + target)
        if options.get("stableSnapshot") is not False and not _same_snapshot(current, after_current):
            raise OSError(kind + " changed while its descriptor identity was checked: " + target)
        return after_current
    finally:
        # A close failure is an infrastructure failure, not a reason to accept an identity
        # result whose secondary descriptor did not close cleanly.
        if fd is not None:
            os.close(fd)


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

    CALLER CONTRACT: `root` must come from argv or a computed path, NEVER from a parsed
    document. JSON `null` decodes to None, and None here means "not supplied", so a root read
    out of parsed JSON would silently DISABLE containment where the oracle throws. A non-string
    root is rejected at the boundary below; `None` is the one case that cannot be told apart.
    """
    # At the BOUNDARY, so a bad root fails here rather than two frames down: os.path.abspath on
    # a bytes root SUCCEEDS and returns bytes, and _path_is_inside then compares str to bytes
    # and raises inside the guard itself. This closes every JSON-derived shape except null.
    if root is not None and not isinstance(root, str):
        raise TypeError(f"{label} root must be a string path, not {type(root).__name__}")
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
                # WRAPPED, because this is the second escape hatch out of this function and it
                # broke the invariant the seven hardcoded `open` constants rest on. The oracle's
                # `throw probeError` re-throws a RAW node fs error, whose message already names
                # `lstat`; a bare `raise` here re-threw a Python-shaped one that a caller guessing
                # `open` would then mislabel. Found by an adversarial review of the invariant, not
                # by the sweep -- the sweep reads sites that INTERPOLATE, and this one only raises.
                raise _node_message_error(probe_error, "lstat") from probe_error
            _assert_regular_single_link(named, target, label, limit)
            raise OSError(f"{label} appeared after its open reported it missing: {target}") from err
        # ENOENT is deliberately the ONLY errno exposed unchanged, and only above: callers that
        # permit a missing file can tell absence at open from a later race.
        raise

    try:
        opened = node_call("fstat", os.fstat, fd)
        named = node_call("lstat", os.lstat, target)
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
        #
        # CALLER OBLIGATION, because the argument above stops holding the moment a root comes
        # from anywhere but a literal: JSON `null` decodes to None, so a root read out of parsed
        # JSON would silently DISABLE containment here where the oracle throws. Every caller
        # must pass a root from argv or a computed path, never from a parsed document. Today's
        # do (dispatch takes --root from the CLI); dispatch.py must keep it that way.
        canonical_root = (None if root is None
                          else _node_realpath(os.path.abspath(root)))
        canonical_before = _node_realpath(target)
        if canonical_root is not None and not _path_is_inside(canonical_root, canonical_before):
            raise OSError(f"{label} resolves outside the allowed root: {target}")

        chunks = []
        total = 0
        while True:
            chunk = node_call("read", os.read, fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OSError(f"{label} exceeds {limit} bytes: {target}")

        after = node_call("fstat", os.fstat, fd)
        _assert_regular_single_link(after, target, label, limit)
        if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (
            opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns
        ):
            raise OSError(f"{label} changed while it was read: {target}")
        if _node_realpath(target) != canonical_before:
            raise OSError(f"{label} changed canonical location while it was read: {target}")
        # errors="replace", NOT strict. A ledger is prose a human pasted into, so one byte of
        # it is routinely not UTF-8 -- a latin-1 accent, a smart quote out of a word processor.
        # Node's Buffer.toString("utf8") substitutes U+FFFD and carries on; a strict decode
        # raises instead, and the caller then reports "cannot read" (exit 2, "not a ledger")
        # on a file that is visibly a ledger, over a byte that is almost never in the row
        # under test. Measured: `caf\xe9` in a unit name turned a complete ledger into exit 2.
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        # SWALLOWED, matching gates.mjs:192 `try { closeSync(fd); } catch { /* ignore */ }`.
        # This was a bare os.close, and the divergence was BEHAVIOURAL rather than cosmetic: on a
        # close failure the oracle ignores it and returns the content it already read, while the
        # port raised -- and a raise from `finally` also REPLACES the in-flight exception, so a
        # real read error could be overwritten by an EIO on the way out.
        # THE POLARITY IS PER-FUNCTION, so it cannot be applied by habit: stat_current_named_file
        # deliberately does NOT swallow (mjs:114, "a close failure is an infrastructure failure"),
        # and write_atomic's main path does not either (mjs:710). Checked all six close sites in
        # this module against their counterparts; this was the only one that disagreed.
        try:
            os.close(fd)
        except OSError:
            pass


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
    raw = js_trim(str(value or "")).replace("\\", "/")
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


# JS spells this /[*?[{]/ -- an unescaped `[` is literal inside a character class in both
# languages, but writing it escaped here keeps the intent readable rather than clever.
_GLOB_META_RE = re.compile(r"[*?\[{]")


def literal_prefix(glob):
    """Leading segments of a glob that carry no wildcard."""
    normalized = normalize_owns_glob(glob)
    if "error" in normalized:
        return ""
    literal = []
    for part in normalized["value"].split("/"):
        if _GLOB_META_RE.search(part):
            break
        literal.append(part)
    return "/".join(literal)


def globs_overlap(left, right):
    """Prove disjointness only when literal path segments disagree.

    Everything else conflicts, including mid-segment pairs such as a* and ab*. A lease is a
    write-permission decision, so the safe answer under uncertainty is "conflict": a false
    "disjoint" hands two workers the same paths, and this project has already seen that
    happen live (an unreplaced OWNS: placeholder let two claims both exit 0).
    """
    a = normalize_owns_glob(left)
    b = normalize_owns_glob(right)
    if "error" in a or "error" in b:
        return True
    a_segments = a["value"].split("/")
    b_segments = b["value"].split("/")
    for av, bv in zip(a_segments, b_segments):
        if _GLOB_META_RE.search(av) or _GLOB_META_RE.search(bv):
            return True
        if av != bv:
            return False
    # An exact prefix may denote a directory ownership claim, so it can overlap every
    # descendant. Treat common-prefix length differences as conflicts.
    return True


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
            raw_title = js_trim(gate_match.group(2))
            id_match = _ID_MATCH_RE.match(raw_title)
            gate_id = id_match.group(1) if id_match else "L" + str(index + 1)
            title = js_trim(raw_title[len(id_match.group(0)):]) if id_match else raw_title
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
            value = js_trim(attr_match.group(3))
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
            reason = js_trim(abandon_match.group(2))
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
            declared = [t for t in (js_trim(i) for i in owns_match.group(1).split(",")) if t]
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
                # _js_json_text, not a bare json.dumps, against gates.mjs:418's
                # `JSON.stringify(gate.expect)`. The bare call was the SECOND instance of the
                # lease-write defect, in USER-VISIBLE text. MEASURED, `EXPECT: /src/café/out.txt/`:
                #     JS  EXPECT "/src/café/out.txt/"
                #     PY  EXPECT "/src/caf<6-char backslash-u escape>/out.txt/"
                # Written in words because spelling the escape literally renders it back as the
                # character, making the two lines identical -- a diff that proves nothing.
                warnings.append("gate " + gate["id"] + ": EXPECT " + _js_json_text(gate["expect"]) +
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


def format_document(doc):
    """Port of formatDocument: re-join a parsed ledger into the bytes it came from.

    Four lines, and the highest-risk function in this half -- not because it is subtle, but
    because gate-check WRITES its result over the user's tracked ledger. A defect here does not
    fail a check; it corrupts a file, and the corruption is invisible until someone diffs. The
    guarantee that matters is therefore the ROUND TRIP, asserted byte-for-byte over every
    fixture: parse_gates(text) -> format_document(...) must return `text` unchanged, including
    its CRLF endings and the presence or absence of a final newline.
    """
    output = doc["eol"].join(doc["lines"])
    if doc["finalNewline"] and not output.endswith(doc["eol"]):
        output += doc["eol"]
    return output


# `\Z` not `$`: JS `$` does not match before a trailing newline, so a label of "x.md\n" keeps its
# extension in the oracle and a `$`-spelled port would strip it. That one HAS a witness in the
# qualify corpus.
#
# re.ASCII is DEFENSIVE and has NO witness, which is the honest label. A review predicted U+217F
# (SMALL ROMAN NUMERAL ONE THOUSAND) folds onto "m" under Python's Unicode IGNORECASE and not
# under JS's, making the flag load-bearing -- MEASURED, and it does not: `re.compile(r"\.md\Z",
# re.IGNORECASE).search(".ⅿd")` is None with or without re.ASCII, and JS agrees. No
# character folds onto "m" or "d", so nothing distinguishes the flag's presence for THIS pattern
# and a corpus row for it would assert nothing. Kept because the flag costs nothing and states
# the intent; recorded as unwitnessed rather than claimed as tested.
_MD_SUFFIX_RE = re.compile(r"\.md\Z", re.IGNORECASE | re.ASCII)


def js_basename(value):
    """`path.basename(String(value))` -- which os.path.basename is NOT.

    MEASURED: Node strips TRAILING SEPARATORS first, so basename("a/b/") is "b". Python's
    returns "" for the same input, and "" would then qualify every gate in such a ledger as
    ":<id>" -- a different label for the same gate in the two runtimes. Three of thirteen probe
    cases differed. `.rstrip("/")` before the split reproduces Node's rule for all thirteen.

    PLATFORM-CORRECT, not posix-only. `node:path`'s basename is platform-specific and so is
    os.path's, and stripping "/" alone is wrong on Windows: node's win32 basename splits on BOTH
    separators, so basename("a\\b\\") is "b" there, while ntpath.basename("a\\b\\".rstrip("/"))
    is "" -- the same empty-label defect this function exists to prevent, just one platform over.
    `os.sep + (os.altsep or "")` IS the platform's separator set: "/" on posix (where a backslash
    is a legal filename character node's posix basename keeps), "\\/" on Windows.

    SCOPE OF THE CLAIM, narrowed after review: the STRIP SET is right on both platforms, but that
    does not make this equal to node's basename in general. ntpath.basename itself still diverges
    on a UNC root -- node's win32 basename is purely lexical and answers "share" for
    "\\\\server\\share", while ntpath treats the whole thing as a drive with an empty tail and
    answers "". That gap predates this function and is untouched by it; it would surface as the
    same empty-label defect, and no test in this repository runs on Windows at all.
    """
    return os.path.basename(str(value).rstrip(os.sep + (os.altsep or "")))


def _normalize_string(path):
    """node's internal `normalizeString`: resolve `.` and `..` LEXICALLY, touching no disk.

    Split out because `js_resolve` and `js_relative` both need it and `posixpath.normpath` is
    not a substitute -- normpath PRESERVES a leading double slash (POSIX makes exactly two
    implementation-defined) where node collapses it. That single difference is why
    `os.path.abspath("//a")` is `//a` against node's `/a`, and it lands on `approvalPath`'s
    sha256 identity, so it is not cosmetic. Normalizing a body WITHOUT its leading slash and
    re-attaching one, which is what node does, sidesteps the whole question.

    NO `allow_above_root` PARAMETER, unlike node's. It was written, and a mutation control
    then showed the `True` arm was UNREACHABLE and could not be made to redden: `js_resolve`
    only stops scanning once it has an absolute segment, and it falls back to `os.getcwd()`,
    which is always absolute -- so `not absolute` is always False at the call. `js_relative`
    reaches this only through `js_resolve`. A branch no caller can enter is exactly the dead
    code this project deletes rather than ships with an untestable control.

    REINSTATE IT if a `js_normalize` or a relative-path `js_join` is ever ported -- those ARE
    node's callers that pass `True`, and they need the `..` segments kept rather than dropped.
    """
    out = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if out and out[-1] != "..":
                out.pop()
        else:
            out.append(segment)
    return "/".join(out)


def js_dirname(value):
    """`path.dirname`, which `os.path.dirname` is NOT -- measured, 5 of 10 probe cases differ.

    `/a/` -> `/` vs `/a`; `a` -> `.` vs `""`; `""` -> `.` vs `""`; `//` -> `/` vs `//`;
    `/a//b//` -> `/a/` vs `/a//b`. Transcribed from node's own loop rather than approximated,
    because the cases that differ are exactly the irregular ones an approximation gets wrong:
    node scans from the END for the last separator that is not part of a trailing run, and has
    a special answer for a path rooted at a DOUBLE slash.

    Only ONE call site exists today (`gate-check.mjs:336`, on `resolve()` output, where a
    trailing slash cannot occur) -- so this is not fixing a live bug. It exists so the next
    call site cannot introduce one, which is the same reasoning `_js_join`'s docstring gives
    for why a rule applied per-site does not hold.
    """
    path = os.fspath(value)
    if len(path) == 0:
        return "."
    has_root = path[0] == "/"
    end = -1
    matched_slash = True
    for i in range(len(path) - 1, 0, -1):
        if path[i] == "/":
            if not matched_slash:
                end = i
                break
        else:
            matched_slash = False
    if end == -1:
        return "/" if has_root else "."
    # A path rooted at `//` keeps BOTH slashes here, unlike everywhere else in node's posix
    # module. Not an oversight in the port: node returns the literal "//" for this case.
    if has_root and end == 1:
        return "//"
    return path[:end]


def js_resolve(*parts):
    """`path.resolve`: right-to-left until an absolute segment, then normalize.

    THERE IS NO `os.path` COUNTERPART, which is the point. `abspath` alone ignores the
    argument list; `abspath(join(...))` is a `join` that was then absolutised, and `join` does
    not reset on an absolute segment -- `resolve("/a", "/b")` is `/b` while node's
    `join("/a", "/b")` is `/a/b`. A stand-in built that way looks close and is not.

    The leading-`//` collapse falls out of the algorithm rather than being special-cased: the
    body is normalized WITHOUT its root and a single `/` is prepended, so any number of
    leading slashes reduces to one. That is node's own construction.
    """
    resolved = ""
    absolute = False
    index = len(parts) - 1
    while index >= -1 and not absolute:
        # index -1 is the CWD, which node consults only if no argument was absolute.
        segment = os.fspath(parts[index]) if index >= 0 else os.getcwd()
        index -= 1
        if len(segment) == 0:
            continue
        resolved = segment + "/" + resolved
        absolute = segment[0] == "/"
    body = _normalize_string(resolved)
    # `absolute` is always True here -- the scan only ends by finding an absolute segment, and
    # the index -1 fallback is os.getcwd(). The branch is kept because node's has it and its
    # absence would read as a port omission; see _normalize_string on why the sibling dead arm
    # was deleted instead. If a caller ever passes something that makes this False, "." is
    # node's answer for an empty relative result.
    if absolute:
        return "/" + body
    return body if body else "."


def js_relative(source, target):
    """`path.relative`, which `os.path.relpath` is NOT: equal paths give "" here and "." there.

    That difference is inert in `pathIsInside` (an empty `rel` satisfies every conjunct of the
    fallback test, so the `rel === ""` clause is provably redundant), but it is NOT inert in
    general, and `relpath` also differs by resolving through a different normalizer.

    Node compares the two RESOLVED paths segment-wise, tracking the last common separator,
    then emits one `..` per remaining segment on the source side. Transcribed rather than
    rebuilt from `os.path.relpath` semantics.
    """
    source_path = js_resolve(source)
    target_path = js_resolve(target)
    if source_path == target_path:
        return ""
    # Skip the root slash on both sides; js_resolve guarantees exactly one.
    from_start, from_end = 1, len(source_path)
    to_start, to_end = 1, len(target_path)
    from_len, to_len = from_end - from_start, to_end - to_start
    length = min(from_len, to_len)
    last_common_sep = -1
    i = 0
    while i < length:
        char = source_path[from_start + i]
        if char != target_path[to_start + i]:
            break
        if char == "/":
            last_common_sep = i
        i += 1
    if i == length:
        if to_len > length:
            if target_path[to_start + i] == "/":
                return target_path[to_start + i + 1:]
            if i == 0:
                return target_path[to_start + i:]
        elif from_len > length:
            if source_path[from_start + i] == "/":
                last_common_sep = i
            elif i == 0:
                last_common_sep = 0
    out = []
    for i in range(from_start + last_common_sep + 1, from_end + 1):
        if i == from_end or source_path[i] == "/":
            out.append("..")
    return "/".join(out) + target_path[to_start + last_common_sep:]


def qualify(file_or_label, gate_id):
    return _MD_SUFFIX_RE.sub("", js_basename(file_or_label)) + ":" + gate_id


# ---------------------------------------------------------------------------------------------
# The gate-definition digest. This one is security-relevant: it is what binds a recorded
# approval to the exact CHECK/EXPECT/CWD that was approved, so a digest that differs between
# the runtimes does not fail loudly -- it silently reports "gate not approved" for a gate the
# other runtime approved, and the cause is misattributed to the approval store.
# ---------------------------------------------------------------------------------------------

MAX_CHECK_OUTPUT_BYTES = 1024 * 1024
MAX_AUTOMATIC_EVIDENCE_CHARS = 900


_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _reject_float(value):
    """Refuse a float anywhere in a payload bound for _js_json_text.

    This helper claims to reproduce JSON.stringify, and for str/int/bool/None/dict/list it does.
    It does NOT for a float, because `json.dumps` renders one with Python's repr and JS has its
    own algorithm. MEASURED, this exact helper against `JSON.stringify(x, null, 2)`:

        -0.0       JS `0`        PY `-0.0`
        1e-7       JS `1e-7`     PY `1e-07`      (Python zero-pads the exponent)
        0.000001   JS `0.000001` PY `1e-06`      (different notation thresholds)

    jsapi._js_number exists precisely for this and digest_drive.py uses it -- but that is the
    DRIVER's hand-rolled serializer, deliberately independent so the differential does not
    compare gates.py with itself. The consequence, which is the reason this guard exists:
    digest-diff.sh proves the DRIVER spells floats like JS, and proves nothing at all about
    THIS function, which is what production hashes with (gate_check.py:619, gates.py:1011).

    No float reaches here today -- `_validated_integer` returns `int(...)`, so timeoutMs and
    friends are ints, and every other field is a str, int or bool. So this cannot fire now. It
    exists because "unreachable today" is how the previous latent divergence in this same
    function shipped: a wrong spelling on a HASHING path is silent, and the corpus that looks
    like it covers it does not. Fail fast and loudly instead of hashing a value the oracle
    spells differently.
    """
    if isinstance(value, float):
        raise TypeError(
            "_js_json_text cannot spell a float the way JSON.stringify does; "
            "route it through jsapi._js_number (got %r)" % (value,))
    if isinstance(value, dict):
        # KEYS as well as values. `json.dumps` COERCES a non-str key to a string, and for a
        # float key that coercion is the same repr this guard exists to reject -- so a
        # values-only walk would leave the hole open in the one place it is least visible.
        for key, item in value.items():
            _reject_float(key)
            _reject_float(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_float(item)
    # No branch for Decimal/Fraction/complex: MEASURED, json.dumps raises TypeError on each
    # rather than emitting anything, so they are already loud. An int SUBCLASS renders as its
    # int value, which JS spells the same way. float is the only natively-serializable Python
    # type whose spelling diverges, which is what makes this guard complete rather than a
    # sample of the types someone happened to think of.
    #
    # DO NOT ADD A `default=` HOOK to _js_json_text without extending this walk. Those three
    # types are safe because json.dumps REFUSES them; a default= hook is exactly the natural way
    # to start accepting a Decimal, and it would convert all three from loud refusals into
    # silent divergences that this guard, as written, would not see.


def _js_json_text(value, indent=None):
    """`JSON.stringify(value)` as TEXT -- including its handling of a lone surrogate.

    `indent=2` gives `JSON.stringify(value, null, 2)`: Python's pretty separators are `,` +
    newline and `": "`, which is what JS emits too, so only the compact form needs the explicit
    `separators`. The parameter exists so the pretty writers can reuse this ONE surrogate-aware
    serializer instead of hand-rolling a second `json.dumps` -- a bare one shipped at the lease
    write and diverged from the oracle on the first non-ASCII glob (see claim_leases).

    `json.dumps(..., ensure_ascii=False)` is right for ordinary non-ASCII (JSON.stringify emits
    the character, and the default ensure_ascii=True would escape it, changing every digest).
    But it leaves a LONE SURROGATE raw, and the str it returns then CANNOT be encoded: sha256's
    `.encode("utf-8")` raises UnicodeEncodeError where the oracle happily returns a digest.
    MEASURED, both runtimes, same gate:
        JS  gateDefinitionDigest -> 83768e4e4ac7e3b427800885528580d4f7af9609d3d2a406a79a7a21b84a50dd
        PY  -> UnicodeEncodeError: surrogates not allowed
    A crash where the oracle returns a value is the loudest possible divergence, and it sits on
    the APPROVAL IDENTITY path -- the highest-ranked hazard in this port.

    JSON.stringify ESCAPES a lone surrogate to the six ASCII characters `\\ud800`, so the fix is
    to do the same and hand sha256 pure ASCII for those code points. Ordinary non-ASCII stays
    raw. Verified: this reproduces the oracle's hex exactly for the measured gate above.

    THE PREMISE THAT MADE THIS LOOK UNREACHABLE WAS FALSE, and it is written into two other
    comments in this file (:96-102 and the write side): "a Python str carries lone surrogates
    only from a surrogateescape decode, which nothing in this port performs". CPython
    surrogateescape-decodes `sys.argv` ITSELF -- no explicit decode call appears anywhere.
    Measured: `\\xff` in argv is U+DCFF to CPython and U+FFFD to node, so the two runtimes
    diverge BEFORE any quoting, and gate_check.py's `--cwd` carries the result straight into
    this payload. A justification that rests on "nothing calls X" has to account for what the
    RUNTIME calls on the program's behalf.

    NOT applied to sha256() itself: that function's divergence is a different one (Node's
    createHash substitutes U+FFFD, hashing a LOSSY key so two paths can share one lock; this
    port refuses instead), and its comment argues fail-closed is the safer answer. That case is
    about hashing a raw path. This one is about reproducing JSON.stringify's TEXT, where the
    oracle's own escaping means no surrogate ever reaches its hash.
    """
    _reject_float(value)
    separators = None if indent is not None else (",", ":")
    return _LONE_SURROGATE_RE.sub(
        lambda m: "\\u%04x" % ord(m.group(0)),
        json.dumps(value, indent=indent, separators=separators, ensure_ascii=False,
                   allow_nan=False))


def gate_definition_digest(gate):
    """sha256(JSON.stringify([...])) over the fields that define a runnable gate, or None.

    Two serialization details decide whether the hex matches the oracle's, and BOTH are silent:

    - `separators=(",", ":")`. `json.dumps` defaults to `", "` and `": "`; JSON.stringify emits
      no spaces at all. Every digest would differ, for every gate.
    - `ensure_ascii=False`. `json.dumps` defaults to escaping non-ASCII to \\uXXXX, while
      JSON.stringify emits the character. So the two agree on an ASCII gate and diverge the
      moment a CHECK, EXPECT or CWD carries one non-ASCII byte -- which is why the differential
      row feeds it the unicode fixture rather than a plain gate.
    """
    if not isinstance(gate, dict):
        return None
    check = gate.get("check")
    expect = gate.get("expect")
    if not isinstance(check, str) or check == "":
        return None
    if not isinstance(expect, str) or expect == "":
        return None
    # KNOWN GAP, live only for a HAND-BUILT gate. `str(cwd)` is not `String(cwd)` for a non-str:
    # True -> "True" vs "true", 1.0 -> "1.0" vs "1", [1] -> "[1]" vs "1". Every gate reaching here
    # from parse_gates carries a str or None, so nothing today can differ -- but
    # hardening-tests.mjs builds gate objects by hand and calls gateDefinitionDigest on them, so
    # porting THAT suite is what makes this reachable, and the failure would be a silently
    # different digest rather than an error. Needs a js_string() helper at that point, not before.
    cwd = gate.get("cwd")
    payload = [
        "agents-discipline.gate-definition",
        1,
        check,
        expect,
        None if cwd is None else str(cwd),
    ]
    # No js_json_object() here: the payload is a flat list of primitives with no dict at any
    # depth, so the JS key-order helper has nothing to reorder. Adding it would be dead code
    # that reads as if key order were a live hazard in this function. It is not -- and it
    # cannot become one without editing the literal above.
    return sha256(_js_json_text(payload))


_DIGEST_HEX_RE = re.compile(r"^[a-f0-9]{64}\Z")


def automatic_evidence_prefix(definition_digest):
    # `String(definitionDigest || "")` -- None and "" both reach the test as "", which fails it.
    if not _DIGEST_HEX_RE.match("" if not definition_digest else str(definition_digest)):
        raise ValueError("automatic evidence needs a full lowercase SHA-256 definition digest")
    return "automatic-evidence=v1; definition-sha256=" + definition_digest + ";"


# `\Z`, not `$`: JS `$` does not match before a trailing newline, so `"pending\n"` is HUMAN
# evidence to the oracle and would be "pending" to a `$`-spelled port -- a gate the oracle calls
# met-by-a-human becomes an unmet-no-evidence gate. re.ASCII with re.IGNORECASE for the same
# reason the id patterns carry it: JS `i` on an ASCII pattern folds ASCII, Python's folds Unicode.
_PENDING_RE = re.compile(r"^pending\Z", re.IGNORECASE | re.ASCII)

# `.` is NOT Python's `.`: JS excludes \n \r U+2028 U+2029, Python excludes only \n. Spelled as a
# class so `shell=\r` is rejected in both. Everything else here is ASCII-only by construction, so
# the \d-vs-Unicode trap cannot fire -- the digits are written [0-9] rather than \d for that.
_AUTOMATIC_SUCCESS_RE = re.compile(
    r"^ exit=0; EXPECT=matched; output-sha256=[a-f0-9]{64}; "
    r"output-bytes=(0|[1-9][0-9]{0,6}); shell=[^\n\r"
    # chr(), because writing these two as CHARACTERS puts raw U+2028/U+2029 bytes in this
    # file -- which is what the first version did. An invisible-character scan I ran over
    # this very line reported it clean, so a scan is not a substitute for not writing them.
    + chr(0x2028) + chr(0x2029) + "]"
)


def classify_gate_evidence(gate):
    """Port of classifyGateEvidence: pending | automatic-current | automatic-stale | human."""
    raw = gate.get("evidence") if isinstance(gate, dict) else None
    # js_truthy/js_string, NOT `not raw` and `str(raw)`. `String((gate && gate.evidence) || "")`
    # coerces on JS rules, and Python's disagree in both directions -- MEASURED against the
    # oracle: evidence=NaN is "pending" there and was "human" here (NaN is falsy in JS, truthy in
    # Python); evidence={} is "human" there and was "pending" here (an empty object is truthy in
    # JS, falsy in Python). Both flip a gate's met/unmet verdict. Unreachable from parse_gates,
    # which only ever stores a str or None; reachable from a hand-built gate, which is how
    # hardening-tests.mjs calls this.
    evidence = "" if raw is None or not js_truthy(raw) else js_string(raw)
    if evidence == "" or _PENDING_RE.match(evidence):
        return "pending"
    definition_digest = gate_definition_digest(gate)
    if definition_digest is not None:
        prefix = automatic_evidence_prefix(definition_digest)
        # js_slice/js_length, not [len:] and len(): the oracle measures BOTH the slice offset and
        # the 900 bound in UTF-16 code units. An evidence string carrying astral characters is
        # therefore cut at a different point and measured at a different length by a naive port,
        # and the verdict this function returns decides whether a gate counts as MET.
        deciding = js_slice(evidence, js_length(prefix))
        success = _AUTOMATIC_SUCCESS_RE.match(deciding)
        if (js_length(evidence) <= MAX_AUTOMATIC_EVIDENCE_CHARS
                and evidence.startswith(prefix)
                and success and int(success.group(1)) <= MAX_CHECK_OUTPUT_BYTES):
            return "automatic-current"
    if evidence.startswith("automatic-evidence=") or evidence.startswith("exit=0; shell="):
        return "automatic-stale"
    return "human"


def gate_state(gate, abandoned):
    """Port of gateState. `abandoned` is a dict here and a Map in the oracle; both are `in`."""
    # .get(), not ["id"]: the oracle does `abandoned.has(gate.id)`, and a gate with no id gives
    # `has(undefined)` -> false, so it carries on. `gate["id"]` RAISES KeyError instead -- a crash
    # where the oracle returns a verdict. Measured on a hand-built gate: oracle stale-unmet, port
    # KeyError. None is not a legal id, so it can never collide with a real abandoned key.
    if gate.get("id") in abandoned:
        return "abandoned"
    if not gate.get("checked"):
        return "unmet"
    evidence = classify_gate_evidence(gate)
    runnable = gate_definition_digest(gate) is not None
    if runnable:
        return "met" if evidence == "automatic-current" else "stale-unmet"
    if evidence == "pending":
        return "unmet-no-evidence"
    if evidence in ("automatic-current", "automatic-stale"):
        return "stale-unmet"
    return "met"


def tail(output, max_chars=240):
    """Port of tail: the last two non-blank lines, joined, capped at `max` UTF-16 code units."""
    lines = [t for t in (js_trim(line) for line in re.split(r"\r?\n", str(output))) if t]
    return js_slice(" | ".join(lines[-2:]) or "(no output)", 0, max_chars)


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


def same_file_identity(left, right):
    """Two stat results name the same file. Mapping-or-attribute, because the oracle's callers
    pass plain objects (`{dev, ino}`) as well as real stat results, and hardening-tests does
    exactly that."""
    def field(value, name):
        return value[name] if isinstance(value, dict) else getattr(value, "st_" + name)
    return field(left, "dev") == field(right, "dev") and field(left, "ino") == field(right, "ino")


def _js_join(*parts):
    """`path.join`, which differs from `os.path.join` in THREE ways, not one.

    Measured: node's join("a//", "b") is "a/b" and Python's is "a//b"; ("a/.", "b") gives
    "a/b" against "a/./b". The port already carried this fix at scope_root and
    status_log_path and then reintroduced the bug at every site added with the discovery
    helpers -- so the lesson is that a rule applied per-site does not hold. One helper, used
    everywhere the oracle says path.join, is the form that cannot drift.

    normpath is the right primitive here and not merely close: node's join normalizes `.`,
    duplicate separators and interior `..` lexically, without touching the filesystem, which
    is exactly normpath's contract. A trailing separator survives BOTH (node keeps "a/b/"),
    which normpath would strip -- hence the re-append.
    """
    # NOT os.path.join. Measured: os.path.join DISCARDS everything before an absolute
    # component -- ("/a", "/b") gives "/b" where node gives "/a/b" -- because that discard is
    # `resolve`'s rule, not `join`'s. normpath cannot recover the dropped prefix, so a helper
    # built on os.path.join was wrong before it ever normalized anything. Node's algorithm is
    # instead: drop ZERO-LENGTH segments, concatenate with the separator, normalize, and
    # answer "." for an empty result.
    # os.fspath, NOT str(). node VALIDATES every argument -- path.join("a", 7) throws
    # ERR_INVALID_ARG_TYPE -- where a str() coercion answered "a/7", and for None "a/None":
    # a PLAUSIBLE path that then lstats ENOENT and reports as an ordinary missing file rather
    # than the programming error it is. os.fspath raises TypeError for an int, None or bool
    # (and bytes then fails at the join), which is node's outcome, while still accepting a
    # pathlib.Path -- the idiomatic Python path type, with no node counterpart, which worked
    # under the previous os.path.join and must keep working.
    #
    # An explicit isinstance guard raising a hand-written "Path must be a string" sat here
    # briefly and was REMOVED: os.fspath already rejects exactly the same values, so the guard
    # changed no outcome any test could observe, and its message was INVENTED -- node's real
    # text is `The "path" argument must be of type string. Received type number (7)`. A guard
    # that adds an unmeasured message and no witnessed behaviour is worse than none.
    kept = [os.fspath(part) for part in parts if part != ""]
    if not kept:
        return os.curdir                      # path.join() and path.join("") are both "."
    joined = os.sep.join(kept).replace("/", os.sep) if os.sep != "/" else "/".join(kept)
    normalized = os.path.normpath(joined)
    # POSIX gives a LEADING double slash implementation-defined meaning and normpath preserves
    # it; node's posix join does not honour that rule and collapses it. Found by a 3000-case
    # randomized differential, not by hand: ["//"] answers "/" in node and "//" from normpath,
    # and every case whose first segment was "/" inherited the same extra slash. Guarded to
    # posix because on Windows a leading "\\" is a UNC root, which node's win32 join keeps.
    # Only EXACTLY two: normpath already collapses three or more ("///a" -> "/a"), so a
    # "///" prefix cannot reach this line. The earlier guard against it was dead code.
    if os.sep == "/" and normalized.startswith("//"):
        normalized = normalized[1:]
    # A trailing separator survives node's join ("a", "b/") -> "a/b/", where normpath strips
    # it. Read the intent off `kept[-1]`, NOT off `joined`: joined could have acquired the
    # separator from an empty final segment, which is how the first version answered "a/" for
    # ("a", "") where node answers "a".
    if kept[-1].endswith(("/", os.sep)) and not normalized.endswith(os.sep):
        normalized += os.sep
    return normalized


# PUBLIC, and the missing underscore is the point. CALLERS OUTSIDE THIS MODULE: gate_lint.py,
# ledger_check.py, dispatch.py and dispatch_check.py -- four modules pinning this function's
# exact OUTPUT, which is itself pinned to node's `error.message`. Its signature and its measured
# errno scope are a CONTRACT, not a private choice.
#
# It carried a leading underscore until four callers existed, and a note here naming TWO of them.
# That note was written when two was the count and was already stale one commit later, which is
# the argument against the underscore rather than for it: a marker saying "internal, refactor
# freely" on the most externally-constrained function in this file misdirects exactly the reader
# who greps before changing it, and a hand-maintained caller list decays the same way the
# commit-tally table did. The rename is cheap; deferring it as churn at two callers was right
# then and stopped being right here.
# libuv ships its OWN error table; os.strerror reads the C library's, and they agree FAR less
# often than this function's first version assumed. MEASURED by forcing each code through node
# and comparing to os.strerror(n).lower(): 3 of 7 forceable codes matched, 4 did not.
#
#     EACCES ENOENT ENOTDIR EBADF   agree
#     EEXIST        node "file already exists"                 vs "file exists"
#     EISDIR        node "illegal operation on a directory"    vs "is a directory"
#     ELOOP         node "too many symbolic links encountered" vs "too many levels of symbolic links"
#     ENAMETOOLONG  node "name too long"                       vs "file name too long"
#
# The lowercase-strerror rule was inferred from the three COMMON codes, which happen to be the
# three that agree -- a sample that could not have revealed the rule was wrong. EEXIST matters
# most: it is reachable through mkdirs, so it is not a theoretical entry.
#
# KEYED ON THE ERRNO ALONE, AND THAT WAS MEASURED RATHER THAN ASSUMED -- the question this table
# would otherwise repeat from _node_realpath, whose bug was a string measured through one syscall
# and generalized. Each entry here was ALSO first measured through one syscall, so the same doubt
# applied. Re-forced through several:
#     EEXIST  mkdir / symlink / open  -> "file already exists"              (identical)
#     EISDIR  open / read             -> "illegal operation on a directory" (identical)
#     ELOOP   stat / open             -> "too many symbolic links encountered" (identical)
# So libuv's table is per-CODE, not per-call, and one string per errno is the right shape.
# THE MECHANISM, which is why the measurement had to come out that way: uv_strerror() is a pure
# errno -> static-string lookup and uvException() composes `<code>: <prose>, <syscall> ...` around
# it, so the prose cannot depend on the call. The right answer was reached before this argument
# existed, which is its own hazard -- a right answer with no mechanism is inherited as a method.
#
# THE FALLBACK IS PLATFORM-DEPENDENT and the overrides are not: os.strerror reads the C library's
# table, which differs BETWEEN libcs -- glibc and BSD/macOS word several codes differently -- while
# libuv's is one fixed English table compiled in. That is the durable reason to grow this table by
# measurement rather than trust strerror to keep agreeing, and it is first-hand: the four entries
# above were measured on Darwin.
# NOT the locale, which an earlier version of this note claimed: CPython does not call
# setlocale(LC_MESSAGES) at startup, so a plain python3 stays in the C locale for messages whatever
# the environment says. The conclusion was right and the mechanism was invented -- worth correcting
# in place, because a wrong mechanism sends the next reader chasing a bug that cannot happen.
# STILL INCOMPLETE, and deliberately not guessed: codes that could not be forced here (EMFILE,
# ENFILE, ENOMEM, EOVERFLOW, ENOSPC, EROFS, EPERM) are UNCONFIRMED either way. An absent entry
# falls back to strerror, which is a coin flip on this evidence -- so add measurements, never
# assumptions.
_LIBUV_PROSE = {
    errno.EEXIST: "file already exists",
    errno.EISDIR: "illegal operation on a directory",
    errno.ELOOP: "too many symbolic links encountered",
    errno.ENAMETOOLONG: "name too long",
}


def node_fs_message(error, syscall):
    """Node's `error.message` for a failed fs call, which is NOT Python's `str(error)`.

    The oracle interpolates `error.message` at this one site (it reads `error.code`
    everywhere else, which is what _err_code is for). Node's shape is
    `CODE: lowercase prose, syscall 'path'`; Python's str() is `[Errno N] Prose: 'path'`.

    MEASURED against node for EACCES, ENOENT and ENOTDIR, where `os.strerror(n).lower()`
    reproduces libuv's prose exactly. Those three are the COMMON cases, NOT the complete set:
    a scan can also raise ELOOP, EMFILE, ENFILE, ENOMEM and EOVERFLOW, and ELOOP looks like a
    live counterexample -- macOS strerror says "Too many levels of symbolic links" where
    libuv's own table reads "too many symbolic links encountered", a different sentence rather
    than a different case. Unverified against node (hard to force through this call, since
    _real_directory_inside rejects a symlinked directory first), so treat any errno outside
    the measured three as UNCONFIRMED rather than assuming the lowercase rule generalizes. Falling back to the errno name alone would
    be a silent, smaller divergence, so an unmatched code still produces this shape.
    """
    # An ALREADY-ATTACHED message wins over the caller's `syscall`, because the caller guessed
    # and node_call knew. Seven sites pass "open" for read_stable_regular_file, which makes
    # eight syscalls; without this branch a failing fstat/lstat/realpath/read would be
    # relabelled `open` right here, at the one site whose whole job is the syscall token.
    attached = getattr(error, "_node_message", None)
    if attached is not None:
        return attached
    code = _err_code(error)
    number = getattr(error, "errno", None)
    prose = ((_LIBUV_PROSE.get(number) or os.strerror(number).lower())
             if number is not None else code)
    path = getattr(error, "filename", None)
    # TWO-PATH OPERATIONS have their own grammar in node, and omitting it is a divergence no
    # syscall token can fix: uvException builds `<code>: <prose>, <syscall> '<path>' -> '<dest>'`
    # for rename/link/symlink. MEASURED on a failing rename into an unwritable directory --
    #     node    EACCES: permission denied, rename '<src>' -> '<dst>'
    #     one-arg EACCES: permission denied, rename '<src>'
    # -- and CPython populates `filename2` for exactly these calls, so the destination was always
    # available here and simply never read. Found by review, not by the sweep: the sweep looks at
    # sites that INTERPOLATE an error, and this is a defect in the interpolation's own grammar.
    destination = getattr(error, "filename2", None)
    if path is None:
        suffix = ", " + syscall
    elif destination is None:
        suffix = ", " + syscall + " '" + str(path) + "'"
    else:
        suffix = ", " + syscall + " '" + str(path) + "' -> '" + str(destination) + "'"
    return code + ": " + prose + suffix


def _named_entry(file):
    """The oracle's namedEntry: any lstat outcome EXCEPT ENOENT counts as present.

    The asymmetry is deliberate upstream -- an entry that exists but cannot be stat'ed must
    stay visible as bad input rather than collapse into the harmless "no such thing" answer.
    Reproduce the polarity exactly: an inverted default here turns an unreadable scope into a
    silent success.
    """
    try:
        os.lstat(file)
        return True
    except Exception as error:
        # `except Exception`, matching the oracle's BARE `catch`, not `except OSError`. Measured:
        # namedEntry(null) throws ERR_INVALID_ARG_TYPE in node, whose code is not "ENOENT", so
        # the oracle answers TRUE; a narrow OSError clause lets Python's TypeError propagate
        # instead. Same for an embedded NUL (ValueError vs ERR_INVALID_ARG_VALUE). Unreachable
        # from resolve_target today -- the scope charset excludes NUL -- but this function's
        # whole contract is "any failure except ENOENT means present", and a clause that lets
        # some failures escape does not implement it.
        return getattr(error, "errno", None) != errno.ENOENT


def _real_directory_inside(root, directory):
    """A real (non-symlink) directory whose resolved path lies inside the resolved root."""
    try:
        named = os.lstat(directory)
        if statmod.S_ISLNK(named.st_mode) or not statmod.S_ISDIR(named.st_mode):
            return False
        # strict=True, because the oracle's realpathSync THROWS on a missing path and lands in
        # the catch (=> False). Python's default realpath silently returns a non-existent path,
        # which would then be compared and could answer True for a path that is not there.
        return _path_is_inside(os.path.realpath(os.path.abspath(root), strict=True),
                               os.path.realpath(directory, strict=True))
    except Exception:
        # Bare `catch` in the oracle; see _named_entry for why the narrow clause is wrong.
        return False


def list_scopes(root):
    directory = _js_join(root, AGENTS_DISCIPLINE_DIR)
    if not os.path.exists(directory):
        return []
    try:
        if not _real_directory_inside(root, directory):
            return []
        # is_dir(follow_symlinks=False) is BOTH of the oracle's first two filters at once:
        # withFileTypes dirents carry lstat semantics, so a symlink-to-directory is
        # isDirectory()=false there. Following symlinks here would admit exactly the entry the
        # oracle's second filter exists to reject.
        # js_sort_key even here, where validate_scope_id has already confined every surviving
        # name to [A-Za-z0-9._-] and the two orders provably agree. The oracle spells both
        # sorts the same way; spelling one of them differently because THIS one is currently
        # unreachable by the divergence leaves a trap for whoever widens the id charset.
        return sorted((entry.name for entry in os.scandir(directory)
                       if entry.is_dir(follow_symlinks=False) and entry.name != "locks"
                       and not validate_scope_id(entry.name)
                       and _real_directory_inside(root, _js_join(directory, entry.name))),
                      key=js_sort_key)
    except OSError:
        # Deliberately NARROWER than the oracle's bare `catch`, unlike _named_entry above.
        # A bare catch here would turn a typo in this comprehension into "no pipelines
        # configured" -- silently, which is the exact failure mode this project keeps hunting.
        # There is nothing to be faithful TO: a Python AttributeError has no counterpart in
        # the oracle's code, so swallowing it reproduces no observable oracle behaviour and
        # only costs the crash that would locate the bug.
        return []


def _markdown_discovery(root, directory):
    if not _named_entry(directory):
        return {"files": [], "errors": []}
    try:
        if not _real_directory_inside(root, directory):
            return {"files": [], "errors": [
                "gate directory must be a real directory inside the repository: " + directory]}
        # Every named Markdown entry, unfiltered by type. Consumers run the stable-file check,
        # so a FIFO, link or directory surfaces as an error rather than vanishing as "no gates".
        # Joins FIRST, then sorts -- the oracle's order of operations, kept rather than
        # "simplified" to sorting the names. The two agree only because the prefix is
        # identical for every entry here, and reasoning that out again at each edit is exactly
        # how a port drifts; matching the oracle costs nothing and needs no such argument.
        files = sorted((_js_join(directory, entry.name) for entry in os.scandir(directory)
                        if entry.name.endswith(".md")), key=js_sort_key)
        return {"files": files, "errors": []}
    except OSError as error:
        # OSError, not Exception -- see list_scopes.
        # THE COMMENT HERE DESCRIBED THE DOUBLING AND DID NOT GUARD AGAINST IT: it noted that a
        # no-errno error "degrades node_fs_message twice over ... emitting the message doubled
        # around a colon", and then called the helper unguarded. MEASURED on an authored
        # OSError:
        #     unguarded  gate directory refused: gate directory refused, scandir
        #     guarded    gate directory refused
        # An `except OSError` catches the port's OWN authored errors, which carry no errno, so
        # the shape the comment warned about is the shape this clause admits. Unreachable today
        # -- os.scandir always sets an errno and nothing authored is raised inside the try --
        # but this was the LAST unguarded call to the helper in the port, in the module that
        # DEFINES it, and it is the sixth site where a comment reasoned correctly about a case
        # the code then did not handle.
        # `scandir` is the THIRD syscall constant in this sweep, after `open` and `stat`, and it
        # is measured: node names a failed readdir `scandir`, identical in both runtimes.
        # Covered by errno-message-diff.sh row 11.
        return {"files": [], "errors": [
            "cannot inspect gate directory " + directory + ": "
            + (node_fs_message(error, "scandir") if error.errno is not None else str(error))]}


def _scope_discovery(root, scope):
    base = scope_root(root, scope)
    if not _real_directory_inside(root, base):
        return {"files": [], "errors": [
            "scope directory must be a real directory inside the repository: " + base]}
    files = []
    top = _js_join(base, "GATES.md")
    if _named_entry(top):
        files.append(top)
    nested = _markdown_discovery(root, _js_join(base, "gates"))
    files.extend(nested["files"])
    return {"files": files, "errors": nested["errors"]}


def _legacy_discovery(root):
    files = []
    top = _js_join(root, "GATES.md")
    if _named_entry(top):
        files.append(top)
    nested = _markdown_discovery(root, _js_join(root, "gates"))
    files.extend(nested["files"])
    return {"files": files, "errors": nested["errors"]}


def scope_files(root, scope):
    return _scope_discovery(root, scope)["files"]


def legacy_files(root):
    return _legacy_discovery(root)["files"]


def _target_from_discovery(mode, scope, discovery):
    return {"mode": mode, "scope": scope, "files": discovery["files"],
            "discoveryErrors": discovery["errors"]}


def resolve_target(options=None):
    options = options or {}
    root = os.path.abspath(options.get("root") or os.getcwd())
    files = options.get("files") or []
    if files:
        return {"mode": "explicit", "scope": None,
                "files": [os.path.abspath(os.path.join(root, f)) for f in files]}

    scopes = list_scopes(root)
    wanted = options.get("scope") or os.environ.get("AGENTS_DISCIPLINE_SCOPE") or None
    if wanted:
        invalid = validate_scope_id(wanted)
        if invalid:
            return {"mode": "none", "scope": wanted, "files": [], "error": invalid}
        if wanted not in scopes:
            scope_path = scope_root(root, wanted)
            state_path = _js_join(root, AGENTS_DISCIPLINE_DIR)
            # A scope that is physically ABSENT is stale configuration and answers harmlessly.
            # One that EXISTS but list_scopes excluded (link, file, FIFO, outside-root, or an
            # unreadable state container) must stay visible as invalid input instead of
            # collapsing into the same "no such scope" result.
            if (_named_entry(scope_path)
                    or (_named_entry(state_path) and not _real_directory_inside(root, state_path))):
                return _target_from_discovery("scope", wanted, _scope_discovery(root, wanted))
            return {
                "mode": "none", "scope": wanted, "files": [],
                "error": 'no such scope "' + wanted + '" under ' + AGENTS_DISCIPLINE_DIR +
                         "/ (have: " + (", ".join(scopes) or "none") + ")",
            }
        return _target_from_discovery("scope", wanted, _scope_discovery(root, wanted))

    if len(scopes) == 1:
        return _target_from_discovery("scope", scopes[0], _scope_discovery(root, scopes[0]))
    if len(scopes) > 1:
        # Ambiguity is REFUSED, not guessed at. The session-binding branch that used to sit
        # here served the Stop hook, which had no way to pass --scope; every caller now can.
        return {
            "mode": "none", "scope": None, "files": [], "ambiguous": scopes,
            "error": str(len(scopes)) + " pipelines present (" + ", ".join(scopes) +
                     "); pass --scope <id> or set AGENTS_DISCIPLINE_SCOPE. Refusing to guess.",
        }

    legacy = _legacy_discovery(root)
    if legacy["files"] or legacy["errors"]:
        return _target_from_discovery("legacy", None, legacy)
    return {"mode": "none", "scope": None, "files": []}


def status_log_path(root, scope):
    if scope:
        return os.path.join(scope_root(root, scope), "status.log")
    # normpath here too. The scoped branch inherits it from scope_root, and this one was left
    # with a bare join -- so it reproduced exactly the divergence the comment in scope_root
    # describes: "a//" gave "a//agents-discipline-status.log" against the oracle's
    # "a/agents-discipline-status.log". gate-check uses this bare form.
    return os.path.normpath(os.path.join(root, "agents-discipline-status.log"))


def mkdirs(path, mode=None):
    """Create `path` and its missing ancestors, applying `mode` to EVERY directory created.

    PUBLIC, like node_fs_message and for the same reason: it is a CONTRACT, not a private
    detail. It was `_mkdirs` while its only callers lived in this module, and gate_check.py
    then reached for a bare `os.makedirs(directory, mode=0o700, exist_ok=True)` instead --
    inheriting BOTH defects this function exists to prevent (see below and the mkdir token in
    _node_mkdir_error). A helper the neighbours cannot import is a helper they will re-implement.

    os.makedirs applies `mode` only to the final component and leaves intermediates at the
    umask default; Node's recursive mkdirSync applies it to all of them. The tree created here
    is `.agents-discipline/<scope>/`, so taking Python's behaviour would leave the coordination
    directory itself world-readable while its leaf claimed 0700 -- a weaker guarantee than the
    oracle's, in the one place the mode argument exists to provide it.
    """
    if mode is None:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as error:
            raise _node_mkdir_error(error, path) from error
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
        except FileExistsError as exists_error:
            # Concurrent creation by a peer is fine; a FILE at this name is not, and os.mkdir
            # raises the same FileExistsError for both. Swallowing it unconditionally made the
            # function return silently where the oracle's mkdirSync raises EEXIST, and left
            # failing-closed to whatever each caller happened to do next -- true of all three
            # call sites today, and a trap for a fourth. Rebuilding the ORIGINAL error keeps the
            # oracle's error class as well as its outcome; _node_mkdir_error preserves the class
            # (a FileExistsError subclass) and adds only the message shape, so a caller's
            # `except FileExistsError` still fires.
            if not os.path.isdir(directory):
                raise _node_mkdir_error(exists_error, path) from exists_error
        except OSError as error:
            raise _node_mkdir_error(error, path) from error


def _assert_real_directory(path, message):
    # Wrapped for the token: this lstat has no local try, and its callers guard with
    # os.path.exists -- which swallows EACCES -- so a failure here is race-only and would
    # otherwise reach a catch guessing `open`. Same category as gate_check's lstat trio.
    info = node_call("lstat", os.lstat, path)
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
    mkdirs(parent, 0o700)
    _assert_real_directory(parent, parent + " must be a real directory")


def _replace_atomic(temp, target):
    deadline = time.monotonic() + 2.0
    delay = 0.005
    while True:
        try:
            # `rename` VERIFIED against the oracle rather than assumed: replaceAtomic at
            # gates.mjs:220-225 calls renameSync(temp, target), which reports syscall `rename`.
            # The two-path form comes from filename2, which CPython sets for this call.
            # os.replace, NOT os.rename, and the difference is Windows-only and silent: os.rename
            # raises FileExistsError there when the target exists, while os.replace overwrites --
            # which is what renameSync does, since uv_fs_rename maps to MoveFileExW with
            # MOVEFILE_REPLACE_EXISTING. On POSIX the two are identical, so the wrong choice would
            # have passed every test run on this machine.
            node_call("rename", os.replace, temp, target)
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
        mkdirs(parent)
        _assert_real_directory(parent, parent + " must be a real directory")
    try:
        # Wrapped for the token: only FileNotFoundError is handled below, so any other errno
        # (EACCES on an unsearchable parent) escapes to a caller that guesses `open`.
        if statmod.S_ISLNK(node_call("lstat", os.lstat, target).st_mode):
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
        # "utf8") substitutes U+FFFD for a lone surrogate, so a strict failure here means a
        # caller built an unencodable string and should hear about it.
        #
        # AN EARLIER VERSION JUSTIFIED THAT WITH "a Python str carrying one can only come from a
        # surrogateescape decode, WHICH NOTHING IN THIS PORT PERFORMS". That premise is FALSE
        # and it appeared at three sites: CPython surrogateescape-decodes sys.argv and the
        # filesystem encoding ITSELF, with no explicit decode call anywhere. Two of the three
        # sites were real crashes (the approval digest, the lock metadata). This one is kept
        # STRICT deliberately, and on a different argument: every caller now routes its payload
        # through _js_json_text, which escapes surrogates to ASCII, so a string arriving here
        # still carrying one was NOT produced by the serializer -- it is a caller bug, and
        # writing Node's lossy U+FFFD substitution of it would silently corrupt a file. The
        # difference from the other two sites is that there the ORACLE loses nothing (it
        # escapes) while here the oracle DOES lose (it substitutes).
        # THE THIRD INSTANCE of the multi-syscall shape, after read_stable_regular_file and
        # read_approval_file, and the one a review predicted by noting write_atomic has no stated
        # invariant. The oracle wraps neither (gates.mjs:707-712 calls writeFileSync then
        # fsyncSync raw), so each carries its own token out; unwrapped, both reached a caller
        # guessing `open`. The os.close below stays bare deliberately -- mjs:710 does not swallow
        # it either, and that polarity was checked per-site.
        # `write` MEASURED on writeFileSync -- the API the oracle actually calls -- not on the
        # writeSync sibling: EBADF gives "EBADF: bad file descriptor, write" from both, and
        # fsyncSync gives `fsync`. Measuring the neighbouring API and generalizing is the realpath
        # mistake, and node_call's own docstring demands the check before wrapping.
        # THE TOKEN ASSUMES write_all ONLY WRITES. If that helper ever grows an lseek or fstat,
        # this goes silently wrong -- the same single-syscall precondition node_call documents.
        node_call("write", write_all, fd, str(text).encode("utf-8"))
        node_call("fsync", os.fsync, fd)
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
    mkdirs(directory, 0o700)
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
                # `stat`, matching the same probe in gate_check's lock loop: ENOENT is absorbed
                # below, anything else re-raises to a caller that would otherwise guess `open`.
                node_call("stat", os.stat, lock)
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
        # write_all here too, and this site is the least obvious of the three: a short write
        # leaves the lock metadata as truncated JSON, the release loop's json.load raises
        # ValueError, that arm breaks WITHOUT unlinking, and the lock is held until a human
        # removes it. A partial write of an ownership record is worse than no record.
        # _js_json_text, not a bare json.dumps: `lock_target` is a RESOLVED PATH, and on Linux a
        # repository path whose bytes are not valid UTF-8 reaches Python as U+DCxx through the
        # filesystem encoding's surrogateescape -- the same runtime-supplied decode that makes
        # the digest case reachable through argv. MEASURED at this exact payload shape:
        #   PY  json.dumps(..., ensure_ascii=False).encode("utf-8") -> UnicodeEncodeError
        #   JS  JSON.stringify(...)                                 -> 51 bytes of pure ASCII
        # JSON.stringify ESCAPES the surrogate, so the oracle writes a faithful, reversible
        # record and loses nothing.
        #
        # THIS IS NOT THE FAIL-CLOSED CASE sha256() defends, and the distinction decides the
        # fix. There, Node's createHash SUBSTITUTES U+FFFD and hashes a LOSSY key, so two
        # different paths can collide on one lock -- refusing is genuinely safer. Here nothing
        # is lost, and refusing is strictly worse than the oracle: the comment below already
        # records what a failed write of this record costs -- the release loop's json.load
        # raises, that arm breaks WITHOUT unlinking, and the lock is held until a human removes
        # it. The port would turn an unusual path into a permanently stuck lock.
        write_all(fd, _js_json_text(
            {"token": token, "pid": os.getpid(), "target": lock_target,
             "at": int(time.time() * 1000)}).encode("utf-8"))
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
                    # (Both runtimes leave the lock file behind here, by design.)
                    #
                    # CORRECTION to what an earlier commit message of mine asserted: "neither
                    # runtime unlinks a lock it cannot prove it owns" is FALSE, of both. This
                    # read-then-unlink is not atomic, so the proof is stale by the time the
                    # syscall runs: read our token, get descheduled, a human removes the lock
                    # (the documented recovery), a successor acquires and writes ITS token, and
                    # this unlink then deletes the SUCCESSOR's lock. The oracle has the identical
                    # shape, so it is a shared hazard rather than a port defect -- but it is not
                    # the guarantee I claimed, and the next reader would have relied on it.
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
            before = node_call("lstat", os.lstat, path)
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
        fd = node_call("open", os.open, path, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                        | getattr(os, "O_NONBLOCK", 0) | no_follow, 0o600)
        opened = node_call("fstat", os.fstat, fd)
        named = node_call("lstat", os.lstat, path)
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
        node_call("write", write_all, fd,
                   (re.sub(r"[\r\n]+", " ", str(line)) + "\n").encode("utf-8"))
        node_call("fsync", os.fsync, fd)
        return path
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


LEASE_MAX_BYTES = 64 * 1024


def _read_leases_unlocked(root):
    """Every lease record under the lock directory, with malformed ones surfaced, not dropped.

    The `(invalid)` placeholder is load-bearing: it carries `globs: ["**"]`, which overlaps
    EVERYTHING, so an unreadable or tampered lease blocks every claim instead of silently
    permitting one. Failing closed is the whole point -- a lease is a write-permission record.
    """
    directory = _js_join(os.path.abspath(root), LOCK_DIR)
    invalid_dir = [{"scope": "(invalid)", "leaf": "locks", "globs": ["**"],
                    "file": directory, "invalid": True}]
    try:
        os.lstat(directory)
    except OSError as error:
        return [] if error.errno == errno.ENOENT else invalid_dir
    if not _real_directory_inside(root, directory):
        return invalid_dir

    leases = []
    # `scandir`, which is what node reports for a failed readdirSync -- NOT `listdir`, and not the
    # `open` a caller would otherwise guess: this call sits outside any try, so it propagates to
    # gate_check's "cannot claim leases" catch, which hardcodes open. MEASURED before the fix on
    # a chmod-300 locks directory: node `..., scandir '<locks>'` against the port's `..., open`.
    # ONE CONSTANT IS SOUND HERE, unlike realpath, and that was checked rather than assumed --
    # the realpath bug WAS one errno generalized to a whole call. readdirSync reports `scandir`
    # for EACCES, ENOTDIR and ENOENT alike, and CPython names the same path in all three.
    for name in sorted(node_call("scandir", os.listdir, directory), key=js_sort_key):
        if not name.endswith(".lease"):
            continue
        file = _js_join(directory, name)
        try:
            value = json.loads(read_stable_regular_file(
                file, max_bytes=LEASE_MAX_BYTES, label="lease record", root=root))
            # The filename IS the identity: it must be the digest of scope::leaf, so a record
            # cannot be renamed to impersonate another owner's lease.
            if (not js_truthy(value) or not isinstance(value.get("scope"), str)
                    or validate_scope_id(value["scope"])
                    or not isinstance(value.get("leaf"), str)
                    or validate_scope_id(value["leaf"], "leaf")
                    or not isinstance(value.get("globs"), list) or not value["globs"]
                    or name != sha256(value["scope"] + "::" + value["leaf"])[:24] + ".lease"):
                raise ValueError("invalid lease record shape or identity")
            normalized = [normalize_owns_glob(glob) for glob in value["globs"]]
            # Rejecting a glob that merely NORMALIZES to something else, not just an invalid
            # one: a record storing "a/../b" would otherwise be compared in its normalized
            # form while a reader sees the stored text.
            if (any("error" in item for item in normalized)
                    or any(item.get("value") != stored
                           for item, stored in zip(normalized, value["globs"]))):
                raise ValueError("invalid lease record OWNS paths")
            leases.append({**value, "globs": [item["value"] for item in normalized],
                           "file": file})
        except Exception:
            # BROAD ON PURPOSE, and for a stronger reason than "the oracle has a bare catch":
            # this block RAISES ValueError itself as control flow, and a lease file holding a
            # JSON array decodes to a list whose .get raises AttributeError -- which is exactly
            # the oracle's `typeof value.scope !== "string"` rejection. `except OSError` here
            # would crash on both.
            #
            # This is NOT the standard applied in list_scopes, which was deliberately narrowed
            # to OSError in the same file. The rule that reconciles them: catch what the block
            # LEGITIMATELY RAISES for bad input. Here that includes ValueError and
            # AttributeError; there, only OSError qualifies, so a bare catch would have turned
            # a typo in the comprehension into a silent "no pipelines configured".
            leases.append({"scope": "(invalid)", "leaf": name, "globs": ["**"],
                           "file": file, "invalid": True})
    return leases


def read_leases(root):
    return _read_leases_unlocked(root)


def _lease_registry(root):
    return _js_join(os.path.abspath(root), LOCK_DIR, "lease-registry")


def claim_leases(root, spec):
    """Claim write ownership of globs for one scope/leaf, refusing any overlap."""
    def claim():
        scope_error = validate_scope_id(spec.get("scope"))
        leaf_error = validate_scope_id(spec.get("leaf"), "leaf")
        if scope_error or leaf_error:
            return {"ok": False, "conflicts": [], "error": scope_error or leaf_error}
        normalized = []
        for glob in spec.get("globs") or []:
            result = normalize_owns_glob(glob)
            if "error" in result:
                return {"ok": False, "conflicts": [], "error": result["error"]}
            normalized.append(result["value"])
        if not normalized:
            return {"ok": False, "conflicts": [], "error": "no OWNS paths to claim"}

        held_leases = _read_leases_unlocked(root)
        same_owner = next((held for held in held_leases
                           if held["scope"] == spec["scope"] and held["leaf"] == spec["leaf"]),
                          None)
        if same_owner:
            return {"ok": False, "conflicts": [
                {"identity": True, "with": spec["scope"] + "/" + spec["leaf"],
                 "heldGlobs": same_owner["globs"]}]}

        conflicts = []
        for glob in normalized:
            for held in held_leases:
                their_glob = next((other for other in held["globs"]
                                   if globs_overlap(glob, other)), None)
                if their_glob:
                    conflicts.append({"glob": glob,
                                      "with": held["scope"] + "/" + held["leaf"],
                                      "theirGlob": their_glob})
        if conflicts:
            return {"ok": False, "conflicts": conflicts}
        file = _js_join(_lock_directory(root),
                        sha256(spec["scope"] + "::" + spec["leaf"])[:24] + ".lease")
        # _js_json_text, not a bare json.dumps. The bare call shipped WITHOUT ensure_ascii=False
        # and diverged from gates.mjs:926 on the first non-ASCII OWNS glob -- MEASURED, same
        # ledger, `OWNS: src/café/**`:
        #     JS  "globs": ["src/café/**"]          <- the character, raw
        #     PY  "globs": ["src/caf...e9/**"]      <- the same character as a 6-char ASCII
        #                                              backslash-u escape, because ensure_ascii
        #                                              defaults to True. Spelled in words here:
        #                                              writing the escape literally in a comment
        #                                              renders it as the character and the two
        #                                              lines then look identical, which is a
        #                                              worked example of a diff that proves
        #                                              nothing -- it happened twice while this
        #                                              comment was being written.
        # It round-trips within one runtime (json.load decodes the escape back), which is why no
        # behavioural test caught it; the FILE BYTES are the contract, and the lease file is
        # read by whichever runtime holds the other end.
        write_atomic(file, _js_json_text({"scope": spec["scope"], "leaf": spec["leaf"],
                                          "globs": normalized, "pid": os.getpid()},
                                         indent=2) + "\n", root=root)
        return {"ok": True, "file": file, "conflicts": [], "globs": normalized}

    return with_file_lock(root, _lease_registry(root), claim)


def release_leases(root, spec):
    def release():
        count = 0
        for lease in _read_leases_unlocked(root):
            if lease["scope"] != spec.get("scope"):
                continue
            if spec.get("leaf") and lease["leaf"] != spec["leaf"]:
                continue
            try:
                os.unlink(lease["file"])
                count += 1
            except OSError:
                pass          # raced or already absent
        return count

    return with_file_lock(root, _lease_registry(root), release)


def sleep(ms):
    time.sleep(ms / 1000)
