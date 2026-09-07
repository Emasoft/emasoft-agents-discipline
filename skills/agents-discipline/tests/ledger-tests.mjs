#!/usr/bin/env node
/**
 * Tests for ledger-check.mjs and the SKILL.md frontmatter.
 * Usage: node tests/ledger-tests.mjs
 */
import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, dirname, join, basename } from "node:path";
import { fileURLToPath } from "node:url";

// A .pyc is validated by (source mtime, source size) ALONE, so a SAME-SIZE rewrite inside one
// mtime second -- exactly what a mutation probe does -- leaves STALE bytecode running while
// the .py on disk reads correct. Measured: that produced a false "no divergence", and
// inspect.getsource() cannot reveal it because it reads the .py. Set HERE rather than in
// scripts/*.py: the guard protects MEASUREMENT, and those are shipped artifacts a user runs.
// Children inherit process.env, so one line covers every spawn site in this file.
process.env.PYTHONDONTWRITEBYTECODE = "1";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const checker = resolve(root, "scripts/ledger-check.mjs");

// The port runs THESE assertions unchanged against the Python implementation: hold the
// oracle fixed, vary only the implementation, so a divergence is a port bug and not a
// re-specified test. `AD_RUNTIME=python node tests/ledger-tests.mjs` selects the port.
const runtime =
  process.env.AD_RUNTIME === "python"
    ? ["python3", [resolve(root, "scripts/ledger_check.py")]]
    : ["node", [checker]];
