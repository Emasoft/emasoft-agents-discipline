"""JavaScript built-in behaviours the port has to reproduce, not approximate.

These are not translations of any function in `lib/gates.mjs`. They are the three places where
a plain Python idiom silently produces a DIFFERENT ANSWER from the JS the oracle is written in,
and where that answer reaches a file on disk or a message a human reads:

  - `js_object_key_order` -- JS object keys are NOT insertion-ordered. Integer-like keys come
    first, numerically, whatever order they were added in. `JSON.stringify` follows that order,
    so `dispatch.json` written by the oracle and by a port using a plain dict differ in BYTE
    ORDER for any wave named "1" -- and wave ids are `[A-Za-z0-9]...`, so digits are legal.
  - `locale_compare_key` -- `String.prototype.localeCompare` is ICU collation, not code-point
    order. Measured: ["a","A","b","B"] sorts a,A,b,B under localeCompare and A,B,a,b under
    Python's `<`. dispatch-check sorts its wave report with it.
  - `parse_date` -- `Date.parse` treats a bare date as UTC and a bare date-TIME as LOCAL, accepts
    hour 24 exactly at 24:00:00, and rolls an out-of-range DAY into the next month while
    rejecting an out-of-range month. `datetime.fromisoformat` agrees with none of that.

Each is verified against Node by tests/jsapi-drive.mjs / tests/jsapi_drive.py, over a corpus
that includes every case measured above.
"""

import datetime
import decimal
import re

# ---------------------------------------------------------------------------------------------
# String.prototype.trim
# ---------------------------------------------------------------------------------------------

# `.trim()` strips the ECMAScript WhiteSpace + LineTerminator set; `str.strip()` strips whatever
# `str.isspace()` accepts. The two sets are NEITHER equal nor nested -- measured by asking both
# runtimes about every code point from 0 to 0x10FFFF:
#
#     JS strips, Python does not:  U+FEFF
#     Python strips, JS does not:  U+001C U+001D U+001E U+001F U+0085
#
# Both directions are live defects, and one of them shipped: an abandon reason of a single
# U+FEFF was REFUSED by the oracle (trimmed to "", then rejected as blank) and ACCEPTED by the
# port, which wrote the BOM into the shared dispatch.json as the reason. Exit 2 versus exit 0 on
# the same command, on the file both runtimes read during the migration.
#
# Built with chr() from the code points, NOT written as characters and NOT as backslash-u
# escapes. Both of those spellings put invisible bytes in this file: the literal version embedded
# a raw CR and a raw newline and left the module unparseable, and so did the escaped version,
# because the escapes are materialised before they reach the file. chr() is the only spelling
# whose source stays pure ASCII, and it is also the only one that shows a reader the code points
# by name. dispatch.py's control-character class learned this the same way.
#
# Python's `\s` is NOT a shortcut here: it carries the five separators above, so a `[\s...FEFF]`
# class would fix the FEFF direction and leave the other one. (_safe_diagnostic in dispatch.py
# can use that spelling only because its CONTROL pass has already replaced all five.)
_JS_WHITESPACE = "".join(chr(c) for c in (
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680,
    *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
))


def js_trim(value):
    """`String(value).trim()` -- the ECMAScript set, not `str.strip()`'s."""
    return str(value).strip(_JS_WHITESPACE)


# ---------------------------------------------------------------------------------------------
# Truthiness and String() -- the two coercions `x || ""` and `String(x)` perform
# ---------------------------------------------------------------------------------------------

# JS falsy is EXACTLY: false, 0, -0, 0n, "", null, undefined, NaN. Python's `bool()` disagrees in
# two directions, and both were MEASURED as live divergences in classify_gate_evidence:
#   NaN  -- falsy in JS, TRUTHY in Python. `NaN || ""` is "" (pending); `not nan` is False, so a
#           plain port took str(nan) == "nan" and returned "human".
#   [] {} -- TRUTHY in JS, falsy in Python. `{} || ""` is the object, String({}) is
#           "[object Object]" (human); a plain port took "" and returned "pending".
# Neither is reachable from parse_gates, which only ever sets a str or None -- both are reachable
# from a hand-built gate, which is exactly how hardening-tests.mjs calls these functions.


