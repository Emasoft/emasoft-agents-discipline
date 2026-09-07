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
  "-0", "--5", "1e", "+-1", "\u{FEFF}12", "\u{2028}12", "\u{00A0}12"];
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
