#!/usr/bin/env node
/**
 * Drive the JS built-ins that scripts/lib/jsapi.py has to reproduce, over a corpus that
 * includes every case measured while writing it. Pair of tests/jsapi_drive.py.
 *
 * These three are not gates.mjs functions; they are the LANGUAGE behaviours a Python idiom gets
 * wrong. That makes the oracle the JS engine itself, so this driver calls the built-ins raw.
 */
const out = [];

// 1. Object key enumeration order. Insertion order is varied deliberately so a port that just
//    preserved it would differ.
const objects = [
  ["mixed", ["b", "10", "a", "2", "01", "-1"]],
  ["all-numeric", ["3", "1", "20", "0"]],
  ["all-string", ["w2", "w1", "alpha"]],
  ["boundary", ["4294967294", "4294967295", "4294967296", "0"]],
  ["idlike", ["1", "1.0", "1e1", "+1", " 1"]],
];
for (const [name, keys] of objects) {
  const o = {};
  for (const k of keys) o[k] = 1;
  out.push(["keys " + name, Object.keys(o)]);
  out.push(["stringify " + name, JSON.stringify(o)]);
}

// 2. localeCompare over the id charset. Permutations, not a hand-picked list: a case rule that
//    is right for the pairs I thought of is the failure this is meant to catch.
const alphabet = [..."-._0123456789abzABZ"];
const ids = [];
for (const a of alphabet) { ids.push(a); for (const b of alphabet) ids.push(a + b); }
out.push(["localeCompare", ids.slice().sort((a, b) => a.localeCompare(b))]);
out.push(["localeCompare pairs", ["a", "A", "aB", "Ab", "aa", "aA", "Aa", "AA", "a1", "A1", "1a"]
  .sort((a, b) => a.localeCompare(b))]);
// THREE characters, not two. A two-character pair cannot distinguish a correct positional
// tertiary weight from several wrong schemes that happen to agree on pairs; three can.
// UTF-16 code units, which is what .length counts and .slice() cuts. dispatch.mjs bounds a
// handle at 256 and a reason at 500 with .length, and safeDiagnostic cuts at 500 with .slice --
// so a port counting CODE POINTS accepts state the oracle rejects.
const STRINGS = ["", "a", "aé", "😀", "😀😀", "a😀b", "\u{1F600}\u{1F601}\u{1F602}", "ⓐ", "😀".repeat(200)];
out.push(["js_length", STRINGS.map((s) => s.length)]);
for (const [a, b] of [[0, 1], [0, 2], [0, 3], [1, 3], [0, 500], [-3, undefined], [3, 1], [0, 0], [-99, 99]]) {
  out.push([`js_slice ${a},${b}`, STRINGS.map((s) => Array.from(s.slice(a, b), (c) => c.codePointAt(0)))]);
}
// Punctuation in NON-initial position, where the tertiary tie-break also engages -- realistic
// for wave ids like `w1.retry-2`, and not covered by a corpus built from two-character pairs.
out.push(["localeCompare punctuation", ["a-B", "aB", "a.b", "ab", "a-b", "a_B", "w1.retry-2", "w1.retry-1"]
  .sort((a, b) => a.localeCompare(b))]);
out.push(["localeCompare triples", ["aAb", "AaB", "aab", "AAB", "aAB", "Aab", "abA", "aBa"]
  .sort((a, b) => a.localeCompare(b))]);
// A dict nested inside an ARRAY. js_json_object passed lists through untouched, so its nested
// objects kept Python insertion order; nothing in dispatch state nests one today, which is why
// only a deliberate row can see it.
out.push(["stringify nested in array",
  JSON.stringify({ waves: [{ b: 1, 10: 2, a: 3 }], meta: { z: 1, 2: 2 } })]);