def js_truthy(value):
    """`!!value` -- JS truthiness, which is not `bool(value)`."""
    if isinstance(value, float) and value != value:      # NaN: falsy in JS, truthy in Python
        return False
    # list/dict/tuple only -- NOT set. js_string cannot render a set (str(set()) is "set()"
    # where String(new Set()) is "[object Set]"), so claiming it truthy here would feed
    # js_string a value it renders wrongly. The two must model the same type domain.
    if isinstance(value, (list, dict, tuple)):           # any object is truthy in JS, even empty
        return True
    return bool(value)


def _js_number(value):
    """`String(<number>)` -- Number::toString, which `str()` matches only in the middle.

    MEASURED against Node, and every one of these was wrong before:
      float("inf")  -- `int(value)` raised OverflowError. A CRASH where JS prints "Infinity".
      1e-7          -- JS "1e-7", Python "1e-07": Python zero-pads the exponent to two digits.
      0.000001      -- JS "0.000001", Python "1e-06": the two switch to exponential notation at
                       DIFFERENT thresholds. JS uses plain decimal over [1e-6, 1e21) and Python's
                       repr leaves it at 1e-4, so the whole band between them is mis-rendered.
      -0.0          -- JS "0" (String, unlike Object.is, does not preserve the sign).
    """
    if value != value:
        return "NaN"
    if value == float("inf"):
        return "Infinity"
    if value == float("-inf"):
        return "-Infinity"
    if value == 0:
        return "0"                                  # covers -0.0, which JS prints unsigned
    magnitude = abs(value)
    if magnitude < 1e21 and float(value).is_integer():
        # Decimal(repr(...)), NOT int(value). Above 2**53 a float names a value whose EXACT
        # binary expansion is not what JS prints: the engine prints the SHORTEST decimal that
        # round-trips, zero-padded. Measured -- String(1.2345678901234567e20) is
        # "123456789012345670000" and str(int(...)) gives "123456789012345667584", the true
        # binary value. repr() is Python's shortest-round-trip form, so converting THAT to an
        # integer reproduces the engine's answer. Exact powers of ten agree either way, which is
        # why my first 26-value corpus missed this entirely.
        return str(int(decimal.Decimal(repr(float(value)))))
    if 1e-6 <= magnitude < 1e21:
        # Plain decimal, at the SHORTEST precision that still round-trips -- which is what both
        # engines print, but Python's repr may hand back an exponent form inside this band.
        # range to 25, NOT 18. `places` counts digits AFTER the point, and this band reaches down
        # to 1e-6, so a value needs up to 5 leading zeros PLUS 17 significant digits -- 22, not
        # the 24 first recorded here. n >= 16 cannot reach this branch at all (the spacing
        # between doubles exceeds 1 there, so every such value is integral and the branch
        # above catches it), so 22 is the true maximum and 25 tries leave real margin. Capping at
        # 17 made the loop exhaust and fall through to repr(), which returns the EXPONENT form --
        # so every such value rendered as "1.2430862257523161e-06" where JS prints
        # "0.0000012430862257523161". Found by a randomized differential over 4314 doubles (21
        # failures); my hand-picked 32-value corpus had no value in that band with a full
        # mantissa, so it passed. 24 is the true maximum here and 25 leaves a margin.
        for places in range(1, 26):
            candidate = f"{value:.{places}f}".rstrip("0").rstrip(".")
            if float(candidate) == value:
                return candidate
        return repr(value)
    # Exponential. Python writes e-07/e+21, JS writes e-7/e+21: strip the zero padding, keep the
    # sign, and drop a mantissa that is a bare integer down to its digits (1e+21, never 1.0e+21).
    # repr() ALWAYS carries an exponent here -- this branch is reached only for magnitude
    # < 1e-6 or >= 1e21, and repr switches to exponential below 1e-4 and at/above 1e16. A
    # `if not exponent:` fallback stood here and could never fire; a guarded path that
    # cannot execute reads as coverage it does not provide.
    mantissa, _, exponent = repr(value).partition("e")
    mantissa = mantissa.rstrip("0").rstrip(".") if "." in mantissa else mantissa
    sign = "-" if exponent.startswith("-") else "+"
    return mantissa + "e" + sign + str(int(exponent.lstrip("+-")))


