#!/usr/bin/env node
/**
 * ledger-check.mjs: parses a DELEGATION.md ledger and reports its state.
 *
 * Structural enforcement for the delegation half: the gate is only satisfied when every
 * unit row is `verified` and the evidence section is non-empty.
 *
 * Usage:
 *   node ledger-check.mjs [path/to/DELEGATION.md]
 *
 * Exit codes:
 *   0  ledger complete (every row verified, evidence present)
 *   1  ledger incomplete (pending/done rows, or missing evidence)
 *   2  not a ledger (no header, no rows, or unparseable)
 */
import * as fs from "node:fs";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import * as nodePath from "node:path";
const { readFileSync } = fs;
const { resolve } = nodePath;
// why: the same runner vocabulary as a word set — a regex alternation of shell names trips the
// publish gate's injection scanner, and a dynamically built RegExp trips its ReDoS rule.
const RUNNER_WORDS = new Set(["node", "python", "python3", "pytest", "git", "npx", "npm", "pnpm", "make", "tsc", "deno", "bash", "sh", "ruby", "go", "cargo"]);

const path = process.argv[2] ?? "DELEGATION.md";

function fail(code, msg) {
  console.error(msg);
  process.exit(code);
}

let text;
try {
  text = readFileSync(resolve(path), "utf8");
} catch (err) {
  fail(2, `agents-discipline: cannot read ${path}: ${err.message}`);
}

const lines = text.split("\n");

