#!/usr/bin/env node
/**
 * Tests for ledger-check.mjs and the SKILL.md frontmatter.
 * Usage: node tests/ledger-tests.mjs
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const checker = resolve(root, "scripts/ledger-check.mjs");

let failed = 0;
function report(ok, name, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
  if (!ok) failed++;
}

const cases = [
  { name: "template (incomplete)", file: "templates/DELEGATION.md", want: 1 },
  { name: "partial ledger", file: "tests/fixtures/partial.md", want: 1 },
  { name: "complete ledger", file: "tests/fixtures/complete.md", want: 0 },
  { name: "not a ledger", file: "SECURITY.md", want: 2 },
  { name: "junk evidence rejected", file: "tests/fixtures/evidence-junk.md", want: 1 },
  { name: "real evidence accepted", file: "tests/fixtures/evidence-real.md", want: 0 },
  { name: "reworded rules still pass", file: "tests/fixtures/rules-reworded.md", want: 0 },
  { name: "truncated row fails loud", file: "tests/fixtures/truncated-row.md", want: 1 },
  { name: "aligned separators + piped cell", file: "tests/fixtures/aligned-and-piped.md", want: 0 },
  { name: "'verified' word is not evidence", file: "tests/fixtures/evidence-word-verified.md", want: 1 },
  { name: "backticked 'done' is not evidence", file: "tests/fixtures/evidence-backtick-done.md", want: 1 },
  { name: "vague phrase is not evidence", file: "tests/fixtures/evidence-vague-phrase.md", want: 1 },
];

for (const c of cases) {
  let code = null;
  try {
    // AGENTS_DISCIPLINE_SKIP_RERUN: these fixtures are SYNTHETIC ledgers. Their acceptance cells name
    // commands from projects that do not exist here (`pytest`, `python3 tests/run_tests.py`),
    // so executing them would test the machine, not the checker. The checker announces the
    // skip in its own output, so a real ledger cannot use this quietly.
    execFileSync("node", [checker, resolve(root, c.file)], {
      stdio: "ignore",
      env: { ...process.env, AGENTS_DISCIPLINE_SKIP_RERUN: "1" },
    });
    code = 0;
  } catch (err) {
    code = err.status;
  }
  report(code === c.want, c.name, `exit ${code}, want ${c.want}`);
}

// Frontmatter regression guard: the skills CLI parses the description as YAML,
// and an unquoted `word: word` inside it breaks `npx skills add` (the README's
// headline install). This is the bug that shipped once; keep it from returning.
// The merged description carries both halves' triggers; 800 characters is about
// the 200-token limit the plugin validator enforces.
const skill = readFileSync(resolve(root, "SKILL.md"), "utf8");
const fm = skill.split(/^---\s*$/m)[1] ?? "";
const descLine = fm.split("\n").find((l) => /^description:/.test(l)) ?? "";
const desc = descLine.replace(/^description:\s*/, "").trim();
const stripped = desc.replace(/^['"]|['"]$/g, "");

report(desc.length > 0, "frontmatter: description present");
report(desc.startsWith("'") && desc.endsWith("'"), "frontmatter: description is single-quoted (YAML-safe)");
report(stripped.length <= 800, "frontmatter: description length", `${stripped.length} chars`);
for (const tok of ["/agents-discipline", "delegate", "fan out", "ledger", "subagent"]) {
  report(stripped.includes(tok), `frontmatter: trigger '${tok}' present`);
}
report(
  stripped.includes("DELEGATION.md") && stripped.includes("partial ledger"),
  "frontmatter: method summary intact",
  "ledger + verify + no-done"
);

console.log(failed ? `${failed} failing` : "all pass");
process.exit(failed ? 1 : 0);