def js_string(value):
    """`String(value)` -- not `str(value)`.

    Covers the types a gate field can carry. NOT a general ToString: a class instance, a
    function or a Symbol has no counterpart here, and inventing one would be a guess rather
    than a port. Anything unlisted falls through to str(), which is what the previous code did
    for everything.
    """
    # None maps to "null". Python has no `undefined`, so a caller that must distinguish a MISSING
    # key from an explicit null does it at the call site (`"k" in d`), not here -- a sentinel
    # parameter would be machinery for a distinction this module cannot see.
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    # int too, NOT just float: a Python int is a JS NUMBER, and Number stringifies in exponential
    # form from 1e21 up. Measured: String(10**21) is "1e+21" and str() gives the 22 digits. They
    # agree for every int below that, which is why falling through to str() looked correct.
    # (A JS BigInt would print the digits -- but nothing in this port produces one, and a value
    # arriving from JSON.parse is a Number.)
    if isinstance(value, (int, float)) and value is not True and value is not False:
        if isinstance(value, int) and abs(value) >= 10 ** 21:
            try:
                return _js_number(float(value))
            except OverflowError:
                # A Python int has unbounded range; a JS Number does not. 10**309 exceeds the
                # double maximum, where JS prints "Infinity" -- float() RAISES instead, so the
                # conversion itself has to produce the same answer the engine would.
                return "-Infinity" if value < 0 else "Infinity"
        return _js_number(value)
    if isinstance(value, dict):
        return "[object Object]"
    if isinstance(value, (list, tuple)):
        # Array.prototype.toString: comma-joined, with null/undefined rendering as EMPTY.
        return ",".join("" if v is None else js_string(v) for v in value)
    return str(value)



# ---------------------------------------------------------------------------------------------
# Object key order
# ---------------------------------------------------------------------------------------------

# A key is an "array index" -- and therefore sorts first, numerically -- only in its CANONICAL
# decimal spelling: no leading zero, no sign, no whitespace, below 2**32-1. "01" and "-1" are
# ordinary string keys, which is why the measured order of {b,10,a,2,01,-1} is [2,10,b,a,01,-1]
# and not [-1,01,2,10,a,b].
_ARRAY_INDEX_RE = re.compile(r"^(?:0|[1-9][0-9]*)\Z")


def is_array_index(key):
    # str(key), because a dict built programmatically can carry an INT key where one read back
    # from json.loads never does. `_ARRAY_INDEX_RE.match(1)` raises TypeError, so js_json_object
    # would crash on `state["waves"][1] = {...}` rather than merely misorder it -- the same
    # latent-until-dispatch.py class as the list-nesting bug. JS coerces the key to a string
    # anyway, so stringifying here is what the oracle does, not a workaround.
    key = str(key)
    return bool(_ARRAY_INDEX_RE.match(key)) and int(key) < 2 ** 32 - 1


def js_object_key_order(keys):
    """Order `keys` as JS enumerates an object's own string properties."""
    indices = sorted((k for k in keys if is_array_index(k)), key=int)
    # The rest keep INSERTION order, so this relies on `keys` arriving in insertion order --
    # which a Python dict guarantees and is the whole reason a dict is the right carrier here.
    return indices + [k for k in keys if not is_array_index(k)]


def js_entries(mapping):
    return [(k, mapping[k]) for k in js_object_key_order(list(mapping))]


