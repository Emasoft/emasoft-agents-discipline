#!/usr/bin/env node
/**
 * Tests for ledger-check.mjs and the SKILL.md frontmatter.
 * Usage: node tests/ledger-tests.mjs
 */
import { execFileSync } from "node:child_process";
import { readFileSync, copyFileSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, dirname, join, basename } from "node:path";
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
  // rerun: true — see the SKIP_RERUN note below. These exercise the half of the
  // checker the skip disables: the no-op scan, acceptanceCommand, evidenceBlockFor,
  // existingArtifactsIn, and the `unbacked` verdict. Until this flag existed NONE of
  // that had ever been executed by a test, which is how the defects below survived.
  // Safe to run because their acceptance cells contain no runnable command at all.
  {
    name: "one unit's artifact does not back the others",
    file: "tests/fixtures/pooled-evidence.md",
    want: 1,
    rerun: true,
    artifacts: ["reports/unit-3-output.txt"],
    // The exit code alone does NOT discriminate here: this fixture also exits 1 if the
    // attribution over-corrects and leaves every row unbacked. These pin the split --
    // units 1 and 2 unbacked, unit 3 backed by the artifact under its own header.
    // The HEADING and the COUNT are what carry the discrimination. `- #1 stats` alone
    // does not: measured against the pre-fix checker it appears there too, just filed
    // under `unreproducible` (which read 3, pooled, with no UNBACKED section at all).
    expect: [
      "UNBACKED verified rows",
      "- #1 stats",
      "- #2 finance",
      "unreproducible: 1 verified row",
    ],
  },
  {
    // Every one of these exits 0 by construction, so each is an attempt to satisfy the
    // re-run without testing anything. #5 (`ls`) was already caught; #1-#4 were not.
    name: "no-op acceptances are rejected",
    file: "tests/fixtures/noop-acceptance.md",
    want: 1,
    rerun: true,
    artifacts: ["reports/noop-1.txt", "reports/noop-2.txt", "reports/noop-3.txt", "reports/noop-4.txt", "reports/noop-5.txt"],
    expect: [
      "#1 $ : -> no-op acceptance",
      "#2 $ exit 0 -> no-op acceptance",
      "#3 $ /bin/true -> no-op acceptance",
      "#4 $ [ -f package.json ] -> no-op acceptance",
      "#5 $ ls -> no-op acceptance",
    ],
  },
  {
    // Positive control for the case above. A no-op scan strict enough to catch `echo ok`
    // must NOT also reject `echo ok && <real verifier>` -- that is a legitimate command,
    // and rejecting it reddens honest ledgers until someone deletes the check.
    name: "echo chained to a real check is not a no-op",
    file: "tests/fixtures/chained-acceptance.md",
    want: 0,
    rerun: true,
    artifacts: ["reports/chained-1.txt"],
    expect: ["re-ran:      1 acceptance command(s), all passed"],
  },
];

for (const c of cases) {
  let code = null;
  const env = { ...process.env };
  // AGENTS_DISCIPLINE_SKIP_RERUN: most fixtures are SYNTHETIC ledgers. Their acceptance cells name
  // commands from projects that do not exist here (`pytest`, `python3 tests/run_tests.py`),
  // so executing them would test the machine, not the checker. The checker announces the
  // skip in its own output, so a real ledger cannot use this quietly.
  // A case may opt IN to the real path with `rerun: true` when its acceptance cells are
  // safe to execute here — that is the only way the re-run half gets any coverage.
  if (c.rerun) delete env.AGENTS_DISCIPLINE_SKIP_RERUN;
  else env.AGENTS_DISCIPLINE_SKIP_RERUN = "1";
  // A rerun case must run against a COPY. ledger-check signs the file it reads
  // (ledger-check.mjs:417), and that write is suppressed only while the skip is set
  // (:412) -- which is why the skipped cases can safely point at tracked fixtures.
  // Without this, running the suite would append a receipt to a tracked fixture and
  // dirty the working tree.
  let target = resolve(root, c.file);
  if (c.rerun) {
    const dir = mkdtempSync(join(tmpdir(), "ledger-rerun-"));
    target = join(dir, basename(c.file));
    copyFileSync(resolve(root, c.file), target);
    // Create the artifacts the fixture cites, INSIDE the temp dir. Without this the
    // citation would still resolve -- `bases` appends process.cwd(), so it would find
    // the real repo copy -- and the case would pass or fail for a reason that has
    // nothing to do with the fixture. Making it self-contained is what keeps the
    // per-row attribution assertion honest when the suite runs from another cwd.
    for (const artifact of c.artifacts ?? []) {
      const path = join(dir, artifact);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, "unit 3 real output\n");
    }
  }
  let out = "";
  try {
    out = execFileSync("node", [checker, target], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], env });
    code = 0;
  } catch (err) {
    code = err.status;
    out = `${err.stdout ?? ""}${err.stderr ?? ""}`;
  }
  report(code === c.want, c.name, `exit ${code}, want ${c.want}`);
  // An exit code is a one-bit answer, and several distinct wrongnesses produce the same
  // bit. A case may pin the actual verdict text when the bit cannot tell them apart.
  for (const want of c.expect ?? []) report(out.includes(want), `${c.name} -> ${want}`);
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
