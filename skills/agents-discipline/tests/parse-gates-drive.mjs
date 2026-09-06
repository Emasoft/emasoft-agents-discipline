#!/usr/bin/env node
/**
 * Dump the ORACLE `parseGates` result as JSON, so the Python port can be compared field by
 * field rather than through one consumer's lens.
 *
 * gate-lint reads only `gates`, `abandoned` and `errors`. `parseGates` also returns `lines`,
 * `eol`, `finalNewline`, `owns` and `warnings` — ported, and touched by nothing in either
 * runtime. `gate-check` is exactly what depends on them: `eol`/`finalNewline` to rewrite a
 * ledger without changing its line endings, `owns` for lease claims. Verifying them after
 * that port is built on them is the expensive order.
 *
 * Usage: node parse-gates-drive.mjs <ledger.md>
 */
import { readFileSync } from "node:fs";
import {
  automaticEvidencePrefix, classifyGateEvidence, gateDefinitionDigest, gateState, parseGates, tail,
} from "../scripts/lib/gates.mjs";

// Shapes a ledger cannot express, where classifyGateEvidence's three divergence classes meet:
// the 900 bound and the prefix offset are UTF-16 measures, and the success pattern ends `shell=.`
// -- and JS `.` excludes \n \r U+2028 U+2029 while Python's excludes only \n. Evidence ending
// `shell=<U+2028>` is automatic-current to a naive port and human to the oracle, which flips the
// gate's met/unmet state. The corpus is built from code points, never typed.
const RUNNABLE = { id: "s", checked: true, check: "echo ok", expect: "ok", cwd: null };
const OK_PREFIX = automaticEvidencePrefix(gateDefinitionDigest(RUNNABLE));
const BODY = " exit=0; EXPECT=matched; output-sha256=" + "a".repeat(64) + "; output-bytes=0; shell=";
const SYNTHETIC = [];
const cp = (n) => String.fromCodePoint(n);
for (const [name, evidence] of [
  ["absent", null], ["blank", ""], ["pending", "pending"], ["PENDING upper", "PENDING"],
  ["pending trailing newline", "pending\n"],           // JS $ does not match before it
  ["pending leading space", " pending"],
  ["falsy zero", 0], ["falsy false", false],
  ["current", OK_PREFIX + BODY + "x"],
  ["shell=LF", OK_PREFIX + BODY + "\n"],
  ["shell=CR", OK_PREFIX + BODY + "\r"],
  ["shell=LS U+2028", OK_PREFIX + BODY + cp(0x2028)],     // the reachable one
  ["shell=PS U+2029", OK_PREFIX + BODY + cp(0x2029)],
  ["shell=astral", OK_PREFIX + BODY + "\u{1F600}"],
  ["bytes over cap", OK_PREFIX + BODY.replace("output-bytes=0", "output-bytes=1048577") + "x"],
  ["bytes at cap", OK_PREFIX + BODY.replace("output-bytes=0", "output-bytes=1048576") + "x"],
  ["wrong digest", automaticEvidencePrefix("b".repeat(64)) + BODY + "x"],
  ["stale automatic", "automatic-evidence=v1; something else"],
  ["stale exit0", "exit=0; shell=sh"],
  ["human", "I ran it and it worked"],
  // 900 is a UTF-16 bound: 449 emoji is 898 units and 449 code points, so a naive len() and
  // js_length agree; 451 emoji is 902 units but only 451 code points, so len() ACCEPTS what the
  // oracle REJECTS. Both sit around the cap with an otherwise-valid body.
  ["long astral over cap", OK_PREFIX + BODY + "x" + "\u{1F600}".repeat(451)],
  ["long ascii over cap", OK_PREFIX + BODY + "x".repeat(900)],
]) {
  const gate = { ...RUNNABLE, evidence };
  SYNTHETIC.push([name, {
    evidence: classifyGateEvidence(gate),
    state: gateState(gate, new Map()),
    stateAbandoned: gateState(gate, new Map([["s", "why"]])),
    unchecked: gateState({ ...gate, checked: false }, new Map()),
    // A gate with no CHECK has no digest, so it takes the non-runnable arm.
    nonRunnable: gateState({ ...gate, check: "" }, new Map()),
  }]);
}
for (const [name, value] of [
  ["empty", ""], ["blank lines", "\n\n  \n"], ["one line", "hello"],
  ["three lines", "a\nb\nc"], ["crlf", "a\r\nb\r\nc"],
  ["trailing blank", "a\nb\n\n"], ["padded", "  a  \n  b  "],
  ["over 240", "x".repeat(300)],
  ["astral over 240", "\u{1F600}".repeat(200)],   // 400 UTF-16 units, 200 code points
  // cp(): built from CODE POINTS, never typed. The first version embedded raw U+2028, U+0085
  // and a vertical tab in this file -- the same invisible-source mistake the port keeps
  // making, here in the corpus whose whole subject is invisible characters.
  ["u2028 inside", "a" + cp(0x2028) + "b"],   // NOT a line break to split(/\r?\n/)
  ["nel inside", "a" + cp(0x85) + "b"],       // Python splitlines() WOULD break here
  ["vertical tab", "a" + cp(0x0b) + "b"],
  // tail() trims each line, and .trim() is not str.strip() in either direction. Without
  // these two, the js_trim inside tail had ZERO coverage.
  ["bom padded line", cp(0xfeff) + "a" + cp(0xfeff) + "\nb"],
  ["nel padded line", "a" + cp(0x85) + "\nb"],
]) {
  SYNTHETIC.push(["tail " + name, tail(value)]);
}

const doc = parseGates(readFileSync(process.argv[2], "utf8"));
// The digest is compared as HEX, per gate, over the real fixtures -- so the unicode fixture
// decides ensure_ascii and every fixture decides the separators. A boolean "both produced a
// digest" would agree while the two hashes differed, which is the whole failure mode: the
// digest binds an approval to the CHECK/EXPECT/CWD that was approved, so a mismatch does not
// raise, it silently reports "not approved" and sends the reader to the approval store.
const digests = doc.gates.map((gate) => {
  const digest = gateDefinitionDigest(gate);
  try {
    return { id: gate.id, digest, prefix: digest === null ? null : automaticEvidencePrefix(digest) };
  } catch (error) {
    return { id: gate.id, digest, prefix: "threw: " + error.message };
  }
});
// Sets and Maps do not survive JSON.stringify; both sides emit sorted arrays so the
// comparison is on content, not on iteration order.
process.stdout.write(JSON.stringify({
  lines: doc.lines,
  eol: doc.eol,
  finalNewline: doc.finalNewline,
  gates: doc.gates,
  abandoned: [...doc.abandoned].sort(),
  owns: doc.owns,
  errors: doc.errors,
  warnings: doc.warnings,
  digests,
  // Per parsed gate, so every fixture contributes; plus the synthetic corpus below for the
  // shapes a ledger cannot express.
  verdicts: doc.gates.map((g) => ({
    id: g.id, evidence: classifyGateEvidence(g), state: gateState(g, doc.abandoned),
  })),
  synthetic: SYNTHETIC,
}, null, 2) + "\n");
