#!/usr/bin/env node
/**
 * Run the ORACLE (`scripts/gate-check.mjs`) over a corpus of argv vectors and dump the exit
 * code, stdout and stderr of each. Pair: tests/argv_drive.py. Compared by tests/argv-diff.sh.
 *
 * WHY BLACK-BOX AND NOT A FUNCTION-LEVEL DRIVER. `parseArgs`, `timeoutValue`, `jobCount` and
 * `asDirectory` are not exported, and the method holds the JS side FIXED as the oracle -- so
 * adding an `export` to reach them would edit the specification to suit the test. The other
 * option, transcribing the four functions into this driver the way digest-drive.mjs transcribes
 * the oracle object, would be testing my transcription against my transcription: both sides
 * authored by the same hand in the same hour, agreeing for that reason rather than because the
 * port is right. Running the real program has neither problem. It also happens to be the only
 * form that exercises the argv DECODING layer, where the two runtimes genuinely differ.
 *
 * EVERY VECTOR MUST TERMINATE AT OR BEFORE gate-check.mjs:179, because that is where the port
 * stops. Two terminators make that reachable for any mode:
 *   `--root <missing>`  fails at :175, BEFORE timeoutValue -- works in every mode, including
 *                       --status and the pipeline actions.
 *   `--cwd <missing>`   fails at :179, AFTER timeoutValue/jobCount -- so it is what makes an
 *                       ACCEPTED numeric value observable at all.
 *
 * That second one is the load-bearing trick of this file. A rejected value prints the same
 * message whether the port read `0x41` as 65 or as NaN, so rejections alone cannot tell a
 * correct Number() from a broken one. Pairing an accepted value with a failing --cwd inverts
 * it: the oracle reports the --cwd error (proving the number was taken), while a port whose
 * Number() returned NaN reports the --timeout error instead. The two messages differ, so the
 * defect is visible. Rows are laid out in accept/reject PAIRS across the range boundary for
 * exactly this reason.
 */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const ENTRY = join(HERE, "..", "scripts", "gate-check.mjs");

// A path that cannot exist. Used as both terminators; the drivers must agree on it verbatim
// because it is echoed back inside the expected message.
const MISSING = "/nonexistent-agents-discipline-xyz";

