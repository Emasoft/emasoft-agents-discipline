#!/usr/bin/env node
/**
 * Tests for ledger-check.mjs and the SKILL.md frontmatter.
 * Usage: node tests/ledger-tests.mjs
 */
import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
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
    // Every one of these exits 0 UNCONDITIONALLY, which is the actual bar. #4 is the one
    // that matters most: `pytest -q || true` is the canonical always-pass idiom, it has a
    // real left half, and no whole-command regex can see it because the cheat lives in the
    // operator. `pipefail` does not help either -- `||` is not a pipe.
    name: "no-op acceptances are rejected",
    file: "tests/fixtures/noop-acceptance.md",
    want: 1,
    rerun: true,
    artifacts: ["reports/noop-1.txt", "reports/noop-2.txt", "reports/noop-3.txt", "reports/noop-4.txt", "reports/noop-5.txt", "reports/noop-6.txt", "reports/noop-7.txt", "reports/noop-8.txt", "reports/noop-9.txt"],
    // Two distinct verdicts, and the split matters. Rows 1-3 and 5 are single commands that
    // happen to exit 0 always, so they are judged. Rows 4 and 6-9 carry `||` or `;` and are
    // REFUSED before judgement -- which is the point of the USER's design decision: every one
    // of those four was a defect that shipped, was found by adversarial review, and was fixed
    // by a parser that then leaked somewhere else. Refusing the operators retires the class.
    expect: [
      "#1 $ : -> no-op acceptance",
      "#2 $ exit 0 -> no-op acceptance",
      "#3 $ /bin/true -> no-op acceptance",
      "#5 $ ( exit 0 ) -> no-op acceptance",
      "#4 $ pytest -q || true -> acceptance uses `||` or `;`",
      "#6 $ true || pytest -q -> acceptance uses `||` or `;`",
      "#7 $ false; true -> acceptance uses `||` or `;`",
      "#8 $ ;true -> acceptance uses `||` or `;`",
      "#9 $ (false) ; (true) -> acceptance uses `||` or `;`",
    ],
  },
  {
    // The other half of the same predicate, and the reason it is a parser rather than a
    // word list. `test -f x`, `[ -f x ]` and `ls x` all EXIT 1 when the thing is missing --
    // weak checks, not tautologies. Two successive versions of this scan flagged them, the
    // second one commit after the first was justified with "a check that reddens honest
    // ledgers is a check someone eventually deletes". Paths are absolute so the verdict does
    // not depend on where the runner resolved its working directory.
    name: "weak-but-real checks are not no-ops",
    file: "tests/fixtures/weak-but-real-acceptance.md",
    want: 0,
    rerun: true,
    // Row 4 pins the half of the USER's decision that is easy to over-apply: `&&` stays
    // LEGAL. It cannot hide a failure (every link must succeed), and forbidding it would
    // also forbid `cd packages/x && pytest -q`, which is honest and common. If someone
    // later widens CHAIN_OPERATORS to include `&&`, this row fails.
    // Rows 5 and 6 are the quote guard. `python3 -c "import sys; sys.exit(0)"` is the
    // canonical checkable one-liner in a Python repo and `awk '{print;}' f` is an ordinary
    // program body -- their `;` is an ARGUMENT, not a chain. The first version of the
    // refusal tested the raw span and rejected both, with advice ("chain with `&&`") that
    // does not even apply. Same false-positive class as flagging `[ -f x ]`.
    artifacts: ["reports/weak-1.txt", "reports/weak-2.txt", "reports/weak-3.txt", "reports/weak-4.txt", "reports/weak-5.txt", "reports/weak-6.txt"],
    // `reject`, not `expect`: the property under test is an ABSENCE -- none of these three
    // is flagged a no-op. Asserting a re-ran COUNT instead would test something else and
    // did: `[ -f x ]` is not extracted as a runnable command at all (a separate, pre-existing
    // limitation of acceptanceCommand), so only two of the three ever execute.
    expect: ["re-ran:      5 acceptance command(s), all passed"],
    reject: ["no-op acceptance", "acceptance uses"],
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
  {
    // Ordinary English must not satisfy the evidence bar. Three separate holes, one
    // fixture: `go` is a RUNNER_WORD and the scan read the whole LINE, `3 of them` hit
    // the measured-result pattern via its `of` alternative, and `U.S.A.` looked like a
    // filename to the extension pattern. Conjunctive on purpose -- evidenceOk is
    // ledger-wide, so if ANY of the three still passes this assertion fails.
    name: "ordinary English is not evidence",
    file: "tests/fixtures/evidence-english.md",
    want: 1,
    expect: ["evidence:    MISSING"],
  },
  {
    // The cheapest fabrication available: copy the template, flip every status to
    // `verified`, add nothing. It must not pass. An earlier template shipped Evidence
    // boilerplate containing a backticked span with whitespace, which scored as STRONG
    // evidence all by itself -- so the copy-and-flip ledger reported `evidence: present`.
    // Derived from the live template, not a snapshot, so a future template edit that
    // reintroduces satisfying boilerplate fails here instead of shipping.
    name: "copy the template and flip every status: still fails",
    file: "templates/DELEGATION.md",
    mutate: (t) => t.replace(/\| pending \|/g, "| verified |"),
    want: 1,
    // Needed for the UNBACKED assertion -- that verdict lives inside the re-run block.
    // Safe: every Acceptance cell is the placeholder `<command>`, which has no whitespace,
    // so acceptanceCommand extracts nothing and nothing is executed.
    rerun: true,
    expect: ["evidence:    MISSING", "UNBACKED verified rows"],
  },
  {
    // T13. The placeholder filter dropped any line CONTAINING angle brackets, so real
    // evidence in every language with generics vanished -- header and all, since the drop
    // takes the whole line. The row then reported UNBACKED with nothing explaining why.
    // `evidence: present` is the discriminator: pre-fix both blocks were gone entirely.
    // SKIP_RERUN is required, not incidental: the acceptances name `cargo`, which most
    // machines lack. What is under test is the EVIDENCE scan, not execution.
    name: "generics in evidence are not mistaken for placeholders",
    file: "tests/fixtures/evidence-generics.md",
    want: 0,
    expect: ["evidence:    present", "verified:    2"],
    reject: ["evidence:    MISSING"],
  },
  {
    // A CHARACTERIZATION test, not a regression guard, and the distinction is measured:
    // pre-fix and post-fix both report `evidence: MISSING` here. An all-placeholder line
    // scores weak whether it is dropped or kept, so `strong === 0` either way -- the
    // dropping only changes what lands in evidenceText for citation extraction and block
    // slicing, and a placeholder line contributes nothing to either. I first shipped this
    // claiming it guarded the dash class; it does not, and a test that passes identically
    // against broken and fixed code is the thing this suite exists to avoid.
    // It still earns its place: it pins that unfilled boilerplate never satisfies evidence.
    name: "a line of pure placeholders is not evidence",
    file: "tests/fixtures/evidence-placeholders.md",
    want: 1,
    expect: ["evidence:    MISSING"],
  },
  {
    // A trailing `|` is OPTIONAL in GitHub-flavoured markdown. Dropping the last field
    // unconditionally made such a header count one column short, which made EVERY row
    // malformed, emptied `rows`, and tripped the "no unit rows" bail -- so a ledger that
    // parsed cleanly before the column check existed became unreadable, and the reason was
    // computed and then discarded. Measured: pre-B3 this fixture was `ledger complete`.
    name: "a table written without trailing pipes still parses",
    file: "tests/fixtures/no-trailing-pipe.md",
    want: 0,
    expect: ["verified:    2"],
    reject: ["malformed"],
  },
  {
    // T6. An UNESCAPED pipe inside an Acceptance command adds a column. The only shape check
    // used to be `cells.length < 6`, which a 7-cell row passes -- so `Status` was read from
    // `cells[5]`, the wrong cell, and the row parsed silently as whatever the shift produced.
    name: "an unescaped pipe is malformed, not silently shifted",
    file: "tests/fixtures/column-shift.md",
    want: 1,
    expect: ["7 cells but the header declares 6", "escape a literal pipe"],
  },
  {
    // T7. The mirror: widening the header leaves an old row short, and every added column
    // read as `undefined` forever with no error. The two causes need opposite fixes, so the
    // message must name the direction -- asserting that here is what keeps them apart.
    name: "a row missing the header's new columns is malformed",
    file: "tests/fixtures/short-row.md",
    want: 1,
    expect: ["6 cells but the header declares 8", "missing 2 trailing cell(s)"],
    reject: ["escape a literal pipe"],
  },
  {
    // T8. Regression guard for the widening itself: `Gates` and `Attempts` are APPENDED after
    // `Status` precisely so `cells[0..5]` keep their meaning. Inserting either before `Status`
    // would shift it to `cells[7]` and every row would parse its gate path as its status.
    name: "a widened 8-column ledger still reads status from cells[5]",
    file: "tests/fixtures/widened-columns.md",
    want: 0,
    expect: ["verified:    2"],
    reject: ["malformed"],
  },
  {
    // The read used to be a plain readFileSync: no size cap, no regular-file assertion,
    // no O_NOFOLLOW, while every other reader here goes through readStableRegularFile.
    // Measured against the pre-fix script, this argument HUNG the process indefinitely
    // (timeout exit 124); it now returns in ~50ms. An unbounded wait in the one script a
    // coordinator is told to run on a ledger it may not have written.
    name: "a FIFO ledger path is rejected, not waited on",
    fifo: true,
    want: 2,
    expect: ["must be one unchanged regular single-link file"],
  },
  {
    // Same helper, the other bound: refuse before parsing rather than reading 9 MiB into
    // memory to discover it is not a ledger.
    // The exit code does NOT discriminate here -- measured, the pre-fix script also exits
    // 2 on this input, just after reading the whole thing and finding no table header. The
    // size message is the only evidence that the bound was the thing that stopped it.
    name: "an oversized ledger is rejected before parsing",
    oversized: true,
    want: 2,
    expect: ["exceeds 8388608 bytes"],
  },
  {
    // The re-run had ONE bound, per row, so a 40-row ledger could occupy the process for
    // `rows x 600s` with nothing watching the total. The budget is now shared. A row that
    // arrives after it is gone must FAIL -- silently skipping it would turn an exhausted
    // budget into a free pass for every row after the one that ate it.
    // Deterministic, and it takes TWO rows to reach the branch. With a 1ms budget the first
    // row still sees `remaining > 0` -- clock granularity -- so it dies by its own timeout,
    // which is an ordinary failure and proves nothing about the budget. Only by row 2 has
    // the clock certainly advanced past the deadline. Nothing sleeps, so the suite stays
    // fast and leaves no process behind.
    name: "an exhausted re-run budget fails the row, never skips it",
    file: "tests/fixtures/budget-two-rows.md",
    rerun: true,
    artifacts: ["reports/budget-1.txt", "reports/budget-2.txt"],
    envExtra: { AGENTS_DISCIPLINE_RERUN_BUDGET_MS: "1" },
    want: 1,
    expect: ["ACCEPTANCE DID NOT REPRODUCE", "budget exhausted before this row ran"],
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
  Object.assign(env, c.envExtra ?? {});
  // A rerun case must run against a COPY. ledger-check signs the file it reads
  // (ledger-check.mjs:417), and that write is suppressed only while the skip is set
  // (:412) -- which is why the skipped cases can safely point at tracked fixtures.
  // Without this, running the suite would append a receipt to a tracked fixture and
  // dirty the working tree.
  // These two build their input rather than naming a fixture: a FIFO cannot be committed,
  // and a 9 MiB file should not be.
  if (c.fifo || c.oversized) {
    if (c.fifo && process.platform === "win32") {
      report(true, `${c.name} (skipped: no mkfifo on win32)`);
      continue;
    }
    const dir = mkdtempSync(join(tmpdir(), "ledger-bounds-"));
    const target = join(dir, "ledger.md");
    if (c.fifo) execFileSync("mkfifo", [target]);
    else writeFileSync(target, "x".repeat(9 * 1024 * 1024));
    let out = "";
    try {
      out = execFileSync("node", [checker, target], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 15000, env: process.env });
      code = 0;
    } catch (err) {
      code = err.status;
      out = `${err.stdout ?? ""}${err.stderr ?? ""}`;
    }
    // A regression here is a HANG, and a hung suite reads as a stuck machine rather than a
    // failing test -- so the timeout above turns it back into an ordinary failure.
    report(code === c.want, c.name, `exit ${code}, want ${c.want}`);
    for (const want of c.expect ?? []) report(out.includes(want), `${c.name} -> ${want}`);
    continue;
  }

  let target = resolve(root, c.file);
  if (c.rerun || c.mutate) {
    const dir = mkdtempSync(join(tmpdir(), "ledger-rerun-"));
    target = join(dir, basename(c.file));
    // `mutate` derives the case's input from a LIVE repo file rather than a frozen copy,
    // so the assertion keeps tracking that file as it changes. That is the point for the
    // template: a fixture snapshot would go on passing after the template drifted.
    const src = readFileSync(resolve(root, c.file), "utf8");
    writeFileSync(target, c.mutate ? c.mutate(src) : src);
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
  // A negative assertion, for cases whose property is an ABSENCE.
  for (const no of c.reject ?? []) report(!out.includes(no), `${c.name} -/-> ${no}`);
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