// 3. Date.parse. The spec-defined format only; the implementation-defined fallbacks are listed
//    at the end as a DECLARED divergence, with the oracle's answer recorded so the difference is
//    visible in the file rather than absent from it.
const spec = [
  "2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00Z", "2020-01-01T00:00:00", "2020-01-01",
  "2020-01-01T24:00:00Z", "2020-01-01T24:00:00.000Z", "2020-01-01T24:00:01Z",
  "2020-01-01T24:01:00Z", "2020-01-01T25:00:00Z", "2020-01-01T00:60:00Z", "2020-01-01T00:00:60Z",
  "2020-01-01T00:00:00+02:00", "2020-01-01T00:00:00+0200", "2020-01-01T00:00:00-05:30",
  "2020-01-01T00:00:00+24:00", "2020-01-01T00:00:00+02:60",
  "2020-13-01T00:00:00Z", "2020-00-01T00:00:00Z", "2020-02-30T00:00:00Z", "2020-02-29T00:00:00Z",
  "2021-02-29T00:00:00Z", "2020-01-32T00:00:00Z", "2020-01-00T00:00:00Z",
  "Jan 1 2020", "2020", "2020-01", "2020-01-01T00:00:00.1234567Z", "2020-01-01T00:00:00.1Z",
  "+002020-01-01T00:00:00Z", "-002020-01-01T00:00:00Z", "-000000-01-01T00:00:00Z", "+000000-01-01T00:00:00Z", "2020-01-01t00:00:00z",
  "2020-01-01T00:00:00.000z", "2020-01-01 00:00:00", "2020-01-01T00:00", "2020-01-01T00",
  "", " ", "not a date", "2020-01-01T00:00:00.000Z ", "1970-01-01T00:00:00Z",
  "1969-12-31T23:59:59Z", "275760-09-13T00:00:00Z", "2020-1-1T00:00:00Z",
];
// DECLARED DIVERGENCE, recorded rather than removed from the corpus. Date.parse also accepts
// formats ECMA-262 leaves implementation-defined; V8 parses both of these as LOCAL time. The
// port returns null for them (jsapi.parse_date's docstring says why). Deleting them from the
// corpus would make the suite agree by not looking, which is the harness defect this port has
// already produced three times -- so they are listed, and both answers are recorded.
const DECLARED_NON_ISO = ["Jan 1 2020", "2020-01-01 00:00:00"];
for (const s of spec) {
  const parsed = Date.parse(s);
  const value = Number.isNaN(parsed) ? null : parsed;
  // Each driver reports its OWN answer under the marked key; python-lib-checks excludes these
  // keys from the equality check and asserts the exception's shape (oracle a number, port null).
  const key = DECLARED_NON_ISO.includes(s) ? "Date.parse (declared non-ISO) " : "Date.parse ";
  out.push([key + JSON.stringify(s), value]);
}
// trim(): every code point the two runtimes could disagree about, plus the ones they agree on,
// asked as "does trimming (c + 'a' + c) leave 'a'". Built from code points rather than written
// as characters -- the fixture attempt proved a markdown file cannot carry these reliably (a
// trailing U+0085 was silently stripped, an escaped one became U+2026), and a corpus that
// quietly loses its hostile input asserts nothing.
const TRIM_PROBE = [
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20, 0x85, 0xa0, 0x1680, 0x2000, 0x200a, 0x2028, 0x2029,
  0x202f, 0x205f, 0x3000, 0xfeff, 0x1c, 0x1d, 0x1e, 0x1f, 0x180e, 0x200b, 0x61,
];
for (const cp of TRIM_PROBE) {
  const c = String.fromCodePoint(cp);
  out.push(["trim U+" + cp.toString(16).padStart(4, "0"), (c + "a" + c).trim() === "a"]);
}
// String(<number>) -- Number::toString, which str() matches only in the middle. Each of these
// was measured wrong at some point: inf CRASHED (int(inf) raises), 1e-5/1e-6 use plain decimal
// in JS and exponent in Python, 1e-7 differs only by a ZERO-PADDED exponent, and an integral
// float above 2**53 prints the shortest round-tripping decimal in JS and the exact binary
// expansion via int(). Exact powers of ten agree either way, which is what hid the last one.
const NUMS = [1.0, 0.5, 1e21, 1.5e21, 1e22, 1e-7, 1e-6, 1e-5, 1 / 3, -0.0, Infinity, -Infinity,
  NaN, 1e25, 1e-21, 123.456, 5e-324, 1.7976931348623157e308, 5, -5, 0, 10 ** 21, 10 ** 25,
  2 ** 53, 2 ** 53 + 2, 1.2345678901234567e20, 9.999999999999999e20, 2 ** 60, 1e20, 1e16, 1e17,
  // The [1e-6, 1e-4) band with a FULL mantissa: JS prints these in plain decimal, which
  // needs up to 24 places after the point (6 leading zeros + 17 significant digits). The
  // port's loop capped at 17, exhausted, and fell back to repr()'s exponent form. Found by
  // a randomized differential over 4314 doubles, not by any hand-picked value.
  1.2430862257523161e-06, 3.4040019134427085e-06, -4.7746763818860036e-05, -1.8366746337768764e-05, 2.7810433130305134e-05, -3.85325871109577e-06, 9.999999999999999e-07, 1.0000000000000002e-06];