// Shared VERBATIM with argv_drive.py. Grouped by the property each row is here to pin.
export const CASES = [
  // --- parseArgs: unknown / malformed options ---
  ["--bogus"],
  ["-x"],
  ["-"],
  ["-hh"],
  ["--="],
  ["--unknown=value"],
  // --- parseArgs: a value option with no value ---
  ["--timeout"],
  ["--timeout="],
  ["--jobs"],
  ["--shell="],
  ["--scope="],
  // `--timeout --jobs` takes the NEXT ARGUMENT as the value, even though it looks like an
  // option: the oracle only checks for undefined and empty. So this is NOT "needs a value";
  // it is a timeout of "--jobs", which fails validation later with that text quoted back.
  ["--timeout", "--jobs"],
  // --- parseArgs: duplicate detection ---
  ["--scope=x", "--scope=y"],
  ["--scope", "a", "--scope", "b"],
  ["--status", "--status"],
  ["--claim", "--claim"],
  // NOT a duplicate: `^-+` strips every leading dash, so `-h` keys as "h" and `--help` as
  // "help". Two different keys. This row asserts the difference by NOT erroring.
  ["--help", "-h"],
  // --- help ---
  ["-h"],
  ["--help"],
  // A parse error still wins over help, because parseArgs runs to completion first.
  ["--help", "--bogus"],
  // --- mutual exclusion (all terminate at :159-173, no terminator needed) ---
  ["--status", "--reverify"],
  ["--status", "--approve"],
  ["--claim", "--release"],
  ["--log", "text", "--list-scopes"],
  ["--claim", "--status"],
  ["--release", "--reverify"],
  ["--claim", "ledger.md"],
  ["ledger.md", "--scope", "s"],
  ["--leaf", "x"],
  ["--timeout", "5", "--status"],
  ["--jobs", "2", "--claim"],
  ["--shell", "/bin/sh", "--status"],
  ["--cwd", ".", "--status"],
  // --- asDirectory (:121-128) ---
  ["--root", MISSING],
  ["--cwd", MISSING],
  // --- value REJECTIONS. Root defaults to cwd and exists, so these reach :176/:177. ---
  ["--timeout", "abc"],
  ["--timeout", "0"],
  ["--timeout", "86401"],
  ["--timeout", "5.5"],
  ["--timeout", "1_000"],
  ["--timeout", "-5"],
  ["--timeout", "Infinity"],
  ["--timeout", "1e400"],
  ["--timeout", "NaN"],
  ["--timeout", "0x"],
  ["--timeout", "+0x10"],
  ["--jobs", "65"],
  ["--jobs", "0"],
  ["--jobs", "abc"],
  ["--jobs", "2.5"],
  // --- value ACCEPTANCE, paired with a failing --cwd so acceptance is observable. Each pair
  //     straddles a boundary: the accepted row must report --cwd, the rejected row --timeout.
  ["--cwd", MISSING, "--timeout", "0x15180"],   // 86400 -- accepted
  ["--cwd", MISSING, "--timeout", "0x15181"],   // 86401 -- rejected
  ["--cwd", MISSING, "--jobs", "0x40"],         // 64 -- accepted
  ["--cwd", MISSING, "--jobs", "0x41"],         // 65 -- rejected
  ["--cwd", MISSING, "--timeout", "1e3"],
  ["--cwd", MISSING, "--timeout", "5.0"],
  ["--cwd", MISSING, "--timeout", " 12 "],
  ["--cwd", MISSING, "--timeout", "1e-3"],      // 0.001 -- not an integer
  ["--cwd", MISSING, "--jobs", "1e1"],
  ["--cwd", MISSING, "--timeout", "1"],
  ["--cwd", MISSING, "--timeout", "86400"],
  // --- `--` and positional handling ---
  // The terminator has to come BEFORE the `--`, since everything after it is a file. If `--`
  // handling were broken, `--bogus` would raise "unknown option" at parse time instead of the
  // --root error, so this row discriminates.
  ["--root", MISSING, "--", "--bogus"],
  ["--root", MISSING, "--", "-h"],
  // The SECOND `--` is swallowed too (the `arg === "--"` test precedes the positional guards).
  // With a scope present, a non-empty file list would raise the mutual-exclusion error, so
  // "which error appears" is what distinguishes swallowed from not.
  ["--root", MISSING, "--scope", "s", "--", "--"],
  ["--scope", "s", "--", "ledger.md"],
  // --- hostile values: JSON.stringify then the terminal sanitizer ---
  ["--timeout", String.fromCharCode(0x2028)],
  ["--timeout", "a" + String.fromCharCode(0x001b) + "b"],
  ["--timeout", String.fromCharCode(0x00e9)],
  ["--timeout", String.fromCharCode(0x202f)],
  ["--timeout", "a" + String.fromCharCode(0x0000) + "b"],
  ["--timeout", String.fromCharCode(0x200e)],
  ["--timeout", '"quoted"'],
  ["--timeout", "back\\slash"],
];

// The one legitimate difference between the runtimes is that a program names itself in its own
// usage line (the convention gate_lint.py set). Collapse ONLY that token, and ONLY where it is
// the program's self-name, so every other byte of the help text is still compared. stderr is
// deliberately NOT touched: the port transliterates "run gate-check.mjs --help for usage"
// verbatim, so both sides already carry the same bytes there and normalizing would break it.
const normalize = (stdout) => stdout.replace("usage: gate-check.mjs ", "usage: <PROGRAM> ");

const out = [];
for (const args of CASES) {
  const result = spawnSync(process.execPath, [ENTRY, ...args], { encoding: "utf8" });
  out.push({
    args,
    status: result.status,
    stdout: normalize(result.stdout || ""),
    stderr: result.stderr || "",
  });
}
process.stdout.write(JSON.stringify(out, null, 2) + "\n");
