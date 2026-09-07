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
  // A LONE HIGH SURROGATE -- not a well-formed character, and reachable: CPython
  // surrogateescape-decodes sys.argv ITSELF, so a `--cwd` byte that is not valid UTF-8 arrives
  // as U+DCxx and lands in this payload. The port CRASHED here (UnicodeEncodeError from
  // sha256's strict encode) where the oracle returned a digest -- on the approval-identity
  // path, the highest-ranked hazard in this port. Measured, then fixed by escaping lone
  // surrogates the way JSON.stringify does; this row is what stops it coming back.
  // GROUND -- and the first version of this comment claimed "mutation-isolated: dropping
  // _js_json_text's substitution reddens this row and no other". THAT WAS FALSE, and the probe
  // said so: NOTHING REDDENED. This differential compares the two DRIVERS' hand-built digests
  // and never calls gates.gate_definition_digest, so no mutation of gates.py can reach it.
  // Mutating the DRIVER'S own substitution does not work either -- the mutant CRASHES on this
  // row (UnicodeEncodeError) instead of diverging, and guard 3 correctly refuses to score it.
  //
  // What this row actually proves is narrower and still worth having: two INDEPENDENT
  // reimplementations of JSON.stringify agree on a lone surrogate. It could not have been added
  // before the fix -- the driver crashed on it, which is how the SECOND copy of the bug was
  // found. The PRODUCTION function is covered separately, in python-lib-checks, against node.
  "lone surrogate": oracle({ check: "echo " + CH(0xd800) }),
  // THE `path` FIELD'S TWO NON-STRING SHAPES. An earlier version of this block had a third,
  // "absent path (undefined is DROPPED, not null)", built by deleting the key -- and it was
  // WRONG: a row justified by a hazard the oracle cannot produce, which is the exact defect
  // this corpus exists to catch in other people's code. gate-check.mjs:325 reads
  //     const pathValue = opt.status ? null : String(process.env.PATH || "");
  // so `|| ""` turns an unset PATH into the empty STRING and `String(...)` guarantees a string.
  // `undefined` never reaches the payload; the key is never dropped. Settled by reading the
  // line rather than by reasoning about `process.env`.
  //
  // What that same line DOES produce is two shapes no other row covers, and both are three-way
  // distinct in the hashed bytes ("" vs null vs a real string):
  "empty path (unset PATH becomes the empty string, not null)": oracle({ path: "" }),
  // `null` from the ternary's other arm. A SERIALIZER CONTROL, and now for a MEASURED reason
  // rather than a hedge -- the third version of this comment, because the first two each
  // asserted more than had been checked:
  //   v1 "the --status arm"        -- claimed --status reaches this payload. Wrong: :165 is
  //                                   `if (opt.status && opt.approve) failUsage(...)`, exit 2,
  //                                   so --status never produces an approval FILE.
  //   v2 "probably still hashed"   -- retreated to a guess, since :736's
  //                                   approvalOracleSignature is not gated on opt.approve.
  //                                   Also wrong, and a guess in a comment is read as a finding.
  //   v3 (this one) MEASURED       -- :721 is
  //                                       if (opt.status || (!opt.reverify && state === "met"))
  //                                         continue;
  //                                   and it precedes the pending.push at :729 whose :736
  //                                   computes the signature. Under --status the loop CONTINUES.
  //                                   The signature is NEVER computed. Settled by reading the
  //                                   control flow, which is what both earlier versions skipped.
  //
  // So the null payload is BUILT (:324-325 assign null) and never HASHED. This row therefore
  // models a shape the serializer must handle, not a digest the oracle ever emits -- which is
  // exactly what "serializer control" means, and now it is true rather than defensive.
  // What the row proves: null, "" and absent are three DISTINCT digests
  // (measured 99c12694 / 4eb17f71 / 2535b409), so a port normalizing between them reddens.
  "null path (serializer control -- null is not empty-string)": oracle({ path: null }),
  // `shell` HAS THE SAME TERNARY and the corpus had the same gap -- every row carried
  // "/bin/bash" and none carried null. gate-check.mjs:324 is
  //     const shell = opt.status ? null : resolveShell(opt.shell);
  // so under --status BOTH ambient fields are null in the same payload. Found by asking the
  // question the previous row's failure taught: `path`'s definition was read, `shell`'s and
  // `timeoutSeconds`' were inherited from whoever wrote this driver, unexamined.
  //
  // THIS BLOCK ONCE READ "REACHABILITY MEASURED, not assumed", and it was the worst comment in
  // the file: it claimed :736's approvalOracleSignature was "unconditional", wore the word
  // MEASURED, and was false. :721 gates the whole push --
  //     if (opt.status || (!opt.reverify && state === "met")) continue;
  // -- so under --status the signature is never computed. What I had actually measured was that
  // `--status` exits 0 on a one-gate ledger; I then inferred the payload was hashed from the
  // fact that the program did not exit EARLY, which does not follow. A label asserting its own
  // rigour is the one a reader will not re-check, so it buys the least scrutiny where the claim
  // needed the most.
  // The row stands unchanged as a SERIALIZER CONTROL for the reason given above.
  "null shell (serializer control -- same ternary as path)": oracle({ shell: null }),
  "both ambient fields null (serializer control)": oracle({ shell: null, path: null }),
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
  // NON-INTEGRAL floats, the gap that let a real defect through. The corpus had only 1500.0,
  // which is integral -- so a serializer that round-tripped through a Python float survived by
  // accident. These are the two spellings where the runtimes genuinely disagree: Python
  // zero-pads the exponent (1e-07), and the two switch to exponential notation at DIFFERENT
  // thresholds (Python at 1e-4, JS not until 1e-6).
  "float exponent spelling": oracle({ timeoutMs: 1e-7 }),
  "float at the notation threshold": oracle({ timeoutMs: 0.000001 }),
  "float non-integral": oracle({ timeoutMs: 1.5 }),
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