def js_json_object(value):
    """Re-key every nested object into JS enumeration order, for json.dumps.

    Descends into LISTS as well as dicts. The first version did not, so a dict inside a list kept
    Python insertion order while its siblings were re-keyed -- silent byte-order drift in the
    serialized state, in the one function whose entire job is preventing exactly that. Nothing in
    today's dispatch state nests an object in an array (`leaves` is strings), which is precisely
    why it would have gone unnoticed until a schema change put one there.
    """
    if isinstance(value, dict):
        return {k: js_json_object(v) for k, v in js_entries(value)}
    if isinstance(value, list):
        return [js_json_object(item) for item in value]
    return value


# ---------------------------------------------------------------------------------------------
# String length and slicing
# ---------------------------------------------------------------------------------------------

# A JS string is a sequence of UTF-16 CODE UNITS; a Python str is a sequence of CODE POINTS.
# `.length` and `.slice()` therefore count different things the moment a character is outside
# the BMP. Measured: "😀" * 200 is 400 to JS and 200 to Python, so dispatch.mjs's
# `handle.length > 256` REJECTS a handle a naive port ACCEPTS -- and the handle is the wave's
# uniqueness key, so the two runtimes would disagree about whether a state file is valid. The
# same trap sits in validReason (500), safeDiagnostic's .slice(0, 500) and tail's .slice(0, max).
#
# This matters beyond tests: during the migration a JS gate-check and a Python dispatch-check
# read and write the SAME dispatch.json, so a length disagreement is a live interop bug.


def js_length(value):
    """`value.length` -- the UTF-16 code-unit count, not the code-point count."""
    return len(str(value).encode("utf-16-le", "surrogatepass")) // 2


def js_slice(value, start, end=None):
    """`value.slice(start, end)` over UTF-16 code units, including JS's clamping rules.

    CAN RETURN A LONE SURROGATE, and that is faithful rather than sloppy: measured,
    `"😀😀".slice(0, 3)` is "😀\ud83d" in V8. Note the interaction with this port's sha256,
    which REFUSES a lone surrogate -- a diagnostic truncated mid-pair and then hashed would
    raise here where the oracle hashes a replacement character. Both behaviours are deliberate;
    they meet in safeDiagnostic, so a caller doing both needs to know.
    """
    text = str(value)
    units = text.encode("utf-16-le", "surrogatepass")
    count = len(units) // 2
    start = count + start if start < 0 else start
    start = max(0, min(count, start))
    if end is None:
        end = count
    else:
        end = count + end if end < 0 else end
        end = max(0, min(count, end))
    if end <= start:
        return ""
    return units[start * 2:end * 2].decode("utf-16-le", "surrogatepass")


# ---------------------------------------------------------------------------------------------
# localeCompare
# ---------------------------------------------------------------------------------------------

# ICU root collation over the id charset `[A-Za-z0-9._-]`, MEASURED with Node rather than
# reasoned about: the primary order of the punctuation is `_` `-` `.`, which is not the code
# point order, not alphabetical, and not what any of the three obvious guesses would produce.
_PRIMARY = {c: i for i, c in enumerate("_-.0123456789abcdefghijklmnopqrstuvwxyz")}


def locale_compare_key(value):
    """Sort key reproducing `a.localeCompare(b)` over the id charset.

    Case is a TERTIARY weight: the whole string compares case-folded first, and only a full tie
    is broken by case, lowercase before uppercase, left to right. That is why "aB" sorts before
    "Ab" -- both fold to "ab", then position 0 decides. A per-character (fold, case) key would
    order them the other way, which is the mistake this two-tuple shape exists to avoid.

    SCOPE OF THE GUARANTEE: the id charset `[A-Za-z0-9._-]`, and nothing else. Every caller
    reaches this only through `validate_scope_id`, which rejects anything outside it. Characters
    that are not in the table fall back to their code point, offset past it, so an unexpected one
    sorts last and deterministically instead of raising -- but that fallback is NOT ICU. Measured:
    the engine sorts ["~","e","E","é","É","ß","z"] and this key gives
    ["e","E","z","~","ß","é","É"], because ICU folds é onto e at primary level and a code point
    cannot. There is deliberately no corpus row for that: a row would assert a fidelity this
    function does not claim, and making it pass would mean shipping a partial ICU table for
    inputs no caller can produce.
    """
    # ord(c.lower()), not ord(c): the fallback has to fold case like the lookup does, or an
    # out-of-charset pair separates at PRIMARY level where ICU separates it at tertiary.
    # Unreachable through validate_scope_id, so this is consistency rather than a live defect.
    primary = tuple(_PRIMARY.get(c.lower(), len(_PRIMARY) + ord(c.lower())) for c in value)
    tertiary = tuple(1 if c.isupper() else 0 for c in value)
    return (primary, tertiary)


