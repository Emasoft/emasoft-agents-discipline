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
  "+002020-01-01T00:00:00Z", "-002020-01-01T00:00:00Z", "2020-01-01t00:00:00z",
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
process.stdout.write(JSON.stringify(out, null, 2) + "\n");
