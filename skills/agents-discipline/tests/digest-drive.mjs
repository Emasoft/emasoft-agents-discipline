#!/usr/bin/env node
/**
 * Dump `sha256(JSON.stringify(oracle))` for a set of PINNED oracle objects.
 *
 * WHAT THIS IS AND IS NOT. `gate-check.mjs:358` computes the approval token as
 * `sha256(JSON.stringify(oracle(file, gate)))` over twelve fields. If the port's digest differs
 * by one byte, EVERY approval fails to match -- and the symptom is indistinguishable from a
 * missing `--approve`: both print `APPROVAL REQUIRED` and `NOT RUN`, and the unapproved branch
 * then runs zero commands and exits 1. No message names the cause. So it is worth settling
 * before 950 lines of port exist to be blamed instead.
 *
 * This file tests HALF the question, deliberately, and the half it skips is the expensive one:
 *   COVERED  -- given identical field VALUES, do the two runtimes serialize and hash them the
 *               same? That is where the named hazards live: `JSON.stringify` emits compact
 *               separators where `json.dumps` defaults to `", "` / `": "`, `json.dumps` escapes
 *               non-ASCII by default where `JSON.stringify` does not, JS renders `1.0` as `1`,
 *               and key ORDER is insertion order in both -- but only if the port preserves it.
 *   NOT COVERED -- does the port DERIVE the same values? `shell` comes from `resolveShell`
 *               (:306), `timeoutSeconds` from `timeoutValue` (:130) via `parseArgs`, `pathValue`
 *               from the executableCandidates/delimiter machinery (:299), `cwd` from
 *               `resolvedGateCwd` (:332). Obtaining those IS porting a chunk of gate_check.py,
 *               so it is a LATER gate. An earlier note promised this half at this half's price.
 *
 * Values are PINNED literals, not read from a real gate, so the comparison cannot pass by both
 * sides happening to read the same environment.
 *
 * EVERY non-printing character is built with String.fromCharCode, never written literally.
 * A first version of this file was authored with escape sequences that the writing layer
 * interpreted, putting a RAW U+2028 and raw control characters into the source -- the exact
 * hazard this project has already been bitten by once, and invisible in a diff.
 *
 * Pair: tests/digest_drive.py. Compared by tests/digest-diff.sh.
 */
import { createHash } from "node:crypto";

const CH = String.fromCharCode;

// Field order matches gate-check.mjs:340-356 EXACTLY. JSON.stringify emits insertion order, so
// this order is part of the hashed bytes -- reordering these lines changes every digest.
const oracle = (over) => ({
  schema: 1,
  check: "echo ok",
  expect: "ok",
  cwd: "/repo/packages/api",
  shell: "/bin/bash",
  timeoutMs: 120000,
  maxOutputBytes: 1048576,
  regexTimeoutMs: 250,
  regexStartupTimeoutMs: 5000,
  maxRegexWorkers: 4,
  platform: "darwin",
  path: "/usr/bin:/bin",
  ...over,
});

// Each case targets one hazard. A case that cannot distinguish the runtimes is still useful as
// a control, and is named as one.
export const CASES = {
  "CONTROL baseline": oracle({}),
  // json.dumps escapes non-ASCII by default (ensure_ascii=True); JSON.stringify never does.
  // This is the single most likely way a naive port diverges, and it changes every byte after.
  "non-ascii check": oracle({ check: "echo na" + CH(0xef) + "ve" }),
  "emoji beyond the BMP": oracle({ check: "echo " + CH(0xd83d, 0xde00) }),
  // Characters JSON MUST escape, where the two escape tables can differ.
  "control chars": oracle({ check: "a" + CH(1) + "b" + CH(31) + "c" }),
  "tab and newline": oracle({ check: "a" + CH(9) + "b" + CH(10) + "c" }),
  "quote and backslash": oracle({ check: 'a"b\\c' }),
  // U+2028/U+2029 are legal RAW inside a JSON string and JSON.stringify leaves them literal;
  // they are illegal only in JavaScript SOURCE. A port that escapes them diverges.
  "line separator U+2028": oracle({ expect: "a" + CH(0x2028) + "b" }),
  "paragraph separator U+2029": oracle({ expect: "a" + CH(0x2029) + "b" }),
  // The float hazard. `timeoutValue` (:130) rejects non-integers, so this is UNREACHABLE in
  // production -- kept to prove the differential WOULD catch it if a later change let one in.
  "float timeout (unreachable today)": oracle({ timeoutMs: 1500.0 }),
  // JSON.stringify renders a non-finite number as `null`; json.dumps writes the bare token
  // `Infinity`, which is NOT valid JSON. That is worse than a byte difference -- the port would
  // emit a document node cannot parse. Also unreachable in production, also kept as a guard.
  "infinity becomes null": oracle({ timeoutMs: Infinity }),
  "nan becomes null": oracle({ timeoutMs: NaN }),
  "negative zero": oracle({ timeoutMs: -0 }),
  "large integer": oracle({ maxOutputBytes: 9007199254740991 }),
  "empty strings": oracle({ check: "", expect: "", path: "" }),
  "platform without version digits": oracle({ platform: "freebsd" }),
};

const answers = {};
for (const [name, value] of Object.entries(CASES)) {
  const json = JSON.stringify(value);
  answers[name] = { json, sha256: createHash("sha256").update(json, "utf8").digest("hex") };
}
process.stdout.write(JSON.stringify(answers, null, 2) + "\n");
