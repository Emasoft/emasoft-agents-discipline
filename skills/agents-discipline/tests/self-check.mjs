#!/usr/bin/env node
// self-check.mjs : structural checks on the skill's own scripts.
//
//   node tests/self-check.mjs
//
// Lives in a file rather than as an inline `node -e` in GATES.md on purpose: a
// one-liner containing quotes, "^" or "!" is parsed differently by cmd.exe and
// by sh, so an inline check can pass on one platform and report a phantom
// failure on the other. A CHECK line should name a script, not embed one.
//
// Prints "self-check ok (N/N)" on success, which is what the gate matches.

import { readFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPTS = [
  "scripts/gate-check.mjs",
  "scripts/gate-lint.mjs",
  "scripts/dispatch-check.mjs",
  "scripts/lib/gates.mjs",
  "scripts/lib/dispatch.mjs",
  "scripts/lib/check-supervisor.mjs",
  "scripts/lib/process-tree.mjs",
  "scripts/lib/regex-worker.mjs",
  "tests/run-tests.mjs",
  "tests/dispatch-tests.mjs",
  "tests/hardening-tests.mjs",
  "tests/stress-tests.mjs",
  "tests/lint-tests.mjs",
  "tests/contract-tests.mjs",
  "tests/self-check.mjs",
  "scripts/ledger-check.mjs",
  "tests/ledger-tests.mjs",
];

// Git may materialize CRLF on Windows. Structural source checks operate on
// text, so normalize checkout line endings before matching multiline rules.
const read = (p) => readFileSync(join(ROOT, p), "utf8").replace(/\r\n/g, "\n");
const checks = [];
const check = (name, fn) => checks.push({ name, fn });

function planStructureProblems(source) {
  const problems = [];
  const lines = source.split(/\r?\n/);
  const section = (name) => {
    const marker = "## " + name;
    const starts = [];
    for (let i = 0; i < lines.length; i++) if (lines[i].trim() === marker) starts.push(i);
    if (starts.length !== 1) {
      problems.push(marker + " occurs " + starts.length + " times");
      return [];
    }
    let end = lines.length;
    for (let i = starts[0] + 1; i < lines.length; i++) {
      if (/^##\s+/.test(lines[i])) { end = i; break; }
    }
    return lines.slice(starts[0] + 1, end);
  };
  const cells = (line) => {
    const trimmed = line.trim();
    if (!trimmed.startsWith("|") || !trimmed.endsWith("|")) return null;
    return trimmed.slice(1, -1).split("|").map((cell) => cell.trim());
  };

  const table = section("Leaf dispatch table");
  const headerIndex = table.findIndex((line) => /^\s*\|\s*Leaf\s*\|/.test(line));
  const expectedHeader = ["Leaf", "Owns", "Needs", "Tier", "Planned wave", "State"];
  const rows = [];
  if (headerIndex < 0) {
    problems.push("leaf dispatch table has no header");
  } else {
    const header = cells(table[headerIndex]);
    if (JSON.stringify(header) !== JSON.stringify(expectedHeader)) {
      problems.push("leaf dispatch header must be: " + expectedHeader.join(", "));
    }
    const separator = cells(table[headerIndex + 1] || "");
    if (!separator || separator.length !== expectedHeader.length ||
        separator.some((cell) => !/^:?-{3,}:?$/.test(cell))) {
      problems.push("leaf dispatch table has an invalid separator");
    }
    for (let i = headerIndex + 2; i < table.length; i++) {
      const row = cells(table[i]);
      if (!row) {
        if (rows.length && table[i].trim() === "") break;
        continue;
      }
      rows.push(row);
    }
  }

  const states = new Set(["WAITING", "READY", "IN-FLIGHT", "VERIFIED", "ABANDONED"]);
  const tiers = new Set(["mechanical", "judgment"]);
  const byId = new Map();
  for (const row of rows) {
    if (row.length !== expectedHeader.length) {
      problems.push("leaf dispatch row has " + row.length + " cells: " + row.join(" | "));
      continue;
    }
    const [id, owns, needsText, tier, waveText, state] = row;
    if (!/^\d+(?:\.\d+)+$/.test(id)) problems.push("invalid leaf id: " + id);
    if (byId.has(id)) problems.push("duplicate leaf row: " + id);
    if (!owns || owns === "-") problems.push(id + " has no Owns paths");
    if (!tiers.has(tier)) problems.push(id + " has invalid Tier: " + tier);
    if (!/^[1-9]\d*$/.test(waveText)) problems.push(id + " has invalid Planned wave: " + waveText);
    if (!states.has(state)) problems.push(id + " has invalid State: " + state);
    const needs = needsText === "-" ? [] : needsText.split(",").map((value) => value.trim());
    if (needs.some((value) => !/^\d+(?:\.\d+)+$/.test(value))) {
      problems.push(id + " has invalid Needs: " + needsText);
    }
    byId.set(id, { needs, state, wave: Number(waveText) });
  }
  if (!rows.length) problems.push("leaf dispatch table has no rows");

  for (const [id, row] of byId) {
    let blocked = false;
    for (const dependency of row.needs) {
      const target = byId.get(dependency);
      if (!target) {
        problems.push(id + " needs unknown leaf " + dependency);
        blocked = true;
        continue;
      }
      if (dependency === id) problems.push(id + " depends on itself");
      if (target.wave >= row.wave) problems.push(id + " is not planned after " + dependency);
      if (target.state !== "VERIFIED") blocked = true;
    }
    if (row.state === "WAITING" && !blocked) problems.push(id + " is WAITING without a blocked dependency");
    if (["READY", "IN-FLIGHT", "VERIFIED"].includes(row.state) && blocked) {
      problems.push(id + " is " + row.state + " with a blocked dependency");
    }
  }

  const tree = section("Tree");
  const topologyLeaves = [];
  for (const line of tree) {
    if (!/^\s*-\s+\d/.test(line)) continue;
    if (/\b(?:Owns|Needs|Tier|Wave|State)\b/i.test(line)) {
      problems.push("tree repeats an operational field: " + line.trim());
    }
    const match = line.match(/^\s*-\s+(\d+(?:\.\d+)+)\s+<leaf>(?:\s|$)/);
    if (match) topologyLeaves.push(match[1]);
  }
  const tableLeaves = [...byId.keys()].sort();
  const treeLeaves = [...topologyLeaves].sort();
  if (new Set(topologyLeaves).size !== topologyLeaves.length) problems.push("tree repeats a leaf id");
  if (JSON.stringify(treeLeaves) !== JSON.stringify(tableLeaves)) {
    problems.push("tree/table leaf sets differ: tree=" + treeLeaves.join(",") + " table=" + tableLeaves.join(","));
  }

  const scheduleHeadings = lines.filter((line) => line.trim() === "## Dispatch schedule").length;
  if (scheduleHeadings) problems.push("separate dispatch schedule is forbidden");
  if (lines.some((line) => /^\s*-\s*Wave\s+\d+/i.test(line))) {
    problems.push("wave list duplicates Planned wave");
  }
  return problems;
}

function releasePrecedesPromotion(section, releaseToken, promoteToken) {
  const release = section.indexOf(releaseToken);
  const promote = section.indexOf(promoteToken);
  return release !== -1 && promote !== -1 && release < promote;
}

check("zero non-stdlib imports", () => {
  const bad = [];
  for (const p of SCRIPTS) {
    for (const m of read(p).matchAll(/^\s*import\s[^;]*?from\s+["']([^"']+)["']/gm)) {
      const spec = m[1];
      if (!spec.startsWith("node:") && !spec.startsWith(".")) bad.push(p + " -> " + spec);
    }
  }
  return bad.length ? "non-stdlib import: " + bad.join(", ") : null;
});

check("one shared gate parser", () => {
  // The v2.0 checker and hook each had their own GATE_RE and disagreed about a
  // gate's id when it carried no "Gn:" prefix. Parsing now lives only in the lib.
  // This file names GATE_RE to describe the rule, so it scans only scripts/.
  const owners = SCRIPTS.filter(p => p.startsWith("scripts/") && read(p).includes("GATE_RE"));
  return owners.length === 1 && owners[0] === "scripts/lib/gates.mjs"
    ? null
    : "GATE_RE should exist only in scripts/lib/gates.mjs, found in: " + owners.join(", ");
});

check("no index-arithmetic argument filtering", () => {
  // The v2.0 arg filter dropped index tIdx+1, which is 0 when --timeout is
  // absent, silently discarding the first file argument.
  return read("scripts/gate-check.mjs").includes("i !== tIdx + 1")
    ? "gate-check.mjs still filters arguments by index arithmetic"
    : null;
});

check("gate files are written atomically", () => {
  const src = read("scripts/gate-check.mjs");
  if (!src.includes("writeAtomic")) return "gate-check.mjs does not write via writeAtomic";
  if (!src.includes("withFileLock")) return "gate-check.mjs does not take a lock before writing";
  return null;
});

check("checks wait for close and cap output", () => {
  const src = read("scripts/gate-check.mjs");
  if (!src.includes('child.once("close"')) return "gate runner does not settle on stdio close";
  if (src.includes('child.once("exit"')) return "gate runner settles on exit before stdio close";
  if (!src.includes("MAX_OUTPUT_BYTES")) return "gate runner has no explicit output cap";
  return null;
});

check("approval identity binds execution semantics", () => {
  const src = read("scripts/gate-check.mjs");
  const required = [
    "check:", "expect:", "cwd", "shell", "timeoutMs", "maxOutputBytes",
    "regexTimeoutMs", "regexStartupTimeoutMs", "maxRegexWorkers", "platform", "path:",
  ];
  const missing = required.filter(token => !src.includes(token));
  return missing.length ? "approval oracle missing source tokens: " + missing.join(", ") : null;
});

check("every local resource the skill names exists", () => {
  const skill = read("SKILL.md");
  const missing = [];
  const named = new Set();
  for (const m of skill.matchAll(/`((?:references|templates|scripts)\/[^`\s]+|SECURITY\.md)`|\]\(((?:references|templates|scripts)\/[^)\s]+|SECURITY\.md)\)/g)) named.add(m[1] || m[2]);
  for (const path of named) {
    try { read(path); } catch { missing.push(path); }
  }
  return missing.length ? "SKILL.md names missing local resources: " + missing.join(", ") : null;
});

check("all executable sources retain the Node 16 floor", () => {
  const bad = SCRIPTS.filter(p => /Node 1[89]\+/.test(read(p)));
  return bad.length ? "newer runtime claim in: " + bad.join(", ") : null;
});

check("the PLAN template carries a revisioned contract denominator", () => {
  const plan = read("templates/PLAN.md");
  const required = [
    "Current contract inventory", "Contract revision", "Required outcome or constraint",
    "Owner", "Observing gate or manual review", "Disposition", "REMOVED_BY_USER",
  ];
  const missing = required.filter((token) => !plan.includes(token));
  return missing.length ? "PLAN contract inventory missing: " + missing.join(", ") : null;
});

check("the PLAN dispatch table is the single operational authority", () => {
  const problems = planStructureProblems(read("templates/PLAN.md"));
  return problems.length ? problems.join("; ") : null;
});

check("the PLAN structural check rejects contradictory representations", () => {
  const plan = read("templates/PLAN.md");
  const controls = [
    ["blocked leaf marked READY", plan.replace(
      "| 1.2.2 | src/<d>/**, tests/<d>/** | 1.2.1 | judgment | 2 | WAITING |",
      "| 1.2.2 | src/<d>/**, tests/<d>/** | 1.2.1 | judgment | 2 | READY |")],
    ["dependency in the same wave", plan.replace(
      "| 1.2.2 | src/<d>/**, tests/<d>/** | 1.2.1 | judgment | 2 | WAITING |",
      "| 1.2.2 | src/<d>/**, tests/<d>/** | 1.2.1 | judgment | 1 | WAITING |")],
    ["unknown dependency", plan.replace(
      "| 1.2.2 | src/<d>/**, tests/<d>/** | 1.2.1 | judgment | 2 | WAITING |",
      "| 1.2.2 | src/<d>/**, tests/<d>/** | 9.9.9 | judgment | 2 | WAITING |")],
    ["model name used as Tier", plan.replace(
      "| 1.1.2 | src/<b>/**, tests/<b>/** | - | judgment | 1 | READY |",
      "| 1.1.2 | src/<b>/**, tests/<b>/** | - | model-x | 1 | READY |")],
    ["duplicate table row", plan.replace(
      "| 1.1.2 | src/<b>/**, tests/<b>/** | - | judgment | 1 | READY |",
      "| 1.1.2 | src/<b>/**, tests/<b>/** | - | judgment | 1 | READY |\n" +
      "| 1.1.2 | src/<b>/**, tests/<b>/** | - | judgment | 1 | READY |")],
    ["operational state copied into the tree", plan.replace(
      "- 1.1.1 <leaf> ...... gates/leaf-1.1.1.md",
      "- 1.1.1 <leaf> ...... gates/leaf-1.1.1.md ...... State: READY")],
    ["separate schedule added", plan.replace(
      "## Status log",
      "## Dispatch schedule\n\n- Wave 1: 1.1.1\n\n## Status log")],
  ];
  for (const [name, control] of controls) {
    if (control === plan) return "negative-control fixture no longer matches: " + name;
    if (!planStructureProblems(control).length) return "negative control passed: " + name;
  }
  return null;
});

check("leaf release precedes dependent promotion everywhere", () => {
  const orchestration = read("references/orchestration.md");
  const stepStart = orchestration.indexOf("6. **Append status and roll forward.**");
  const stepEnd = orchestration.indexOf("7. **Integrate bottom-up.**", stepStart);
  const loopStart = orchestration.indexOf("while an unverified leaf remains:");
  const loopEnd = orchestration.indexOf("```", loopStart);
  const sections = [
    ["driver step 6", orchestration.slice(stepStart, stepEnd),
      "--leaf leaf-1.2.1 --release", "Only then promote"],
    ["rolling dispatch loop", orchestration.slice(loopStart, loopEnd),
      "release that exact leaf lease and record the release", "promote each WAITING leaf"],
  ];
  for (const [name, section, releaseToken, promoteToken] of sections) {
    if (!releasePrecedesPromotion(section, releaseToken, promoteToken)) {
      return name + " does not release the exact leaf before promotion";
    }
    const omitted = section.replace(releaseToken, "record the leaf result");
    if (releasePrecedesPromotion(omitted, releaseToken, promoteToken)) {
      return name + " missing-release negative control passed";
    }
  }
  if (releasePrecedesPromotion("promote, then release", "release", "promote")) {
    return "swapped-order negative control passed";
  }
  const parallel = read("references/parallel.md");
  if (!parallel.includes("Release the whole scope only after every leaf has settled")) {
    return "parallel guide does not reserve whole-scope release for final cleanup";
  }
  const plan = read("templates/PLAN.md");
  if (!plan.includes("Do not promote a dependent until that exact\nrelease is recorded")) {
    return "PLAN template does not gate promotion on the recorded exact release";
  }
  const skill = read("SKILL.md");
  if (!skill.includes("parent-verified leaf's exact lease has been released")) {
    return "SKILL.md rolling rule omits exact release";
  }
  return null;
});

check("request reconciliation keeps the focused solo cheap path", () => {
  const skill = read("SKILL.md");
  if (!skill.toLowerCase().includes("re-read the current request")) return "SKILL.md lacks final request reread";
  if (!skill.includes("a PLAN table is not required")) return "focused solo path was made needlessly orchestrated";
  if (!read("references/orchestration.md").includes("review every current contract row")) {
    return "orchestration guide lacks final contract reconciliation";
  }
  return null;
});

check("abandonment is terminal handoff rather than ALL MET", () => {
  const checker = read("scripts/gate-check.mjs");
  if (!checker.includes("HANDOFF REQUIRED") || !checker.includes("totalAbandoned === 0")) {
    return "gate checker can still classify abandonment as completion";
  }
  return null;
});

check("every reference and template is linked from an entry document", () => {
  // An orphaned file is worse than a missing one: it exists, so it reads as
  // covered ground, but nothing points at it so the model never loads it and
  // its rules silently stop applying. The merge left three references that way.
  // Requiring a link from SKILL.md or SECURITY.md (rather than from any file at
  // all) keeps a file that only some other orphan links from counting as
  // reachable. This is the file->doc direction; the dangling-link check above
  // covers doc->file, and neither alone catches both.
  //
  // The closing paren is load-bearing: it matches a link TARGET, not a bare
  // mention. Dropping it lets the label half of "[dir/x.md](...)" count as its
  // own link, so a broken target goes unnoticed. The optional "./" accepts the
  // other spelling of the same target. The filename is fully regex-escaped: it
  // comes from readdir, so an unescaped "+" or "(" in a future name would
  // silently widen the match or throw.
  const entry = ["SKILL.md", "SECURITY.md"].map(read).join("\n");
  const orphans = [];
  for (const dir of ["references", "templates"]) {
    for (const f of readdirSync(join(ROOT, dir))) {
      if (!f.endsWith(".md")) continue;
      const esc = f.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      if (!new RegExp("\\((?:\\./)?" + dir + "/" + esc + "\\)").test(entry)) orphans.push(dir + "/" + f);
    }
  }
  return orphans.length ? "unlinked file(s): " + orphans.join(", ") : null;
});

// The merge's whole promise is that the completion half reaches the LEAF. Until this check
// existed, `templates/worker-brief.md` contained the word "gate" ZERO times: SKILL.md told
// the coordinator to hand each worker a gate ledger, and the template that actually goes to
// the worker never mentioned one. That is the gap this skill was created to close, and
// nothing detected it. These are cheap structural assertions, not behaviour tests -- their
// job is to make the two halves fail loudly when they drift apart again.
check("worker brief carries the completion half", () => {
  const brief = read("templates/worker-brief.md");
  const missing = [];
  if (!/^## Gates$/m.test(brief)) missing.push("a `## Gates` section");
  if (!/CHECK/.test(brief) || !/EXPECT/.test(brief)) missing.push("CHECK/EXPECT written out inline");
  if (!/timeout/i.test(brief)) missing.push("a timeout wrapper (gate-check's bounds do not reach a raw shell)");
  if (!/--status/.test(brief)) missing.push("the read-only `--status` self-validation");
  if (!/^## You are a leaf$/m.test(brief)) missing.push("the `## You are a leaf` no-delegation guard");
  if (!/four passes/i.test(brief)) missing.push("the four-pass rule");
  if (!/AGENTS_DISCIPLINE_SKIP_RERUN/.test(brief)) missing.push("the env-hygiene list");
  return missing.length ? "worker-brief.md is missing " + missing.join("; ") : null;
});

check("the no-delegation guard is in Rule zero, not only the template", () => {
  const skill = read("SKILL.md");
  // Scoped to Rule zero, and matching the GUARD rather than the word "leaf". My first version
  // tested `/leaf/i.test(skill)`, which is trivially true -- SKILL.md says "leaf ledger",
  // "leaf self-check", "leaf-1.md" -- so it would have passed with the guard deleted outright.
  // That is the self-confirming-predicate failure this suite exists to catch.
  const parts = skill.split(/^### Rule zero[^\n]*$/m);
  // Distinguish "section renamed" from "guard deleted". Reporting the latter for the former
  // sends someone hunting for a guard that is still there -- the same lie as `40bd2c4`'s
  // "no unit rows" on a file that visibly had rows.
  if (parts.length < 2) return "SKILL.md has no `### Rule zero` heading — this check needs rescoping, the guard may be fine";
  const ruleZero = parts[1].split(/^### /m)[0];
  // Matches the GUARD's subject, not an incidental word. An earlier version required the
  // literal "CLOSED", which punishes a correct rewording ("the gate does not open").
  return /brief[^\n]*\bleaf\b/i.test(ruleZero)
    ? null
    : "SKILL.md's Rule zero does not close the gate for a worker whose brief says it is a leaf";
});

check("approval is named where --reverify is prescribed", () => {
  const skill = read("SKILL.md");
  // Bounded to the section. Splitting on the heading alone takes everything to END OF FILE,
  // so an `--approve` anywhere later in the document would have satisfied this while
  // `## Run both` itself lacked one.
  const runBoth = (skill.split(/^## Run both$/m)[1] ?? "").split(/^## /m)[0];
  // Without --approve, gate-check takes the unapproved branch, runs ZERO commands and reports
  // the gate unmet. A coordinator following this section verbatim would see a false failure.
  return /--reverify/.test(runBoth) && !/--approve/.test(runBoth)
    ? "`## Run both` prescribes --reverify without the --approve step, so it would run nothing"
    : null;
});

check("the brief prescribes no non-portable command", () => {
  const brief = read("templates/worker-brief.md");
  // Every bounding tool the brief can name is non-portable somewhere, and I shipped TWO of
  // them uncaveated in successive commits: `timeout` (GNU coreutils, absent on stock macOS)
  // and `pytest --timeout=` (needs the pytest-timeout plugin; measured `error: unrecognized
  // arguments` on a stock install). So each must appear only alongside its caveat.
  //
  // The backtick anchors matter. An earlier version tested `/timeout /`, which also matches
  // `go test -timeout 120s` -- core Go, needing no caveat -- so it would have demanded one;
  // and it paired that with a DOCUMENT-level `!/gtimeout/`, satisfied by any mention anywhere,
  // so a new uncaveated instruction elsewhere would still have passed.
  const problems = [];
  if (/`timeout /.test(brief) && !/coreutils/.test(brief)) problems.push("`timeout` without the coreutils/macOS caveat");
  if (/--timeout=/.test(brief) && !/pytest-timeout/.test(brief)) problems.push("`--timeout=` without naming the pytest-timeout plugin");
  return problems.length ? "the brief prescribes " + problems.join("; ") : null;
});

let passed = 0;
const failures = [];
for (const c of checks) {
  let problem;
  try { problem = c.fn(); } catch (e) { problem = e.message; }
  if (problem) { failures.push(c.name + ": " + problem); console.log("FAIL " + c.name + "\n     " + problem); }
  else { passed++; console.log("ok   " + c.name); }
}

console.log("");
if (failures.length) {
  console.log("self-check FAILED (" + passed + "/" + checks.length + ")");
  process.exit(1);
}
console.log("self-check ok (" + passed + "/" + checks.length + ")");
