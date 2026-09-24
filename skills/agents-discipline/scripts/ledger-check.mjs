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
 *   1  ledger incomplete (pending/done rows, or missing evidence), OR the ledger changed on
 *      disk before the verdict was printed (verdict VOID, no receipt signed)
 *   2  not a ledger (no header, no rows, or unparseable)
 */
import * as fs from "node:fs";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import * as nodePath from "node:path";
import { readStableRegularFile, writeAtomic } from "./lib/gates.mjs";
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
  // Every other reader in this codebase goes through readStableRegularFile; this one used a
  // plain readFileSync, with no size cap, no regular-file assertion and no O_NOFOLLOW. A FIFO
  // passed as argv blocked the process forever -- an unbounded wait in the one script a
  // coordinator is told to run on a ledger it may not have written.
  text = readStableRegularFile(path, { label: "ledger" });
} catch (err) {
  fail(2, `agents-discipline: cannot read ${path}: ${err.message}`);
}

const lines = text.split("\n");

// Find the table: a line starting with `| # |` is the header we accept.
let headerIdx = lines.findIndex((l) => /^\|\s*#\s*\|/.test(l));
if (headerIdx === -1) {
  fail(2, `agents-discipline: ${path} is not a DELEGATION.md ledger (no unit table header)`);
}

// The header's width is the contract every row is held to. Without it the only shape check
// was `cells.length < 6`, which cannot see the failure that actually happens: an UNESCAPED
// pipe inside an Acceptance command adds a column, so a 6-column row becomes 7 cells, passes
// `< 6`, and `Status` is then read from `cells[5]` -- the wrong cell. The row parses silently
// as whatever the shifted text happens to say. Widening the table has the mirror problem: a
// row omitting the new trailing columns reads them as `undefined` forever, with no error.
// A space, not a NUL byte: a raw NUL here made git treat this whole file as binary; only
// .split("|").length reads the replaced string, and the port uses a space too.
const columnCount = lines[headerIdx].trim().replace(/\\\|/g, " ").split("|").length - 1 - (/(^|[^\\])\|$/.test(lines[headerIdx].trim()) ? 1 : 0);

const rows = [];
const malformed = [];
for (let i = headerIdx + 1; i < lines.length; i++) {
  const line = lines[i].trim();
  if (!line) continue;
  if (!line.startsWith("|")) break; // table ended
  const cells = line
    .replace(/\\\|/g, "\u0000")
    .split("|")
    // A trailing pipe only delimits when it is UNESCAPED. Testing `line.endsWith("|")` looked
    // at the RAW line while the split above ran on the escaped one, so a row whose last cell
    // ends in `\|` had that cell silently dropped -- and since the header used the same wrong
    // test, the two agreed and no malformed error fired. The column just vanished.
    .slice(1, /(^|[^\\])\|$/.test(line) ? -1 : undefined)
    .map((c) => c.trim().replace(/\u0000/g, "|"));
  // Separator row: every cell is dashes and/or colons (`---`, `:--`, `:--:`, `--:`).
  if (cells.every((c) => c === "" || /^:?-+:?$/.test(c))) continue;
  if (cells.length < 6 || cells.length !== columnCount) {
    malformed.push({
      line: i + 1,
      cells,
      // Direction matters, because the two causes need opposite fixes: too MANY cells is
      // almost always an unescaped pipe inside a command, too FEW is a row that never got
      // the columns the header added.
      why: cells.length < 6
        ? `only ${cells.length} cells; a unit row needs at least 6`
        : cells.length > columnCount
          ? `${cells.length} cells but the header declares ${columnCount} — escape a literal pipe inside a cell as \\|`
          : `${cells.length} cells but the header declares ${columnCount} — the row is missing ${columnCount - cells.length} trailing cell(s)`,
    });
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
  // Every diagnostic below this point is printed by the summary, which this bail skips. So a
  // ledger whose rows are ALL malformed used to report "no unit rows" on a file that visibly
  // has rows, with the reason already computed and then thrown away. Say it here instead.
  if (malformed.length) {
    const lines_ = malformed.map((m) => `    - line ${m.line}: ${m.why}`).join("\n");
    fail(2, `agents-discipline: ${path} has a table header but every row is malformed:\n${lines_}`);
  }
  fail(2, `agents-discipline: ${path} has a table header but no unit rows`);
}

// `abandoned` is TERMINAL-but-unsuccessful: the unit cannot be finished, its Evidence block
// carries the reason, and the ledger reports a required handoff rather than completion. The
// template already defined the word; the checker did not know it, so an `abandoned` row landed
// in `other` and the ledger read "indefinitely incomplete" -- the right verdict with the wrong
// claim behind it. Kept OUT of `unverified` because those two say opposite things: one is "still
// coming", the other "never coming", and an abandoned row printed under "unverified rows" reads
// as work in flight. Mirrors the gate half (`gate-check` HANDOFF REQUIRED, exit 1).
const counts = { pending: 0, done: 0, verified: 0, abandoned: 0, other: 0 };
const unverified = [];
const abandoned = [];
for (const r of rows) {
  // hasOwnProperty, NOT `counts[r.status] === undefined`: a status cell reading `constructor` or
  // `toString` finds a FUNCTION on the prototype, so the old test was false, `counts[...]++`
  // stored NaN, and `other` never incremented -- the row vanished from every printed total. It
  // was also a silent node-vs-python divergence: a Python dict inherits no such keys, so the
  // port already counted those rows as `other` while the oracle lost them.
  if (!Object.prototype.hasOwnProperty.call(counts, r.status)) counts.other++;
  else counts[r.status]++;
  if (r.status === "abandoned") abandoned.push(r);
  else if (r.status !== "verified") unverified.push(r);
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
    // A runner word counts only when it IS the span -- `pytest`, `make`. The scan this
    // replaced read the whole LINE and split on non-alphanumerics, so "I will go to the
    // shop" was strong evidence: `go` is a runner. One English sentence satisfied the
    // evidence requirement for an entire ledger.
    if (RUNNER_WORDS.has(inner.toLowerCase())) return true;
  }
  // {2,5}, not {1,5}: a one-character extension made "U.S.A." look like a filename. The
  // trade is real and worth naming rather than denying -- `main.c`, `foo.h` and `analysis.R`
  // no longer read as filename-shaped in UNBACKTICKED prose. Cite those in backticks, where
  // the span loop above accepts any path character. The citation pattern is untouched.
  if (/\b[\w./-]+\.[a-z0-9]{2,5}\b/i.test(l)) return true;
  // `of` dropped ("3 of them" is not a measured result), and intervening words allowed,
  // because the natural way to report a real run is "93 tests passed" -- which `\d+\s*`
  // did NOT match, since " tests " is not whitespace. Tightening the cheat without widening
  // this would have silently failed honest unbackticked evidence.
  if (/\b\d+\s+(?:[a-z]+\s+){0,3}(passed|passing|pass|ok)\b/i.test(l)) return true;
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
    // Drop a line that IS a placeholder, not any line that CONTAINS angle brackets. The old
    // test killed real evidence in every language with generics: "changed the signature to
    // Vec<String>, 12 tests passed" and "returns Result<(), Error>" were both discarded --
    // and since the drop takes the whole line, a `**Unit N —**` header goes with it, so the
    // unit's entire block disappears and the row reports UNBACKED with nothing explaining why.
    // Require a letter or digit to survive the strip, rather than enumerating punctuation.
    // The enumerating form needed a patch per character: it listed the ASCII hyphen but not
    // the em-dash this skill's own format uses (`**Unit N —**`), and would have needed
    // another for `·`, `•`, `―`, `>`. Every real evidence line contains an alphanumeric;
    // no all-placeholder line does, whatever glues it together.
    // A header with an UNFILLED placeholder (`**Unit 1 —** <what you ran>`) is deliberately
    // kept: it is a real unit header, so the block exists and the row lands UNBACKED, which
    // is the honest outcome rather than the block vanishing.
    // `\p{L}\p{N}`, not `[a-z0-9]`: the ASCII form dropped evidence written in any script
    // without Latin letters -- "тесты пройдены" and "テスト成功" both vanished, taking their
    // unit block with them. An all-placeholder line still has no letter or digit in ANY
    // script, so nothing is given up.
    if (!/[\p{L}\p{N}]/u.test(l.replace(/<[^>]+>/g, ""))) continue;
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
// Sliced by LINE-INITIAL `**Unit N` headers, not matched by a per-unit regex. The
// regex this replaced could not terminate a block: both its lookaheads
// (`\n\s*\n\*\*Unit` and `\n##\s`) needed text the evidence scan above had already
// removed — blank lines at :121, `##` headings at :122-124 — before joining with a
// single \n. So every block ran to end-of-text, and one real artifact cited under the
// LAST unit backed every invented row above it. That is verbatim the pooling cheat the
// old comment claimed was closed; it never was.
//
// Line-anchored is load-bearing: a substring test for "Unit 1" also matches prose that
// merely mentions a unit (the template's own Evidence how-to did), which reproduces the
// same over-crediting on an unmodified template.
//
// Slicing also removes the `${unit}` RegExp interpolation. That cell is untrusted table
// content: `(a+)+$` used to be compiled and matched on the main thread with no budget,
// while gate-check.mjs keeps a Worker + 250ms sandbox for exactly this hazard.
//
// DELIBERATE BEHAVIOUR CHANGE: the old regex also accepted a bare `#N` block header.
// Only `**Unit N` is recognised now — the one shape templates/DELEGATION.md documents.
// Evidence under no header at all backs no row: unattributed prose cannot corroborate a
// specific unit, so a single pooled sentence can never satisfy a multi-row ledger.
const evidenceBlocks = new Map();
{
  let current = null;
  for (const line of evidenceText.split("\n")) {
    // Case-insensitive: `**unit 1 —**` is the same header a human meant to write, and
    // matching it case-sensitively would silently yield an empty block and report the row
    // UNBACKED for a reason no message names.
    // Dotted ids (`1.2`, `1.2.1`) are captured whole: the Depth Tree numbers leaves that way, and
    // `[0-9]+` alone keyed `**Unit 1.2 —**` as unit `1` -- row `1.2` then read UNBACKED while
    // row `1` was credited with evidence that was never about it.
    const m = /^\*\*unit\s+([0-9]+(?:\.[0-9]+)*)\b/i.exec(line);
    if (m) current = m[1];
    if (current) evidenceBlocks.set(current, (evidenceBlocks.get(current) ?? "") + line + "\n");
  }
}
function evidenceBlockFor(unit) {
  return evidenceBlocks.get(String(unit).trim()) ?? "";
}
function existingArtifactsIn(block, bases) {
  return [...block.matchAll(/`([^`\s]*\/[^`\s]*\.[A-Za-z0-9]{1,6})`/g)]
    .map((m) => m[1].trim())
    .filter((rel) => {
      const cands = nodePath.isAbsolute(rel) ? [rel] : bases.map((b) => nodePath.join(b, rel));
      return cands.some((c) => fs.existsSync(c));
    });
}

// Created-stamp grammar, declared ONCE and ported line-for-line to ledger_check.py's
// CREATED_STAMP_RE, because leaving calendar validity to Date.parse/fromisoformat made parity
// an accident of engine leniency: Date.parse rolls an impossible date over instead of rejecting
// it (`2026-02-30T10:00Z` -> March 2), fromisoformat rejects it outright, and Date.parse alone
// also accepts a non-ES `+0200` offset. Shape: YYYY-MM-DD, `T` or a space, HH:mm, optional
// :ss with optional .fraction, optional Z/+HH:mm/+HHmm. Hour 24 legal ONLY as exactly
// 24:00[:00[.0...]] (next-day midnight) -- checked separately below, never by the shape alone.
// Year must be 1970-2999 inclusive, else the stamp is skipped -- checked separately below,
// never by the shape alone. Two reasons: (1) Python's next-day rollover on 24:00 does
// `dt += timedelta(days=1)`, which raises an uncaught OverflowError once `dt` is already
// datetime.max's day (year 9999); (2) a naive (no-offset) datetime's `.timestamp()` goes
// through the platform C `mktime`/`localtime`, which raises OSError on Windows for years well
// before 1970 or past roughly 3000, while Node's `Date` has no such limit -- so without a
// shared cap the two runtimes would diverge by engine instead of agreeing the stamp is invalid.
// This range also replaces (not stacks on top of) the old bare `year < 100` guard: `\d{4}`
// alone let `year` through as low as 0, where `new Date(0, ...)` (JS: silently maps years 0-99
// to 1900-1999, a documented `Date` legacy footgun) and `datetime(0, ...)` (Python: raises)
// disagree on what a low 4-digit year even means -- 1970-2999 already excludes that whole
// ambiguous range, so a second, narrower guard added nothing.
// The offset sign, hours and minutes are captured directly here (groups 8-10) instead of via a
// second regex re-parsed from a combined offset group -- a stamp that matched this grammar's
// offset shape cannot then fail to match a laxer copy of the same shape, so the second parse
// was always either redundant or (if it ever drifted from this one) a silent divergence.
const CREATED_STAMP_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(?:(Z)|([+-])(\d{2}):?(\d{2}))?$/;

function isLeapYear(y) {
  return y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
}

const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

function daysInMonth(y, m) {
  return m === 2 && isLeapYear(y) ? 29 : DAYS_IN_MONTH[m - 1];
}

// Validates a `Created:` stamp against the declared grammar above and returns the instant it
// denotes, in epoch ms -- or null when the stamp is out of shape or not a real calendar
// date/time. Never falls back to Date.parse on the raw text: that leniency (rolling
// `2026-02-30` into March) is exactly the engine quirk this function exists to remove.
function parseCreatedStamp(raw) {
  const m = CREATED_STAMP_RE.exec(raw);
  if (!m) return null;
  const [, yS, moS, dS, hS, miS, sS, fracS, zFlag, offSign, offHS, offMS] = m;
  const year = Number(yS), month = Number(moS), day = Number(dS);
  const hour = Number(hS), minute = Number(miS), second = sS !== undefined ? Number(sS) : 0;
  // See the grammar comment above CREATED_STAMP_RE: outside 1970-2999 either runtime can
  // raise on a stamp that is otherwise well-formed, so the range is part of the grammar
  // rather than a try/catch bolted around the arithmetic below.
  if (year < 1970 || year > 2999) return null;
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > daysInMonth(year, month)) return null;
  if (minute > 59 || second > 59) return null;
  let nextDay = false;
  if (hour === 24) {
    // Hour 24 denotes next-day midnight and is legal ONLY as exactly 24:00, 24:00:00, or
    // 24:00:00 with an all-zero fraction -- any non-zero minute/second/fraction is out of shape.
    if (minute !== 0) return null;
    if (sS !== undefined && second !== 0) return null;
    if (fracS !== undefined && /[1-9]/.test(fracS)) return null;
    nextDay = true;
  } else if (hour > 23) {
    return null;
  }
  let offsetMinutes = null;
  if (zFlag) {
    offsetMinutes = 0;
  } else if (offSign) {
    const oh = Number(offHS), omin = Number(offMS);
    if (oh > 23 || omin > 59) return null;
    offsetMinutes = (offSign === "-" ? -1 : 1) * (oh * 60 + omin);
  }
  const fracMs = fracS ? Number(`0.${fracS}`) * 1000 : 0;
  const wallHour = nextDay ? 0 : hour;
  const wallDay = day + (nextDay ? 1 : 0);
  if (offsetMinutes === null) {
    // No offset: local time, built from validated components -- never Date.parse on raw text.
    return new Date(year, month - 1, wallDay, wallHour, minute, second).getTime() + fracMs;
  }
  // `Z` or an explicit offset: an absolute instant computed from the UTC components.
  return Date.UTC(year, month - 1, wallDay, wallHour, minute, second) - offsetMinutes * 60000 + fracMs;
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
  // Fractional seconds and a `Z` designator are captured: `new Date().toISOString()` writes
  // both, `date -u +%FT%TZ` writes the `Z` alone; dropping the `Z` made a UTC stamp parse as LOCAL time,
  // so on a machine west of UTC every artifact produced in the first hours read "older than the
  // ledger" and an honest ledger failed.
  // A `Created:` line's mandatory separator (`\s+` after the optional colon) is the anchor: a
  // line that doesn't even have that shape is not a `Created:` line at all, and falls into the
  // "no Created: line" branch below rather than erroring on something that was never meant to
  // be a date. Its Python mirror is CREATED_LINE, a SEPARATE regex with the same anchor written
  // out longhand (Python's `^`/`/m` can't be relied on to agree with JS's, see CREATED's own
  // comment) -- if this line's anchor ever changes, change CREATED_LINE the same commit.
  const createdLineM = text.match(/^Created:?\s+(.*)$/m);
  let createdInstant = null;
  if (createdLineM) {
    const rest = createdLineM[1];
    // The trailing `(?=\s|$)` is load-bearing: without it, `Created: 2099-09-24T10:00+2` captures
    // only `2099-09-24T10:00` and silently drops `+2`, so a malformed stamp is read as a valid
    // LOCAL time instead of being rejected. Stopping at the next whitespace (or end of line)
    // also lets a `Created:` line carry trailing prose after a well-formed stamp.
    const stampM = /^(\d{4}-\d{2}-\d{2}[T ][\d:]+(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)(?=\s|$)/.exec(rest);
    const candidate = stampM ? stampM[1] : rest.trim();
    createdInstant = stampM ? parseCreatedStamp(candidate) : null;
    if (createdInstant === null) {
      // A `Created:` line that is present but not a real, in-grammar stamp used to be silently
      // treated the same as no line at all -- the staleness rule just went quiet. That let a
      // typo'd date defeat the one check that catches stale, re-cited evidence. A line that
      // exists and is wrong is a ledger defect, not a shrug.
      fail(1, `agents-discipline: Created: ${candidate} is not a real date -- fix it or remove the line`);
    }
  } else {
    // stdout, not stderr: this is a successful run's own report (like the summary below), not
    // a failure -- a warning that only showed up on stderr would be invisible to a caller that
    // pipes just stdout, which is exactly the audience this line exists to reach.
    console.log("agents-discipline: no Created: date -- the file-age check was skipped");
  }

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
    else if (createdInstant !== null && st.mtimeMs < createdInstant) staleArtifacts.push(p);
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

// A no-op acceptance is one that exits 0 NO MATTER WHAT, so writing one is an attempt to
// satisfy the re-run without testing anything. The bar is UNCONDITIONAL success, and getting
// that bar wrong is what broke both previous attempts, in opposite directions:
//
//   FALSE POSITIVES. `test -f x`, `[ -f x ]`, `ls dist/*.js` and `cat f` all EXIT 1 when the
//   thing is missing. They are WEAK checks, not tautologies. The original prefix form flagged
//   `test`/`ls`/`cat`, and the whole-command form that replaced it added `[ ... ]` on the
//   claim that it was a "missed cheat" -- it is not. Rejecting these reddens honest ledgers,
//   and a check that reddens honest ledgers is a check someone eventually deletes.
//
//   ESCAPES. `pytest -q || true` exits 0 unconditionally and is the canonical always-pass
//   idiom. NO whole-command regex can see it, because the cheat lives in the OPERATOR, not
//   in any one word -- and `pipefail` does not help, since `||` is not a pipe. Same for a
//   trailing `;`, a `( ... )` wrapper, and a `/bin/` prefix wired to only one verb.
//
// So this is a small parser rather than a bigger regex. Measured truth table in the tests.
const ALWAYS_TRUE =
  /^(?:(?:\/(?:usr\/)?bin\/)?(?:true|echo|printf|pwd|sleep)(?:\s+[^&|;]*)?|:|command\s+true|exit\s+0)$/i;

// The three operators have THREE DIFFERENT status rules, and collapsing any two of them is
// how the previous two versions of this leaked:
//   `;`   only the LAST status survives  -> `false; true` exits 0
//   `||`  ANY always-true link forces 0  -> `true || pytest` AND `pytest || true`
//   `&&`  every link must hold           -> `echo ok && pytest -q` is honest
// A single `split(/&&|;/)` treated `;` as a conjunction, so `pytest -q; true` -- the same
// always-green trick as `|| true`, with more natural punctuation -- ran and certified a row.
// Measured before this fix: `false; true` re-ran and PASSED.
// `||` and `;` are REFUSED rather than parsed. Both exist to decouple a chain's exit status
// from whether its work succeeded -- `pytest -q || true` and `false; true` are green whatever
// the code does -- and an acceptance has no honest need for either. Refusing them deletes the
// entire always-green class by construction, instead of by out-parsing whoever writes the next
// one. `&&` stays legal because it cannot hide a failure: every link must succeed, so
// `cd packages/x && pytest -q` is exactly as strong as `pytest -q`.
//
// This replaced a hand-rolled shell-semantics parser that was wrong FOUR times -- a prefix
// regex, a whole-command regex, a flat `split(/&&|;/)` that read `;` as a conjunction, and an
// anchored paren peel that mangled `(false) ; (true)`. Each fix surfaced the next defect, and
// three of the four were caught only by adversarial review. The lesson is not that the fourth
// parser was finally right; it is that reimplementing shell status semantics was the wrong
// job to take on. USER decision, 2026-09-06.
const CHAIN_OPERATORS = /\|\||;/;
// A lone `&` backgrounds the command before it and discards its status, so `pytest -q & true`
// is as always-green as `pytest -q || true` -- measured: `false & true` re-ran and PASSED.
// The lookarounds keep the honest `&` forms legal: `&&`, and the redirections `2>&1`, `>&2`,
// `&>f`, `|&`.
const BACKGROUND_OPERATOR = /(?<![&<>|])&(?![&>])/;

// Operators only count OUTSIDE quotes. `python3 -c "import sys; sys.exit(0)"` and
// `awk '{print;}' f` are honest commands whose `;` is an ARGUMENT, not a chain -- refusing
// them would be the same false-positive class that already flagged `[ -f x ]` and `test -f x`
// as cheats. Masking the quoted regions (keeping the delimiters, so the shape survives) is
// what tells an operator from a character inside a string.
//
// It also tightens the other direction for free: `echo "$(pytest)"` masks to `echo ""`, which
// ALWAYS_TRUE then recognises -- and it IS always-green, because echo's status is the one that
// survives, never pytest's.
//
// Unbalanced quotes simply do not match, leaving the text as-is, which errs toward treating a
// character as an operator -- the conservative direction for a refusal that a human can read
// and rewrite, rather than a silent pass.
// A single left-to-right scan, because two independent regex passes cannot track quote state.
// The two-pass version (`replace(/'[^']*'/g).replace(/"[^"]*"/g)`) matched an apostrophe
// INSIDE double quotes against a later single quote, and spliced away everything between:
//   echo "it's fine" && grep -q 'x' f   ->   echo "it''x' f
// which DELETES the `&&`, so the remains matched the `echo` branch and an honest command was
// flagged a no-op. Masking must never invent or destroy an operator.
//
// Length-preserving on purpose: each quoted character becomes `x`, so every operator outside
// quotes keeps its position and nothing shifts. An unterminated quote masks to end-of-string,
// which errs toward refusal -- readable, and a human can rewrite it.
//
// KNOWN CEILING: a backslash-escaped quote (`echo "a\"b; c"`) is read as closing the string,
// so the `;` after it looks like a chain and the row is refused. Rare, and it fails toward a
// message rather than a silent pass.
function maskQuoted(s) {
  let out = "";
  let quote = null;
  for (const c of s) {
    if (quote) {
      out += c === quote ? ((quote = null), c) : "x";
    } else if (c === "'" || c === '"') {
      quote = c;
      out += c;
    } else {
      out += c;
    }
  }
  return out;
}

// A no-op acceptance exits 0 no matter what. With `||` and `;` refused upstream, the only
// remaining shape is an `&&` chain, and it is always-green exactly when EVERY link is.
// True when the leading bracket closes only at the very end -- i.e. the pair really does
// enclose the whole string, rather than being the first of several groups.
function wrapsWhole(s, open, close) {
  let depth = 0;
  for (let i = 0; i < s.length; i++) {
    if (s[i] === open) depth++;
    else if (s[i] === close && --depth === 0) return i === s.length - 1;
  }
  return false;
}

// `( true )` and `{ true }` wrap the same command. The balance check stays: I removed it
// once on the rationale that `;` can no longer appear, and that reasoning was wrong --
// `(echo a) && (echo b)` still mangles to `echo a) && (echo b` under an anchored
// `^\(...\)$`. Shared between the whole-string call in isNoopAcceptance and the
// per-fragment call after its `&&` split: a fragment like `(echo ok)` inside a real chain
// is exactly as wrapped as the whole string is, and needs the same balance check for the
// same reason -- see isNoopAcceptance for why skipping the fragment call was a live bug.
function peelWrapping(input) {
  let s = input;
  for (let i = 0; i < 4; i++) {
    const before = s;
    for (const [open, close] of [["(", ")"], ["{", "}"]]) {
      if (s.startsWith(open) && s.endsWith(close) && wrapsWhole(s, open, close)) {
        s = s.slice(1, -1).trim();
      }
    }
    if (s === before) break;
  }
  return s;
}

function isNoopAcceptance(raw) {
  const s = peelWrapping(String(raw).trim());
  if (!s) return false;
  // Judged on the MASKED text: a quoted `&&` is an argument, not a link. Each fragment is
  // peeled AGAIN after the split -- `true && (echo ok)` is exactly as always-green as
  // `true && echo ok`, and ALWAYS_TRUE has no alternative that can match a `(...)`-wrapped
  // fragment directly. Without this second peel the parenthesized form made this function
  // return `false` (not a no-op), fell through to acceptanceCommand() and REAL execution,
  // and really returned 0 -- the same fabricated-pass outcome as if this function had lied
  // outright, just reached through real execution instead of a forged EVIDENCE line.
  // Measured: `true && (echo ok)` re-ran and PASSED before this fix. This stays inside the
  // peel/split-on-`&&` domain the checker already owns; it does not reopen the `;`/`||`
  // status-propagation question the 2026-09-06 refuse-rather-than-parse decision closed --
  // `&&` alone has no propagation ambiguity, every link must succeed.
  return maskQuoted(s).split("&&").map((p) => peelWrapping(p.trim())).filter(Boolean)
    .every((p) => ALWAYS_TRUE.test(p));
}

const reran = [];
const reproFailed = [];
const unreproducible = [];
const unbacked = [];
const rerunSkipped = Boolean(process.env.AGENTS_DISCIPLINE_SKIP_RERUN);
// One budget for the whole re-run, not one per row. An env var rather than a flag because
// argv[2] is the ledger path and every other knob here is already AGENTS_DISCIPLINE_*.
// `?? 600000` alone was wrong: an EMPTY string is not nullish, so `Number("")` is 0 and an
// exported-but-blank var hard-failed the run -- while the sibling SKIP_RERUN treats "" as
// simply off. Two env vars in one file with opposite empty-string semantics is a trap, so
// blank is treated as unset here too. The validation also only runs when a re-run will
// actually happen: a structure-only check has no budget to misconfigure.
const rerunBudgetRaw = (process.env.AGENTS_DISCIPLINE_RERUN_BUDGET_MS ?? "").trim();
const rerunBudgetMs = rerunBudgetRaw === "" ? 600000 : Number(rerunBudgetRaw);
if (!rerunSkipped && (!Number.isFinite(rerunBudgetMs) || rerunBudgetMs < 1)) {
  fail(2, `agents-discipline: AGENTS_DISCIPLINE_RERUN_BUDGET_MS must be a positive number of milliseconds, got ${JSON.stringify(rerunBudgetRaw)}`);
}
const rerunDeadline = Date.now() + rerunBudgetMs;
if (!rerunSkipped) {
  for (const r of rows.filter((x) => x.status === "verified")) {
    // The no-op scan runs over EVERY code span, BEFORE extraction, because `acceptanceCommand`
    // requires an argument — so a bare `true` was not recognised as a command at all and fell
    // through to `unreproducible`, which does not fail. That is the cheapest cheat in the file:
    // one word, and the row passes. Scan first, then extract.
    const spans = (r.acceptance.match(/`[^`]+`/g) ?? []).map((s) => s.slice(1, -1).trim());
    // Refuse the operators before judging the command. `||` and `;` both decouple a chain's
    // exit status from its work, so there is no version of them worth re-running, and saying
    // so is stronger than trying to out-parse the next always-green idiom.
    const chained = spans.find((s) => CHAIN_OPERATORS.test(maskQuoted(s)));
    if (chained) {
      reproFailed.push({
        unit: r.unit, cmd: chained,
        code: "acceptance uses `||` or `;` — those pass whatever the code does; chain with `&&` or put the steps in a script",
      });
      continue;
    }
    const backgrounded = spans.find((s) => BACKGROUND_OPERATOR.test(maskQuoted(s)));
    if (backgrounded) {
      reproFailed.push({
        unit: r.unit, cmd: backgrounded,
        code: "acceptance backgrounds a command with `&` — its status is discarded; drop the `&` or put the steps in a script",
      });
      continue;
    }
    const noop = spans.find((s) => isNoopAcceptance(s));
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
    // The per-row timeout was the ONLY bound: a 40-row ledger could legitimately occupy this
    // process for `rows x 600s` with nothing watching the total. The budget is shared, so a
    // ledger's whole re-run is bounded no matter how many rows it has, and a row that arrives
    // after the budget is gone FAILS -- it is not silently skipped, which would turn an
    // exhausted budget into a free pass for every row after it.
    const remaining = rerunDeadline - Date.now();
    if (remaining <= 0) {
      reproFailed.push({ unit: r.unit, cmd, code: "re-run budget exhausted before this row ran — nothing was verified here" });
      continue;
    }
    let code = null;
    try {
      // `set -o pipefail` is load-bearing, not hygiene. A shell pipeline reports the LAST
      // command's status, so `pytest | tail -1` exits 0 while pytest is failing — caught in
      // this checker's own fixtures, where exactly that string returned exit 0. Without this
      // line the re-run is defeated by one pipe, which is the cheapest cheat in the file.
      // why: same shell, same pipefail, same command string, passed as argv instead of a template
      // string — the publish gate's static scanner flags interpolation into a shell string.
      // stdio "ignore", not "pipe". Measured: an acceptance of `sh -c 'sleep 300 & exit 0'`
      // held this call for its ENTIRE timeout even though the direct child exited at once --
      // execFileSync drains the stdout pipe, and the backgrounded grandchild inherited and
      // held it. Isolated side by side: pipe 10002ms, ignore 7ms. Nothing read this output
      // (only the exit status is used), so the pipe bought nothing and cost a hang. It also
      // retires the maxBuffer failure mode, where a chatty-but-passing command overflowed the
      // default 1 MiB and was reported as a FAILED acceptance, indistinguishable from a real
      // failure.
      execFileSync("/bin/bash", ["-o", "pipefail", "-c", cmd], { cwd: runCwd, stdio: "ignore", timeout: remaining });
      code = 0;
    } catch (err) {
      // Name the two failures that are NOT the acceptance failing: a timeout (the budget ran out
      // mid-command) and a missing /bin/bash (Windows, NixOS). Both used to print `exit 1`,
      // indistinguishable from a real test failure. Verdict unchanged: both still fail the row.
      code = err.code === "ETIMEDOUT"
        ? "timed out — the re-run budget ran out while this acceptance was running"
        : err.code === "ENOENT"
          ? "cannot run /bin/bash — the ledger re-run requires it (see SECURITY.md)"
          : typeof err.status === "number" ? err.status : 1;
    }
    (code === 0 ? reran : reproFailed).push({ unit: r.unit, cmd, code });
  }
}

console.log(`agents-discipline: ${path}`);
console.log(`  units:       ${rows.length}`);
console.log(`  verified:    ${counts.verified}`);
console.log(`  done:        ${counts.done}`);
console.log(`  pending:     ${counts.pending}`);
if (counts.abandoned) console.log(`  abandoned:   ${counts.abandoned}`);
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
  // Say WHICH shape was missing. "none cited" reads as "you cited nothing" even when the
  // ledger cited plenty -- as commands. `node test/run-tests.mjs` yields no citation: the
  // pattern forbids whitespace inside the span, deliberately, because every command names
  // a script that already exists and extracting paths out of commands would make the
  // corroboration trivially satisfiable. The ask is the artifact you PRODUCED.
  console.log("  artifacts:   none cited — no backticked bare path found (a citation must be a path, not a command); evidence is uncorroborated prose (shape checked, truth not)");
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
if (abandoned.length) {
  // Loud, and worded so it cannot be read as progress. The reason lives in the row's Evidence
  // block, deliberately NOT copied here: it is free-form text of unbounded length, and the gate
  // half makes the same choice (`references/dispatch.md:76` -- name the wave, surface the reason
  // in the final handoff).
  //
  // The row IS now checked for having one, and the wording is the careful part. The predicate is
  // `evidenceBlockFor(unit) === ""`, which means "no line-initial `**Unit N —**` block was
  // attributed to this row" -- NOT "no reason was written". Those come apart in shapes a careful
  // author actually uses: one pooled paragraph covering three units blocked on the same cause, a
  // `#1`-style header, an indented header inside a list. In every one of those the reason exists
  // and this check cannot see it. So the line says what was MEASURED. Printing "NO REASON GIVEN"
  // would be the checker making a claim it cannot support, in the ledger's own output, about an
  // author who did the right thing -- a confident wrong accusation is worse than the silent hole
  // it replaces.
  console.log(`  HANDOFF REQUIRED: ${abandoned.length} abandoned unit(s) — terminal and unsuccessful, not completion:`);
  for (const r of abandoned) {
    // A block STARTS with its own `**Unit N —**` header line — the scanner appends that line to
    // the block it opens. So a header with nothing under it yields a NON-EMPTY block, and testing
    // emptiness alone let a bare `**Unit 1 —**` silence this marker at six keystrokes. Strip the
    // marker and test what is left, which is the reason itself. `[ \t]` rather than `\s` because
    // this same expression is ported to Python, whose `\s` excludes U+FEFF where JS's includes it.
    const reason = evidenceBlockFor(r.unit).replace(/^\*\*unit[ \t]+[0-9]+(?:\.[0-9]+)*[ \t]*[-—–:]*[ \t]*\*\*/i, "").trim();
    // "found in a **Unit N** block" and not "no reason given": a pooled paragraph or a `#1`-style
    // header carries a real reason this predicate cannot see, and the ledger's output is read by
    // a human who would take the stronger claim as an accusation.
    const why = reason === "" ? ` — no reason found in a **Unit ${r.unit}** evidence block` : "";
    console.log(`    - #${r.unit} ${r.name}${why}`);
  }
}
if (malformed.length) {
  console.log("  malformed rows:");
  for (const m of malformed) {
    // Say WHY. A row printed back verbatim leaves the author counting pipes by eye, which is
    // exactly the mistake that produced the malformed row.
    console.log(`    - line ${m.line}: ${m.why ?? "malformed"}`);
    console.log(`        ${m.cells.join(" | ")}`);
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

// The re-run can hold this process for the whole budget (600s by default), and the receipt write
// below would otherwise replace the file with the bytes read at START -- a lost update erasing
// any row or evidence the coordinator edited in the meantime. Re-read BEFORE the verdict is
// printed (not just before the write): a `complete`/`INCOMPLETE` line computed from `rows`
// parsed out of the START bytes is a claim about content that may no longer be on disk, and the
// old code signed nothing but still exited 0 for a ledger nobody's re-read actually vouches for.
// Skip the check when the rerun itself was skipped -- that path already signs and verifies
// nothing, so there is nothing for a mid-run edit to invalidate.
//
// A ledger deleted, replaced with something unreadable, or made unreadable mid-run makes
// readStableRegularFile THROW rather than return unequal bytes -- an uncaught throw here would
// crash with a stack trace and no verdict line at all. The bytes on disk are unknown, not equal,
// which is exactly the "changed" case: treat the throw the same as a content mismatch so the
// run still prints VOID, signs nothing, and exits 1.
let ledgerChanged = false;
if (!rerunSkipped) {
  try {
    ledgerChanged = readStableRegularFile(path, { label: "ledger" }) !== text;
  } catch {
    ledgerChanged = true;
  }
}

// Three-way, not two. An abandoned ledger is not "INCOMPLETE" -- that word promises the work is
// still coming. It is terminal: nobody is going to finish those units, and the honest line is the
// handoff. Mirrors `gate-check`, which likewise never prints ALL MET when anything was abandoned.
// A changed ledger outranks both: neither `complete` nor `abandoned` describes bytes that are no
// longer the ones on disk, so the verdict is VOID rather than a stale claim about either.
if (ledgerChanged) {
  console.log("  -> verdict VOID: the ledger changed while it was being checked; re-run the checker.");
} else if (complete) {
  console.log("  -> ledger complete: every unit verified.");
  // `&& !unverified.length` is load-bearing. TERMINAL is a claim about the WHOLE ledger, and one
  // abandoned row among four pending ones does not make the ledger terminal -- somebody is still
  // working it. Without the conjunct this branch outranks every other incomplete reason and prints
  // TERMINAL over work in flight: the SAME over-claim as the `INCOMPLETE`-on-terminal-work defect
  // this whole change fixes, with the sign flipped. Both facts still reach the reader, on separate
  // lines -- the HANDOFF REQUIRED section above is printed unconditionally -- which is exactly
  // gate-check's shape (it prints HANDOFF REQUIRED and UNMET independently, never one instead of
  // the other). An earlier draft appended the unfinished count to the TERMINAL line instead; that
  // carried both facts but still LED with the contested word.
} else if (abandoned.length && !unverified.length) {
  console.log("  -> ledger TERMINAL: HANDOFF REQUIRED — abandoned unit(s) will not be finished.");
} else {
  console.log("  -> ledger INCOMPLETE.");
}

// A run that SKIPPED the re-run did not verify anything, so it must not sign anything.
// Signing it would mint exactly the artifact this receipt exists to make unforgeable: a
// PASS stamp on a ledger nobody executed. Same for a ledger that changed mid-run: the digest
// below is over the START bytes, and signing it would mint a receipt for content nobody's
// re-read just confirmed is still on disk.
if (!rerunSkipped && !ledgerChanged) {
  // PERMISSIONS. writeAtomic (lib/gates.mjs) creates its temp file 0o600 and renames it over
  // the target -- correct for the coordination state it was built for, but a receipt write
  // going through it would silently reset every ledger's mode to owner-only on every run, and
  // would now succeed on a read-only ledger where the truncating write this replaced used to
  // fail and get swallowed by the same catch. lstat + a writability probe BEFORE writing
  // reproduces that old silent-skip for a ledger the user cannot write; a ledger the user CAN
  // write gets its original mode bits restored after the atomic rename, so writeAtomic's
  // internal 0o600 never leaks past this one caller. This guard belongs here, not inside
  // writeAtomic itself, because dispatch state deliberately WANTS the 0o600 reset.
  const target = resolve(path);
  let originalMode = null;
  try {
    originalMode = fs.lstatSync(target).mode & 0o777;
    fs.accessSync(target, fs.constants.W_OK);
  } catch {
    originalMode = null; // unreadable or unwritable -- treated as "cannot write" below
  }
  if (originalMode !== null) {
    try {
      const stamp =
        `<!-- agents-discipline-check: ${new Date().toISOString()} sha256:${digest} -->\n` +
        `<!-- agents-discipline-check: this is a CONTENT BINDING, not a verdict. It records which bytes were ` +
        `checked, never whether they passed. Re-run ledger-check.mjs on this file for a verdict. -->`;
      // Atomic write (write-to-temp + fsync + rename), not a truncating writeFileSync: a crash or
      // concurrent reader between truncate and write must never observe a half-written receipt.
      writeAtomic(target, bodyForHash + stamp + "\n");
      // win32's chmod only toggles the read-only attribute and cannot express POSIX mode bits
      // (group/other/setuid), so restoring `originalMode` there would be a no-op at best and a
      // misleading one at worst -- skip it rather than pretend to restore something it can't.
      if (process.platform !== "win32") fs.chmodSync(target, originalMode);
    } catch {
      // A read-only ledger (already excluded above), or any other write failure past the
      // re-read (disk full, a symlink swapped in), is not a verification failure -- say nothing
      // and keep the verdict already printed.
    }
  }
}
process.exit(!ledgerChanged && complete ? 0 : 1);