for (const n of NUMS) out.push(["String(number) " + JSON.stringify(String(n)), String(n)]);
// Number(string) -- the OTHER direction, needed by timeoutValue/jobCount which coerce an argv
// string before range-checking it. Every case here either differs from Python's float() or
// would raise in it, which is the whole reason js_to_number exists rather than a float() call.
const NUM_STRINGS = ["", "   ", "0", "12", "-12", "+5", ".5", "5.", "1e3", "1E3", "1e-3",
  "0x1f", "0X1F", "-0x10", "0x", "0b101", "0o17", "1_000", "nan", "NaN", "inf", "Infinity",
  "-Infinity", "+Infinity", "infinity", "12abc", "abc", "  12  ", "1,000", "0.1", "1e400",
  "-0", "--5", "1e", "+-1", "\u{FEFF}12", "\u{2028}12", "\u{00A0}12",
  // GRAMMAR EDGE CASES. The port matches JS with an anchored regex plus radix prefixes, and
  // these are the shapes where a regex written from memory usually gets it wrong: "1.e3" is
  // LEGAL (trailing dot then exponent), a sign before a radix prefix is NOT ("+0x10" is NaN
  // though " 0x10 " is 16), "0X"/"0b"/"0o" with no digits are NaN, and a lone sign or dot is
  // NaN. All verified identical before being added, not added hoping they would be.
  //
  // "VERIFIED IDENTICAL" IS NOT A GROUND FOR KEEPING A ROW -- the two grounds are
  // mutation-isolated, or named by the helper's docstring.
  //
  // Two rows are MUTATION-ISOLATED. Both mutants were confirmed to IMPORT first (the crash
  // guard: a regex edit that fails to compile raises at import, every row reddens, and that
  // reads as a large isolated set), and in both runs the non-listed rows stayed green:
  //   `\d+(?:\.\d*)?` -> `\d+(?:\.\d+)?`  reddens "5." (pre-existing), "0." and "1.e3"
  //   `[eE][+-]?\d+`  -> `[eE]\d+`        reddens "1e-3" (pre-existing) and "1e+3"
  //
  // The REST are ground TWO: `js_to_number`'s docstring now ENUMERATES the grammar it accepts,
  // and every shape listed there has its row here. An earlier version kept them as
  // "documentation of the grammar", which was a THIRD ground introduced by naming it something
  // else -- and a corpus that admits unbounded documentation rows has no stopping condition at
  // all. The enumeration is what closes it: adding a shape means adding it in both places.
  "0.", "1.e3", " +0x10", "0X", "1e+3", "1e999", "+", "-", ".", "1.2.3", "0.0e0", "+.5",
  "-.5", "00", "010", ".e3", "0b", "0o", "0xg", " 0x10 ", "+0x10", "1 2",
  // NON-ASCII DIGITS AND THE PEP-515 UNDERSCORE -- ground ONE, and they close a REAL defect the
  // 202-row corpus was blind to: `Number("١٢")` is NaN, but the port returned 12,
  // because Python's `\d` matches every Unicode decimal digit while JS's is ASCII-only in every
  // mode. `int(digits, radix)` has the same hole PLUS the underscore, so `0x1_0` was 16 to the
  // port and NaN to the oracle. Case count is not coverage: every one of those 202 rows had
  // ASCII digits, so the corpus was blind in a direction nobody had varied.
  //
  // Written with String.fromCharCode, never as literal characters. Both drivers must hold the
  // same bytes, and a literal Arabic-Indic digit in a source file is invisible to a reader and
  // one careless re-encode away from silently becoming a different code point.
  //
  // GROUNDS, MEASURED -- and the first version of this comment got them wrong twice.
  //
  //   M1  `[0-9]` -> `\d` in _JS_DECIMAL_RE   reddens the THREE unicode-digit rows, together
  //   M2' revert the radix whitelist to the   reddens "0x1_0" and "0x<U+0661>", together
  //       pre-fix `try/except ValueError`
  //
  // Both mutants IMPORT and run to completion, so both verdicts are catches, not crashes.
  //
  // ERROR 1, and it is the interesting one: the mutation this comment ORIGINALLY named for the
  // radix pair -- "drop the _RADIX_DIGITS guard" -- is NOT a valid control. It CRASHES the
  // driver with an uncaught ValueError on "0xg", because the whitelist is now the only thing
  // catching that (the try/except was removed as dead when the whitelist landed). A crash is
  // not a catch; every row reddens and the result reads as a huge isolated set. M2' -- reverting
  // to the code as it actually was before the fix -- is the control that means something.
  //
  // ERROR 2, AND IT NEEDED A METHOD CHANGE RATHER THAN A LABEL. Neither mutation isolates a
  // ROW: M1 reddens three at once, M2' reddens two at once. Calling that "class isolation" and
  // filing it under ground one was a THIRD GROUND INTRODUCED BY NAMING IT -- the identical move
  // retracted two commits earlier, where the new name was "documentation". Ground two is not
  // available either: this file forbids non-ASCII LITERALS, and a docstring is prose that cannot
  // call chr(), so a non-ASCII shape can never be enumerated there. These rows were structurally
  // groundless.
  //
  // So the method gains a THIRD GROUND, declared rather than smuggled -- EQUIVALENCE-CLASS
  // COVERAGE, with its own bound:
  //   a mutation that reddens N rows together grounds ONE of them. A sibling is kept only when
  //   a MECHANISM is named that would separate it -- and "named" means an implementation you
  //   can write down, not a story about one.
  //
  // Applied honestly here, that keeps ONE row per class on solid ground and leaves three
  // siblings. They are kept, and the reason is cost, not mechanism: U+FF11 and U+06F4 are
  // different Unicode blocks, but every implementation anyone would actually write --
  // `[0-9]`, `\d` + re.ASCII, str.isascii, str.isdecimal, category(c) == "Nd" -- treats all
  // three identically, so no separating mechanism could be named. An earlier version of this
  // comment asserted such a narrowing as if it were the reason; nobody had run it, and it is
  // very likely impossible. Three free rows against a hazard class is a fine trade. Pretending
  // they were isolated was not.
  String.fromCharCode(0x0661, 0x0662),  // Arabic-Indic -- the pair that shipped as 12
  String.fromCharCode(0xFF11),          // fullwidth ONE
  String.fromCharCode(0x06F4),          // Extended Arabic-Indic FOUR
  "0x1_0",                              // int("1_0", 16) is 16; Number is NaN
  "0x" + String.fromCharCode(0x0661),   // the same hole reached through the radix path
];
for (const s of NUM_STRINGS) {
  const n = Number(s);
  out.push(["Number(string) " + JSON.stringify(s), Number.isNaN(n) ? "NaN" : String(n)]);
}
for (const [label, v] of [["null", null], ["true", true], ["false", false], ["empty array", []],
  ["array one", ["pending"]], ["array null", [null, 1]], ["nested", [[1, 2], [3]]],
  ["object", {}], ["array of object", [{}]]]) {
  out.push(["String(other) " + label, String(v)]);
}
process.stdout.write(JSON.stringify(out, null, 2) + "\n");