const [runtimeBin, runtimeArgs] = runtime;

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
  {
    // `want: 1` DOES NOT DISCRIMINATE -- an unknown status exits 1 too, which is precisely the
    // behaviour that made this defect invisible: the verdict was already right and only the
    // CLAIM behind it was wrong. Every assertion below is therefore on the text, and each names
    // one requirement of the item rather than trusting one string to cover them all:
    //   1. the status is KNOWN -- counted under its own name, never swept into `other`
    //   2. the handoff is announced, and names the row
    //   3. the row is NOT filed as work still in flight
    //   4. the closing line says TERMINAL, not INCOMPLETE
    // No `rerun: true`: the status branch sits outside the re-run block, and this case proves
    // that by passing with the skip on rather than by my having read where the block starts.
    name: "abandoned row is terminal, not incomplete",
    file: "tests/fixtures/abandoned-row.md",
    want: 1,
    expect: [
      "abandoned:   1",
      "HANDOFF REQUIRED: 1 abandoned unit(s)",
      "- #2 finance",
      "-> ledger TERMINAL: HANDOFF REQUIRED",
    ],
    reject: [
      "other:",                        // (1) swept into the unknown bucket
      "- #2 finance [abandoned]",      // (3) listed under "unverified rows"
      "-> ledger INCOMPLETE.",         // (4) "still coming" claim on terminal work
    ],
  },
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
    // The same helper's THIRD bound, and the only one that is a security property rather than
    // a liveness one: O_NOFOLLOW. A ledger path that is a symlink is refused outright, so
    // pointing one at a file outside the project cannot make the checker read it. ac569dc
    // claimed this guarantee was "ported, not dropped" and nothing had ever executed it in
    // either runtime — a claim about a security control, resting on having read the code.
    name: "a symlinked ledger path is refused, not followed",
    symlink: true,
    want: 2,
    expect: ["must be one unchanged regular single-link file"],
  },
  {
    // The CONTROL that makes the refusal ATTRIBUTABLE to the link. Same bytes, reached
    // directly instead of through a symlink: exit 0. Without it, "following it would
    // succeed" is a counterfactual nobody runs, and a future change that made this content
    // fail for an unrelated reason would leave the symlink case passing for the wrong
    // reason — silently, on the one bound here that is a security property.
    // I built exactly this remedy for the T24 case two commits earlier and did not apply it
    // here, which is why the pattern is worth naming rather than just fixing.
    name: "the same ledger reached directly is accepted (control for the symlink case)",
    symlink: "target",
    want: 0,
    reject: ["must be one unchanged regular single-link file"],
  },
  {
    // A ledger is prose a human pasted into, so ONE byte of it is routinely not UTF-8: a
    // latin-1 accent, a smart quote out of a word processor, a name copied from a terminal in
    // another encoding. Node decodes those lossily (U+FFFD) and checks the ledger anyway.
    // A decoder that RAISES instead would report exit 2 `cannot read` -- "this is not a
    // ledger" -- on a file that is visibly a ledger, and the row it could not decode is
    // almost never the row under test. Measured on the Python port before its fix: exactly
    // that, `'utf-8' codec can't decode byte 0xe9`, exit 2, on the ledger below.
    // The bytes are written raw rather than through a JS string, because any JS string is
    // valid UTF-8 by construction and cannot express the input this guards.
    name: "a ledger with one non-UTF-8 byte is still checked, not rejected as unreadable",
    rawBytes: Buffer.concat([
      Buffer.from("# L\n\nUnits: 1\nCreated: 2020-01-01T00:00:00+0000\n\n| # | Unit | Files | Worker | Acceptance | Status |\n|---|---|---|---|---|---|\n| 1 | caf", "utf8"),
      Buffer.from([0xe9]),
      Buffer.from(" | a.py | w | `pytest -q` | verified |\n\n**Unit 1 —** ran `pytest -q`, 3 passed\n", "utf8"),
    ]),
    // 0, because the ledger is honest: one verified row, strong evidence, no cited artifact
    // to be missing, and the re-run skipped. The point of the case is that the undecodable
    // byte does not stop the checker -- the verdict is whatever the CONTENT deserves.
    want: 0,
    expect: ["units:       1", "verified:    1"],
    reject: ["cannot read"],
  },
  {
    // JavaScript's `\s` is Unicode-wide; a `\s` compiled under Python's re.ASCII is not, and
    // that flag is otherwise REQUIRED on the same patterns to keep `\d`/`\b` ASCII the way
    // JavaScript has them. Measured: with re.ASCII, `12 tests passed` is a measured
    // result to the oracle and NOT one to the port -- so honest evidence carrying a
    // non-breaking space (what a paste out of a browser or a word processor routinely
    // carries) is silently demoted, and the row it backs reports MISSING evidence.
    // The measured-result pattern is the ONLY strong signal in this ledger: no code span,
    // no filename shape, no exit code. So the assertion cannot pass by another route.
    name: "a non-breaking space inside a measured result is still evidence",
    rawBytes: Buffer.concat([
      Buffer.from("# L\n\nUnits: 1\n\n| # | Unit | Files | Worker | Acceptance | Status |\n|---|---|---|---|---|---|\n| 1 | u | a.py | w | measured by hand | verified |\n\n## Evidence\n\n- 12", "utf8"),
      Buffer.from([0xc2, 0xa0]),
      Buffer.from("tests passed\n", "utf8"),
    ]),
    want: 0,
    expect: ["evidence:    present"],
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
  {
    // A malformed budget must HARD-FAIL, in every runtime, before a single row runs. The
    // underscore form is the one that separates a JavaScript `Number()` from a Python
    // `float()`: Number("1_000") is NaN (numeric separators are a literal-syntax feature, not
    // a parsing one) and float("1_000") is 1000.0. Measured on the port before its fix, the
    // oracle refused this configuration with exit 2 while the port silently ACCEPTED a
    // one-second budget -- the same env var meaning two different things depending on which
    // implementation happened to run it. Silent acceptance of malformed input is the wrong
    // direction to diverge in for a tool whose thesis is that unverifiable claims fail.
    name: "a budget JavaScript would reject is rejected by every runtime",
    file: "tests/fixtures/budget-two-rows.md",
    rerun: true,
    envExtra: { AGENTS_DISCIPLINE_RERUN_BUDGET_MS: "1_000" },
    want: 2,
    expect: ["must be a positive number of milliseconds", '"1_000"'],
  },
  {
    // The other half of the same divergence, and it needs its own case because it fails in
    // the opposite direction: `Number("0x10")` is 16, so the oracle ACCEPTS a 16 ms budget
    // where a bare `float()` raises and the port refused with exit 2. Deterministic —
    // measured identical on both runtimes: 16 ms is under a node startup, so row 1 dies on
    // its own timeout and row 2 on the exhausted budget.
    name: "a hex budget JavaScript accepts is accepted by every runtime",
    file: "tests/fixtures/budget-two-rows.md",
    rerun: true,
    artifacts: ["reports/budget-1.txt", "reports/budget-2.txt"],
    envExtra: { AGENTS_DISCIPLINE_RERUN_BUDGET_MS: "0x10" },
    want: 1,
    expect: ["budget exhausted before this row ran"],
    reject: ["must be a positive number of milliseconds"],
  },
  {
    // The OCTAL prefix. `_js_number` exists only because Number() accepts these and float()
    // does not, and nothing else reaches that rung. The three prefixes share ONE loop, so
    // 0x and 0o already exercise it and a third near-duplicate case for 0b would be padding.
    // (Named "octal and binary" until the review pointed out that only 0o17 appears here — a
    // test name asserting coverage it does not have, in the commit closing a campaign against
    // exactly that. The fix is the name, not another case.)
    // 15 ms is under a node startup, so row 1 dies on its own timeout and row 2 on the budget.
    name: "an octal budget JavaScript accepts is accepted by every runtime",
    file: "tests/fixtures/budget-two-rows.md",
    rerun: true,
    artifacts: ["reports/budget-1.txt", "reports/budget-2.txt"],
    envExtra: { AGENTS_DISCIPLINE_RERUN_BUDGET_MS: "0o17" },
    want: 1,
    expect: ["budget exhausted before this row ran"],
    reject: ["must be a positive number of milliseconds"],
  },
  {
    // `T24:00:00` is legal ISO 8601 for next-day midnight and Date.parse returns it;
    // datetime.fromisoformat raises ("hour must be in 0..23"). Swallowed, that ValueError
    // leaves `Created:` unparsed and the STALENESS RULE SILENTLY SKIPPED — a gate that stops
    // running without saying so. The ledger is dated in the FUTURE so every artifact on disk
    // is necessarily older than it, which is what makes the rule's absence visible: unfixed,
    // the port reported a complete ledger (exit 0) on a citation it should have called stale.
    name: "a Created hour of 24 still enforces the staleness rule",
    file: "tests/fixtures/created-hour-24.md",
    want: 1,
    expect: ["older than the ledger", "tests/fixtures/complete.md"],
  },
  {
    // The CONTROL for the case above, and it belongs in the suite rather than in a commit
    // message: byte-for-byte the same ledger except for one digit in `Created:`, so the same
    // future date and the same citation now yield COMPLETE. Without it, "the T24 case
    // discriminates" rests on a one-off measurement that never runs again.
    // `T24:00:01` and not merely an unparseable string, because it pins the TIGHTENING too:
    // hour 24 is legal ISO only as exactly 24:00:00, and Date.parse returns NaN for this one
    // (measured). A rewrite loose enough to accept it — the `(:.*)?` this replaced — makes
    // the port ENFORCE staleness where the oracle skips it, and this pair is what says so:
    // both cases would then report stale, and only one of them should.
    name: "hour 24 with a non-zero second skips the rule, as Date.parse does",
    file: "tests/fixtures/created-unparseable.md",
    want: 0,
    expect: ["artifacts:   1 cited, all present"],
    reject: ["older than the ledger"],
  },
  {
    // Node reports a signal death as `status: null`, which the oracle's `typeof === "number"`
    // test renders as `exit 1`; Python reports it as returncode -9. Same verdict, different
    // line — and the ledger's own printed output is the artifact a human reads, so a row that
    // segfaults or is OOM-killed must not describe itself two ways depending on which
    // implementation ran.
    name: "a signal-killed acceptance renders the same in every runtime",
    file: "tests/fixtures/signal-killed-acceptance.md",
    rerun: true,
    artifacts: ["reports/signal-1.txt"],
    want: 1,
    expect: ["ACCEPTANCE DID NOT REPRODUCE", "$ kill -9 $$ -> exit 1"],
    reject: ["exit -9"],
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
  if (c.fifo || c.oversized || c.rawBytes || c.symlink) {
    if ((c.fifo || c.symlink) && process.platform === "win32") {
      // Windows HAS symlinks — what it lacks is O_NOFOLLOW, which is why the guarantee
      // cannot hold there and the case is skipped rather than expected to pass.
      report(true, `${c.name} (skipped on win32: no mkfifo, no O_NOFOLLOW)`);
      continue;
    }
    const dir = mkdtempSync(join(tmpdir(), "ledger-bounds-"));
    const target = join(dir, "ledger.md");
    if (c.fifo) execFileSync("mkfifo", [target]);
    else if (c.symlink) {
      // A REAL, valid ledger behind the link, so a refusal cannot be mistaken for the file
      // being unreadable for some other reason. MEASURED, not assumed: reading that target
      // directly exits 0 in BOTH runtimes, so `want: 2` can only come from the refusal —
      // if O_NOFOLLOW were dropped, this case would go green-to-red rather than passing on.
      // (`getattr(os, "O_NOFOLLOW", 0)` degrades to 0 on a platform without it, exactly as
      // the oracle's `fsConstants.O_NOFOLLOW || 0` does; the case skips on win32 anyway.)
      const real = join(dir, "real.md");
      writeFileSync(real, readFileSync(resolve(root, "tests/fixtures/complete.md"), "utf8"));
      if (c.symlink === "target") writeFileSync(target, readFileSync(real, "utf8"));
      else symlinkSync(real, target);
    } else if (c.rawBytes) writeFileSync(target, c.rawBytes);
    else writeFileSync(target, "x".repeat(9 * 1024 * 1024));
    let out = "";
    try {
      // `env`, not `process.env`: the two bounds cases never reach the re-run, but a case that
      // builds a PARSEABLE ledger from raw bytes does, and executing its acceptance would test
      // whether pytest is installed on this machine rather than how the checker decodes.
      out = execFileSync(runtimeBin, [...runtimeArgs, target], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 15000, env });
      code = 0;
    } catch (err) {
      code = err.status;
      out = `${err.stdout ?? ""}${err.stderr ?? ""}`;
    }
    // A regression here is a HANG, and a hung suite reads as a stuck machine rather than a
    // failing test -- so the timeout above turns it back into an ordinary failure.
    report(code === c.want, c.name, `exit ${code}, want ${c.want}`);
    for (const want of c.expect ?? []) report(out.includes(want), `${c.name} -> ${want}`);
    for (const no of c.reject ?? []) report(!out.includes(no), `${c.name} -> no "${no}"`);
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
    out = execFileSync(runtimeBin, [...runtimeArgs, target], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], env });
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