# ---------------------------------------------------------------------------------------------
# Date.parse
# ---------------------------------------------------------------------------------------------

# The ECMA-262 Date Time String Format, which is the part of Date.parse that is SPECIFIED.
# Anything else Date.parse accepts ("Jan 1 2020", "2020-01-01 00:00:00" with a space) is
# implementation-defined fallback parsing; see parse_date's docstring for why this port rejects
# those rather than guessing at V8's heuristics.
_DATE_RE = re.compile(
    r"^([+-][0-9]{6}|[0-9]{4})"                       # year, or expanded ±YYYYYY
    r"(?:-([0-9]{2})(?:-([0-9]{2}))?)?"               # -MM-DD
    r"(?:[Tt]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]+))?)?"   # THH:MM:SS.sss
    r"([Zz]|[+-][0-9]{2}:?[0-9]{2})?)?\Z")            # offset


def parse_date(value):
    """`Date.parse(value)` in milliseconds since the epoch, or None for the spec's NaN.

    DECLARED DIVERGENCE, and the only one: Date.parse also accepts formats the standard leaves
    implementation-defined -- "Jan 1 2020" and "2020-01-01 00:00:00" both parse in V8, as local
    time. Reproducing V8's fallback heuristics is neither specified nor stable across engines,
    so this returns None for them. The practical effect is that the port is STRICTER than the
    oracle on hand-edited state: a timestamp the oracle would have silently accepted is reported
    as "must be an ISO timestamp". Every value this codebase WRITES comes from toISOString().
    """
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(value)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    # "-000000" is the one expanded year the format forbids: there is no negative zero year, so
    # the sign would be meaningless. Measured: V8 returns NaN for "-000000-01-01T00:00:00Z" and a
    # real number for "+000000-...". The regex accepts both, and int() erases the sign, so
    # without this the port answered where the oracle refused.
    if year.startswith("-") and int(year) == 0:
        return None

    # A bare date is UTC and a bare date-TIME is LOCAL. That asymmetry is in the specification
    # (it is the one place the format is not self-describing) and it is a 1-hour error on this
    # machine if a port picks either one uniformly.
    has_time = hour is not None
    month_n, day_n = int(month or 1), int(day or 1)
    if not 1 <= month_n <= 12:
        return None
    # 01-31 is a SYNTAX check on the field, applied before any calendar is consulted. That is
    # what makes 2020-01-32 and 2020-01-00 NaN while 2020-02-30 parses and rolls to March 1 --
    # measured, and not a distinction any date library draws for you. A port that let the
    # calendar decide accepted both of the first two; one that rejected on the calendar would
    # have rejected the third.
    if not 1 <= day_n <= 31:
        return None
    hour_n, minute_n = int(hour or 0), int(minute or 0)
    second_n = int(second or 0)
    # `.1234567` truncates to milliseconds rather than rounding; measured, ".1234567" -> 123.
    ms = int((fraction or "")[:3].ljust(3, "0")) if fraction else 0
    if minute_n > 59 or second_n > 59:
        return None
    # Hour 24 is legal at EXACTLY 24:00:00.000 and rolls into the next day; 24:00:01 is NaN.
    # This exact pair already produced a divergence in the ledger port, in both directions:
    # first by rejecting 24:00:00, then by accepting 24:00:01.
    if hour_n == 24 and (minute_n or second_n or ms):
        return None
    if hour_n > 24:
        return None

    # Integer epoch arithmetic rather than a datetime, because the format's expanded years
    # (±YYYYYY, so 2020 BC and 275760 AD) fall outside datetime's 1..9999 range: the first port
    # returned None for "-002020-01-01T00:00:00Z" where the oracle returns a real number, which
    # is the "port is stricter than the oracle" defect this whole module exists to avoid.
    # Day-of-month rolling comes out of the algorithm for free.
    epoch_ms = ((_days_from_civil(int(year), month_n, day_n) * 86400 + hour_n * 3600
                 + minute_n * 60 + second_n) * 1000 + ms)

    if offset and offset not in ("Z", "z"):
        body = offset[1:].replace(":", "")
        offset_hours, offset_minutes = int(body[:2]), int(body[2:])
        if offset_hours > 23 or offset_minutes > 59:
            return None
        sign = -1 if offset[0] == "-" else 1
        return epoch_ms - sign * (offset_hours * 3600 + offset_minutes * 60) * 1000
    if offset or not has_time:
        return epoch_ms                                # Z, or a bare date: already UTC
    # A bare date-TIME is LOCAL, and the local offset is only knowable through the platform's
    # zone database, which is datetime's job. Years outside its range have no local answer here;
    # that residue is unreachable for this codebase, whose timestamps are all toISOString().
    try:
        naive = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=epoch_ms)
    except (OverflowError, ValueError):
        return None
    # round(), not int(): timestamp() is a float and int() truncates, so a value that should
    # land on .000 can come back one millisecond low.
    return round(naive.astimezone().timestamp() * 1000)