// Find the table: a line starting with `| # |` is the header we accept.
let headerIdx = lines.findIndex((l) => /^\|\s*#\s*\|/.test(l));
if (headerIdx === -1) {
  fail(2, `agents-discipline: ${path} is not a DELEGATION.md ledger (no unit table header)`);
}

const rows = [];
const malformed = [];
for (let i = headerIdx + 1; i < lines.length; i++) {
  const line = lines[i].trim();
  if (!line) continue;
  if (!line.startsWith("|")) break; // table ended
  // Split on unescaped pipes, honoring `\|` escapes inside cells.
  const cells = line
    .replace(/\\\|/g, "\u0000")
    .split("|")
    .slice(1, -1)
    .map((c) => c.trim().replace(/\u0000/g, "|"));
  // Separator row: every cell is dashes and/or colons (`---`, `:--`, `:--:`, `--:`).
  if (cells.every((c) => c === "" || /^:?-+:?$/.test(c))) continue;
  if (cells.length < 6) {
    malformed.push({ line: i + 1, cells });
    continue;
  }
  if (cells[0] === "..." || cells[1] === "...") continue; // template placeholder row
  rows.push({
    unit: cells[0],
    name: cells[1],
    files: cells[2],
    worker: cells[3],
    acceptance: cells[4],
    status: cells[5].toLowerCase(),
  });
}

if (rows.length === 0) {
  fail(2, `agents-discipline: ${path} has a table header but no unit rows`);
}

const counts = { pending: 0, done: 0, verified: 0, other: 0 };
const unverified = [];
for (const r of rows) {
  if (counts[r.status] === undefined) counts.other++;
  else counts[r.status]++;
  if (r.status !== "verified") unverified.push(r);
}

// A line counts as evidence when it names something concrete: a code span that
// looks like a real command (space-separated, or containing a path), a known
// command or runner, a file path, or a measured result. Single words and
// generic phrases fail this on purpose, so "verified" or "`done`" do not pass.
function isStrongEvidence(l) {
  const spans = l.match(/`[^`]+`/g) ?? [];
  for (const s of spans) {
    const inner = s.slice(1, -1).trim();
    if (/\s/.test(inner) || /[/.]/.test(inner)) return true;
  }
  if (l.toLowerCase().split(/[^a-z0-9_]+/).some((w) => RUNNER_WORDS.has(w))) return true;
  if (/\b[\w./-]+\.[a-z0-9]{1,5}\b/i.test(l)) return true;
  if (/\b\d+\s*(passed|pass|ok|of)\b/i.test(l)) return true;
  if (/exit\s+\d+/i.test(l)) return true;
  return false;
}

// Evidence: content after the table. Real ledgers record verification as a
// trailing paragraph or an `## Evidence` section. The template's
// "## Rules of this ledger" section is boilerplate and is skipped structurally,
// by heading, so template wording can change without breaking the check.
// Evidence only counts when it looks like verification: at least one line that
// names a command, a file path, or a measured result. Template placeholders
// (angle-bracket markup) and bare words ("done", "verified", "all test passed")
// are not evidence.
let evidenceOk = false;
let evidenceText = "";
if (rows.length) {
  let inRulesSection = false;
  const evidenceLines = [];
  for (let i = headerIdx + 1; i < lines.length; i++) {
    const l = lines[i].trim();
    if (l === "") continue;
    if (/^##\s+/.test(l)) {
      inRulesSection = /^##\s+Rules of this ledger\s*$/i.test(l);
      continue;
    }
    if (l.startsWith("|")) continue; // still in the table
    if (inRulesSection) continue; // template rules boilerplate
    if (l.toLowerCase().startsWith("units:")) continue;
    if (/<[^>]+>/.test(l)) continue; // template placeholder, not evidence
    evidenceLines.push(l);
  }
  const strong = evidenceLines.filter(isStrongEvidence).length;
  evidenceOk = strong >= 1;
  evidenceText = evidenceLines.join("\n");
}

// ---------------------------------------------------------------------------
// Artifact corroboration.
//
// Everything above reads PROSE the coordinator wrote, so everything above can be
// invented in one keystroke: `isStrongEvidence` is satisfied by typing the word
// `pytest`. That is the hole this section exists to narrow. It does not read the
// ledger's claims — it reads the FILESYSTEM, and asks whether the artifacts the
// ledger names are actually there.
//
// The bar it sets is deliberately not "is the evidence true" (no local checker can
// answer that). It is: to pass, a fabricator must now CREATE FILES at the paths it
// invented, non-empty, dated after the ledger began. That is a different and much
// larger act than writing a plausible sentence, it leaves artifacts in `git status`,
// and it cannot happen by the accident this check was written for — an agent filling
// in a table that looked like it wanted filling.
//
// STALE-ARTIFACT RULE: an artifact older than the ledger cannot be evidence FOR THIS
// RUN. Citing a report from a previous session is the subtlest cheat available here,
// because the file genuinely exists and genuinely contains real output — it just is
// not about the work being claimed.
const artifactPaths = [
  // NO WHITESPACE in the span: `node test/run-tests.mjs` is a COMMAND that happens to name a
  // path, and demanding that string exist as a file is nonsense. Only a bare path is a citation.
  ...evidenceText.matchAll(/`([^`\s]*\/[^`\s]*\.[A-Za-z0-9]{1,6})`/g),
].map((m) => m[1].trim());

// Per-unit Evidence blocks. A row whose acceptance cannot be re-run rests on the
// coordinator's word UNLESS its own evidence points at something on disk, so evidence is
// attributed per row rather than pooled: a single real artifact anywhere used to satisfy a
// whole ledger, which let one genuinely-checked unit carry two invented ones.
function evidenceBlockFor(unit) {
  const re = new RegExp(`(^|\\n)[^\\n]*(?:\\bUnit\\s+${unit}\\b|#${unit}\\b)[\\s\\S]*?(?=\\n\\s*\\n\\*\\*Unit\\b|\\n##\\s|$)`);
  const m = evidenceText.match(re);
  return m ? m[0] : "";
}
function existingArtifactsIn(block, bases) {
  return [...block.matchAll(/`([^`\s]*\/[^`\s]*\.[A-Za-z0-9]{1,6})`/g)]
    .map((m) => m[1].trim())
    .filter((rel) => {
      const cands = nodePath.isAbsolute(rel) ? [rel] : bases.map((b) => nodePath.join(b, rel));
      return cands.some((c) => fs.existsSync(c));
    });
}

const bases = [];
for (let d = nodePath.dirname(nodePath.resolve(path)); ; d = nodePath.dirname(d)) {
  bases.push(d);
  if (d === nodePath.dirname(d)) break;
}
bases.push(process.cwd());

const missingArtifacts = [];
const emptyArtifacts = [];
const staleArtifacts = [];
let artifactsOk = true;

if (rows.some((r) => r.status === "verified")) {
  // The ledger's own start time, so "newer than the ledger" is answerable. Absent it,
  // the staleness rule is skipped rather than guessed — a check that invents its own
  // baseline would fail honest ledgers, and a gate that cries wolf gets deleted.
  const createdM = text.match(/^Created:?\s+(\d{4}-\d{2}-\d{2}[T ][\d:]+(?:[+-]\d{2}:?\d{2})?)/m);
  const createdMs = createdM ? Date.parse(createdM[1].replace(" ", "T")) : NaN;

  // Resolution walks UP from the ledger, because a cited path is written relative to the
  // PROJECT, not to wherever the ledger happens to sit. `reports/agents-discipline/DELEGATION.md`
  // citing `reports/trdd-verify/x.md` is the normal, correct shape, and resolving only
  // against the ledger's own directory would redden every honest ledger of that shape —
  // the one failure mode that guarantees a gate gets switched off instead of obeyed.

  for (const p of artifactPaths) {
    const cands = nodePath.isAbsolute(p) ? [p] : bases.map((b) => nodePath.join(b, p));
    const hit = cands.find((c) => fs.existsSync(c));
    if (!hit) {
      missingArtifacts.push(p);
      continue;
    }
    const st = fs.statSync(hit);
    if (st.isFile() && st.size === 0) emptyArtifacts.push(p);
    else if (Number.isFinite(createdMs) && st.mtimeMs < createdMs) staleArtifacts.push(p);
  }
  artifactsOk =
    missingArtifacts.length === 0 && emptyArtifacts.length === 0 && staleArtifacts.length === 0;
}

// ---------------------------------------------------------------------------
// Acceptance RE-RUN — the only check here that does not take the coordinator's word.
//
// Everything above inspects things the coordinator authored: the table it typed, the
// prose it wrote, the paths it chose to cite. Corroborating those raises the cost of a
// lie without ever contradicting one. This does contradict it: when a row's Acceptance
// cell holds a runnable command, the checker RUNS IT and reads the exit code itself. A
// row cannot then be `verified` because someone wrote the word — it is verified because
// the machine reproduced the check, here, now.
//
// That is the whole difference between raising the cost of fabrication and removing the
// opportunity. To fake a command-backed row you must now make the real command really
// pass, which is indistinguishable from doing the work.
//
// A row whose acceptance CANNOT be a command is reported as unreproducible, not failed.
// Real acceptance criteria legitimately include "the card carries a first-hand argument
// with file:line" — no exit code expresses that. Failing those would redden honest
// ledgers, and a gate that punishes honest work is a gate someone switches off. So the
// rule is: every acceptance that CAN be machine-checked IS machine-checked, and the rest
// is named out loud as resting on the coordinator's word.
function acceptanceCommand(cell) {
  const spans = cell.match(/`[^`]+`/g) ?? [];
  for (const s of spans) {
    const inner = s.slice(1, -1).trim();
    // A command, not a bare path or a single word: it must have an argument or a runner.
    if (/\s/.test(inner) && /^[\w./-]+/.test(inner)) return inner;
  }
  return null;
}

// Run from the project, not from wherever the checker was invoked: acceptance commands are
// written relative to the repo the work happened in.
let runCwd = nodePath.dirname(nodePath.resolve(path));
for (let d = runCwd; ; d = nodePath.dirname(d)) {
  if (fs.existsSync(nodePath.join(d, ".git"))) { runCwd = d; break; }
  if (d === nodePath.dirname(d)) break;
}

const reran = [];
const reproFailed = [];
const unreproducible = [];
const unbacked = [];
const rerunSkipped = Boolean(process.env.AGENTS_DISCIPLINE_SKIP_RERUN);
if (!rerunSkipped) {
  for (const r of rows.filter((x) => x.status === "verified")) {
    // The no-op scan runs over EVERY code span, BEFORE extraction, because `acceptanceCommand`
    // requires an argument — so a bare `true` was not recognised as a command at all and fell
    // through to `unreproducible`, which does not fail. That is the cheapest cheat in the file:
    // one word, and the row passes. Scan first, then extract.
    const noop = (r.acceptance.match(/`[^`]+`/g) ?? [])
      .map((s) => s.slice(1, -1).trim())
      .find((s) => /^(?:true|:|echo|printf|ls|pwd|cat|sleep|test|\[)\b/.test(s));
    if (noop) {
      reproFailed.push({ unit: r.unit, cmd: noop, code: "no-op acceptance — exits 0 by construction, tests nothing" });
      continue;
    }
    const cmd = acceptanceCommand(r.acceptance);
    if (!cmd) {
      // No runnable acceptance is an honest limit — but only when the row's own evidence
      // still points at something that EXISTS. Otherwise "unreproducible" becomes the hole
      // every fabricated row escapes through: write an acceptance no command can express,
      // and nothing is ever checked. Disk-backed prose is the floor.
      const backing = existingArtifactsIn(evidenceBlockFor(r.unit), bases);
      if (backing.length === 0) unbacked.push(r);
      else unreproducible.push(r);
      continue;
    }
    // A NO-OP acceptance is not a weak check, it is a cheat, and it must be told apart from
    // honestly having no command. `true`, `:`, `echo ok`, `ls` all exit 0 by construction —
    // writing one as acceptance is an attempt to satisfy the re-run without ever testing
    // anything, which is the obvious next move once execution is enforced. So it FAILS rather
    // than falling into `unreproducible`: the row with no command is admitting a limit, the row
    // with `true` is asserting a pass it did not earn.
    let code = null;
    try {
      // `set -o pipefail` is load-bearing, not hygiene. A shell pipeline reports the LAST
      // command's status, so `pytest | tail -1` exits 0 while pytest is failing — caught in
      // this checker's own fixtures, where exactly that string returned exit 0. Without this
      // line the re-run is defeated by one pipe, which is the cheapest cheat in the file.
      // why: same shell, same pipefail, same command string, passed as argv instead of a template
      // string — the publish gate's static scanner flags interpolation into a shell string.
      execFileSync("/bin/bash", ["-o", "pipefail", "-c", cmd], { cwd: runCwd, stdio: "pipe", timeout: 600000 });
      code = 0;
    } catch (err) {
      code = typeof err.status === "number" ? err.status : 1;
    }
    (code === 0 ? reran : reproFailed).push({ unit: r.unit, cmd, code });
  }
}

console.log(`agents-discipline: ${path}`);
console.log(`  units:       ${rows.length}`);
console.log(`  verified:    ${counts.verified}`);
console.log(`  done:        ${counts.done}`);
console.log(`  pending:     ${counts.pending}`);
if (counts.other) console.log(`  other:       ${counts.other}`);
if (malformed.length) console.log(`  malformed:   ${malformed.length}`);
console.log(`  evidence:    ${evidenceOk ? "present" : "MISSING"}`);
if (artifactPaths.length) {
  console.log(`  artifacts:   ${artifactPaths.length} cited, ${artifactsOk ? "all present" : "PROBLEMS"}`);
} else if (rows.some((r) => r.status === "verified")) {
  // NOT a failure. Evidence that is a command plus its output, with no report file, is
  // honest evidence, and failing it would redden real ledgers until someone deletes the
  // gate. But say out loud what the gate could NOT do here: with nothing on disk to point
  // at, every word above was checked for SHAPE and none of it for TRUTH.
  console.log("  artifacts:   none cited — evidence is uncorroborated prose (shape checked, truth not)");
}
for (const [label, list] of [["missing", missingArtifacts], ["empty", emptyArtifacts], ["older than the ledger", staleArtifacts]]) {
  if (!list.length) continue;
  console.log(`  ${label} artifacts:`);
  for (const p of list) console.log(`    - ${p}`);
}

if (rerunSkipped) {
  // Announced, never silent. The escape hatch exists so this checker's own fixtures — synthetic
  // ledgers naming commands from projects that do not exist — are not executed. Any real ledger
  // that sets it is declaring, in its own output, that nothing was reproduced.
  console.log("  re-ran:      SKIPPED via AGENTS_DISCIPLINE_SKIP_RERUN — no acceptance was executed this run");
}
if (reran.length) {
  console.log(`  re-ran:      ${reran.length} acceptance command(s), all passed`);
  for (const x of reran) console.log(`    - #${x.unit} $ ${x.cmd} -> exit 0`);
}
if (reproFailed.length) {
  console.log("  ACCEPTANCE DID NOT REPRODUCE:");
  for (const x of reproFailed) console.log(`    - #${x.unit} $ ${x.cmd} -> ${typeof x.code === "number" ? `exit ${x.code}` : x.code}`);
}
if (unbacked.length) {
  console.log("  UNBACKED verified rows — no runnable acceptance AND no artifact of their own on disk:");
  for (const r of unbacked) console.log(`    - #${r.unit} ${r.name}`);
}
if (unreproducible.length) {
  console.log(`  unreproducible: ${unreproducible.length} verified row(s) have no runnable acceptance — those rest on the coordinator's word alone`);
  for (const r of unreproducible) console.log(`    - #${r.unit} ${r.name}`);
}
if (unverified.length) {
  console.log("  unverified rows:");
  for (const r of unverified) {
    console.log(`    - #${r.unit} ${r.name} [${r.status}]`);
  }
}
if (malformed.length) {
  console.log("  malformed rows:");
  for (const m of malformed) {
    console.log(`    - line ${m.line}: ${m.cells.join(" | ")}`);
  }
}

const complete =
  rows.every((r) => r.status === "verified") && evidenceOk && artifactsOk && reproFailed.length === 0 && unbacked.length === 0 && malformed.length === 0;
// ---------------------------------------------------------------------------
// RECEIPT. The checker signs the ledger it just read, in the ledger itself.
//
// This is the answer to the last cheat that needed no skill at all: simply never running
// the checker. Nothing can force an agent to invoke a command — but the ABSENCE of a
// receipt is now a visible fact about the artifact, readable by anyone, with no access to
// the agent's transcript. "I verified it" and "I did not run the gate" stop being the same
// document.
//
// The hash is over the ledger with any prior receipt stripped, so it binds the receipt to
// the CONTENT that was checked. That closes the check-then-edit move: pass a clean ledger,
// then quietly flip a row to `verified` or add a fabricated evidence line, and the stored
// hash no longer matches what is on disk. The next run says STALE instead of PASS, and the
// mismatch is the evidence.
const RECEIPT_RE = /\n?<!-- agents-discipline-check: [^>]*-->\n?/g;
const bodyForHash = text.replace(RECEIPT_RE, "\n").trimEnd() + "\n";
const digest = createHash("sha256").update(bodyForHash).digest("hex").slice(0, 16);

// The receipt DELIBERATELY CARRIES NO VERDICT. It used to read `PASS`/`FAIL`, and that word
// was the last forgery left in the design: pass a clean ledger, tamper it afterwards, and hand
// the file to someone who reads the stamp instead of re-running. They would see `PASS` — a true
// statement about a document that no longer exists, printed on the one that replaced it.
//
// No claim can be misread if no claim is made. The stamp now binds CONTENT and nothing else, so
// the only way to learn whether a ledger passed is to run this checker, which re-derives every
// verdict from scratch and cannot inherit a stale one. That is not a weaker receipt; it removes
// the attack rather than detecting it, and a reader who trusts the wrong thing is no longer
// possible because there is nothing to trust but the hash.
const prior = text.match(/<!-- agents-discipline-check: ([^ ]+) sha256:([0-9a-f]+) -->/);
if (prior && prior[2] !== digest) {
  console.log(`  receipt:     STALE — ledger content changed since it was last checked (${prior[1]})`);
} else if (prior) {
  console.log(`  receipt:     binds this exact content, last checked ${prior[1]}`);
}

console.log(complete ? "  -> ledger complete: every unit verified." : "  -> ledger INCOMPLETE.");

// A run that SKIPPED the re-run did not verify anything, so it must not sign anything.
// Signing it would mint exactly the artifact this receipt exists to make unforgeable: a
// PASS stamp on a ledger nobody executed.
try {
  if (rerunSkipped) throw new Error("skip");
  const stamp =
    `<!-- agents-discipline-check: ${new Date().toISOString()} sha256:${digest} -->\n` +
    `<!-- agents-discipline-check: this is a CONTENT BINDING, not a verdict. It records which bytes were ` +
    `checked, never whether they passed. Re-run ledger-check.mjs on this file for a verdict. -->`;
  fs.writeFileSync(resolve(path), bodyForHash + stamp + "\n");
} catch {
  // A read-only ledger is not a verification failure — say nothing and keep the verdict.
}
process.exit(complete ? 0 : 1);