def _days_from_civil(year, month, day):
    """Days from 1970-01-01 in the proleptic Gregorian calendar (Hinnant's algorithm).

    The published form writes the era as `(y >= 0 ? y : y - 399) / 400`, because C++ integer
    division TRUNCATES toward zero and the `- 399` is what turns that into a floor. Python's
    `//` already floors, so carrying the correction over applies it TWICE: for year -2021 it
    gives era -7 where the answer is -6, putting year_of_era outside 0..399 and the result one
    day off. Measured against the oracle on "-002020-01-01T00:00:00Z", which is why the corpus
    carries a negative expanded year at all -- the bug is invisible for every year AD.
    """
    year -= month <= 2
    era = year // 400
    year_of_era = year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def js_sort_key(value):
    """Sort key reproducing `Array.prototype.sort()`'s DEFAULT order: UTF-16 code units.

    JS compares strings by code UNIT and Python by code POINT, and the two disagree for every
    astral character: U+1F600 is the surrogate pair D83D DE00, so it sorts BEFORE U+FFFD in JS
    and AFTER it in Python. MEASURED against the oracle on a gates/ directory holding both --
    plain `sorted()` reversed those two filenames, which reorders the gate list a checker
    walks and, on a first-error-wins path, changes which failure a user is shown.

    Big-endian is load-bearing, not stylistic: comparing UTF-16-BE BYTES is equivalent to
    comparing the 16-bit units themselves, because the high byte of each unit is compared
    first. The little-endian encoding of the same string does NOT have that property.
    `js_string`, not `str()`: JS's sort begins with ToString on each element, so `None` sorts as
    "null" where `str()` would key it on "None" and order `[None, "a"]` the other way. No caller
    reaches this with a non-string today -- only `entry.name` and joined paths -- but a docstring
    claiming equivalence to Array.prototype.sort() has to be true for the conversion step too,
    and the model of it already exists one call away.

    `surrogatepass` because a filename read from the filesystem CAN carry a lone surrogate: on a
    non-UTF-8 name Python's surrogateescape produces U+DC80-U+DCFF, which strict UTF-16 refuses
    to encode, and a crash while merely SORTING would be a worse divergence than the one this
    fixes. Note this does NOT make the two runtimes agree on such a name -- node decodes the same
    bytes to U+FFFD, so the two sides hold different strings before any sort key is consulted.
    That divergence is upstream of this function and cannot be repaired here.
    """
    return js_string(value).encode("utf-16-be", "surrogatepass")
