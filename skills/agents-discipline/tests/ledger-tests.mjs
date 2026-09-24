#!/usr/bin/env node
/**
 * Tests for ledger-check.mjs and the SKILL.md frontmatter.
 * Usage: node tests/ledger-tests.mjs
 */
import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync, symlinkSync, chmodSync, statSync } from "node:fs";
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
// PYTHON overrides the interpreter name (`python` on Windows, `python3` elsewhere) -- the same
// contract the other three AD_RUNTIME-aware suites honor; hardcoding `python3` here could red
// the Windows CI cells for a config reason, not a port defect ("could": unmeasured on Windows).
const runtime =
  process.env.AD_RUNTIME === "python"
    ? [process.env.PYTHON || "python3", [resolve(root, "scripts/ledger_check.py")]]
    : ["node", [checker]];
const [runtimeBin, runtimeArgs] = runtime;

let failed = 0;
function report(ok, name, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
  if (!ok) failed++;
}

// U+001C, for the ARMING CHECK at the bottom of this file. Spelled as ASCII, never as the
// character, for the reason the fold guard states: an assertion written with the raw byte is
// vulnerable to the very normalization pass it exists to catch.
const FS = String.fromCharCode(0x1c);

// `|&` needs bash >= 4.0. `ledger-check.mjs` hardcodes the literal path `/bin/bash` to run every
// acceptance (`execFileSync("/bin/bash", ...)`), and on macOS that path is Apple's own build,
// frozen at 3.2 for GPLv3 avoidance and never updated -- a permanent platform gap, not a defect
// of this machine. Probe the EXACT binary the checker invokes, not whatever `bash` resolves to
// on PATH, or this probe could pass on a machine where the checker itself still cannot.
let bashSupportsPipeAmp = false;
try {
  execFileSync("/bin/bash", ["-c", "true |& cat >/dev/null"], { stdio: "ignore" });
  bashSupportsPipeAmp = true;
} catch { /* /bin/bash here predates 4.0 and cannot parse `|&` at all */ }

const cases = [
  { name: "template (incomplete)", file: "templates/DELEGATION.md", want: 1 },
  { name: "partial ledger", file: "tests/fixtures/partial.md", want: 1 },
  {
    // This fixture has no `Created:` line at all -- the DECISION's other half: absence keeps
    // the ledger passing (the staleness rule genuinely has nothing to check), but now prints a
    // visible warning instead of quietly doing nothing, so a coordinator who forgot the line
    // finds out without the ledger being failed for an absence that was never a grammar
    // violation (that's reserved for a line that's PRESENT and wrong -- the table below).
    name: "complete ledger",
    file: "tests/fixtures/complete.md",
    want: 0,
    expect: ["no Created: date", "file-age check was skipped"],
    reject: ["is not a real date"],
  },
  { name: "not a ledger", file: "SECURITY.md", want: 2 },
  { name: "junk evidence rejected", file: "tests/fixtures/evidence-junk.md", want: 1 },
  { name: "real evidence accepted", file: "tests/fixtures/evidence-real.md", want: 0 },
  { name: "reworded rules still pass", file: "tests/fixtures/rules-reworded.md", want: 0 },
  { name: "truncated row fails loud", file: "tests/fixtures/truncated-row.md", want: 1 },
  { name: "aligned separators + piped cell", file: "tests/fixtures/aligned-and-piped.md", want: 0 },
  // Covers the HEADER's escaped-pipe placeholder branch of columnCount (the row splitter has its
  // own, proven by the case above). BY INSPECTION, dropping the header's `\|` replace makes this
  // header count 7 against a 6-cell row. MEASURED (the pipe unescaped in a copy of this fixture):
  // "6 cells but the header declares 7", the only row malformed, exit 2, both runtimes. The two
  // mutations reach the same branch under today's code; only the second was run. One shape: `\|`.
  { name: "escaped pipe in the HEADER keeps the column count", file: "tests/fixtures/header-escaped-pipe.md", want: 0 },
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
    // The fixture carries PASSING evidence and has no `pending`/`done` row, so the abandonment is
    // the ONLY thing between it and `complete`. That makes TERMINAL beat `complete`, not merely
    // beat `INCOMPLETE` — a weaker fixture failing for two reasons at once could not show which
    // one the closing line was responding to.
    name: "abandoned row is terminal, not incomplete",
    file: "tests/fixtures/abandoned-row.md",
    want: 1,
    expect: [
      "abandoned:   1",
      "evidence:    present",
      "HANDOFF REQUIRED: 1 abandoned unit(s)",
      "-> ledger TERMINAL: HANDOFF REQUIRED",
    ],
    reject: [
      "other:",                        // (1) swept into the unknown bucket
      // Unit 2 here HAS a reason, so the no-reason marker must not appear. This is what kills
      // an implementation that marks every abandoned row — that one passes the unreasoned
      // fixture's `expect` list outright, and only a reasoned row anywhere can catch it.
      "no reason found",
      // The TRAILING BRACKET is the whole assertion. A bare `- #2 finance` is VACUOUS: it appears
      // in the BROKEN output too, because the unverified-rows form `- #2 finance [abandoned]` is
      // a superstring of it. Measured against a mutant whose status read `abandonedX` — the plain
      // substring was present in output that got every other thing wrong. Only the unverified
      // form appends ` [`, so this discriminates where the plain string cannot.
      "- #2 finance [",                // (3) listed under "unverified rows" in ANY status spelling
      "unverified rows",               //     ... and that section should not exist here at all
      "-> ledger INCOMPLETE.",         // (4) "still coming" claim on terminal work
    ],
  },
  {
    // The case above CANNOT see this, and the direction it misses is the dangerous one. Its rows
    // are verified + abandoned, so `unverified` is empty and TERMINAL is correct. Swap one row to
    // `pending` and TERMINAL becomes an over-claim: the ledger is NOT terminal, somebody is still
    // working unit 3. Without the `&& !unverified.length` conjunct the abandoned branch outranks
    // every other incomplete reason and prints TERMINAL anyway — the same over-claim as the
    // `INCOMPLETE`-on-terminal-work defect this whole item fixes, with the sign flipped.
    // Both facts must still reach the reader, on SEPARATE lines: the handoff section above (which
    // is unconditional) and `INCOMPLETE` below. That is gate-check's shape — it prints HANDOFF
    // REQUIRED and UNMET independently, never one instead of the other.
    name: "one abandoned row does not make a ledger with pending work terminal",
    file: "tests/fixtures/abandoned-and-pending.md",
    want: 1,
    expect: [
      "abandoned:   1",
      "pending:     1",
      "- #3 strings [pending]",             // the pending row stays in "unverified rows"
      "HANDOFF REQUIRED: 1 abandoned unit(s)",  // ... and the handoff is still announced
      "-> ledger INCOMPLETE.",              // ... because work IS still coming
    ],
    reject: [
      "- #2 finance [",                     // the abandoned row does not join the unverified list
      "ledger TERMINAL",                    // the over-claim this case exists to catch
    ],
  },
  {
    // The third clause of `abandoned`'s definition, which the first two cases do not touch: the
    // template says the reason is written in the row's Evidence block, and until now nothing
    // checked, so an unexplained abandonment printed identically to an explained one.
    //
    // EXIT CODE CANNOT DISCRIMINATE HERE, exactly as with `abandoned` itself: `complete` requires
    // every row `verified`, so any abandoned ledger already exits 1 whether or not a reason is
    // attributable. `want: 1` is therefore a consistency check, not the assertion — the two
    // `expect` strings and the one `reject` carry the whole case.
    //
    // The `reject` IS the control, and it is the half that can be false. If the marker were
    // printed unconditionally, both `expect` lines would still pass and the case would look
    // green while proving nothing; only the reasoned middle row can catch that. It is the middle
    // row on purpose — see the fixture, which explains why two-rows-one-reasoned is passable by
    // accident and this shape is not.
    //
    // No `rerun: true`. `evidenceBlocks` is populated at `ledger-check.mjs:263-272`, well before
    // `rerunSkipped` exists at `:488`, so the lookup works on the default skip path — and this
    // case passing with the skip ON is the proof of that, rather than my having read where the
    // block starts. Setting `rerun: true` would populate the map for a different reason and hide
    // a regression that broke the skip path.
    name: "an abandoned row with no attributable evidence block is marked as such",
    file: "tests/fixtures/abandoned-unreasoned.md",
    want: 1,
    expect: [
      // #1 has no header at all; #2 has a header with NOTHING under it. The second is the
      // one that matters — a header-only block is non-empty, so an emptiness test passed it.
      "- #1 alpha — no reason found in a **Unit 1** evidence block",
      "- #2 beta — no reason found in a **Unit 2** evidence block",
      "HANDOFF REQUIRED: 3 abandoned unit(s)",
    ],
    reject: [
      // The reasoned row must come through UNTOUCHED. Anchored on the em dash that only the
      // marker introduces: a bare `- #3 gamma` would be vacuous, since it is a substring of the
      // marked form too — the same trap the `- #2 finance [` assertion above documents.
      // It is also what kills a parity-keyed implementation: `unit % 2 === 1` marks 1 and 3.
      "- #3 gamma —",
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
    // Guards the dotted-unit-id fix (d1011f0): the header regex used to key evidence blocks by
    // `[0-9]+` alone, so `**Unit 1.2 —**` filed under unit `1` and row `1.2` (the Depth Tree's
    // own leaf-numbering scheme) read UNBACKED even though its own evidence cites a real file.
    // Row `#` is the literal string "1.2" (unit = cells[0]), so this only exercises the fix
    // when the header capture keeps the dot instead of stopping at the first digit run.
    // want:0 and "ledger complete" is the discriminator: unfixed, the row lands in `unbacked`
    // (line 573) instead of `unreproducible`, which fails the ledger outright.
    name: "a dotted unit id's own evidence block backs it, not row 1",
    file: "tests/fixtures/dotted-unit-backed.md",
    want: 0,
    rerun: true,
    artifacts: ["reports/dotted-1.2.txt"],
    expect: ["ledger complete"],
    reject: ["UNBACKED verified rows"],
  },
  {
    // Guards `d88f586`: Python's bare `re.I` folds U+0131 onto `i`, while JS `/i` without the
    // `u` flag refuses every non-ASCII->ASCII fold -- so a `**Un<U+0131>t 1` line is a unit
    // header to the port and NOT to the oracle. This finder decides evidence ATTRIBUTION, so
    // the port backs a row the oracle leaves unbacked. No fixture carried a fold vector before
    // this one, which is why the suite passed identically with the flag deleted.
    //
    // U+0131 rather than U+0130, though both fold onto `i`: U+0130 decomposes under NFD to
    // `I` + U+0307, which makes the BUGGY checker reject the header too -- this case would
    // then pass with the bug present. U+0131 is stable under all four normalization forms,
    // and its plausible corruption (an editor "fixing" it to ASCII `i`) makes the header
    // match in BOTH states, failing this case loudly instead of disarming it. A guard below
    // asserts the code point is still in the file, because a disarmed fixture still passes.
    //
    // MEASURED in three states -- oracle / port / port with `re.A` removed -- exit 1 / 1 / 0.
    // `- #1 stats` is deliberately NOT asserted: it appears in all three, filed under
    // `unreproducible` in the broken one. The COUNT discriminates and doubles as the control.
    // 1 means row 2 was attributed to its own header; 2 means the fold pooled both rows under
    // row 1; 0 would mean attribution never ran at all.
    //
    // The red is uniquely the LITERAL fold only because a space follows the digit here.
    // Dropping `re.A` unfolds a SECOND thing in the same pattern -- NOT_WORD_AFTER's
    // `[0-9A-Za-z_]`, which under bare `re.I` admits those same four code points -- and that
    // effect runs the OPPOSITE way (`**Unit 1` + U+017F matches with the flag and not without).
    // A space is outside the class in both states, so it is inert here. A future case that puts
    // a fold character after the digit reddens under the same mutation for a different reason.
    name: "a unit header holding U+0131 backs nothing, the ASCII header beside it backs its own row",
    file: "tests/fixtures/unit-header-fold.md",
    want: 1,
    rerun: true,
    artifacts: ["reports/unit-1-output.txt", "reports/unit-2-output.txt"],
    expect: ["UNBACKED verified rows", "unreproducible: 1 verified row"],
    reject: ["ledger complete"],
  },
  {
    // Guards the rules-heading fold (TRDD-REJRD8V5 item 4): Python's bare `re.I` folds U+017F
    // onto `s`, while JS `/i` without `u` refuses every non-ASCII->ASCII fold -- so a heading
    // with U+017F in `Rules` is the skipped boilerplate section to the port and NOT to the
    // oracle. The oracle scans the bullet under it: a code span with `/` and `.` scores strong,
    // and its backticked bare path cites the one artifact that backs row 1. The port with the
    // fold skips the whole section, so it reports `evidence: MISSING`, cites nothing, and lands
    // row 1 in UNBACKED. `re.I | re.A` at the match closes it.
    //
    // The weak `**Unit 1 <U+2014>** see the section below` line sits OUTSIDE the section so the only
    // strong evidence, and the only citation, is behind the folded heading: both the exit and
    // the `evidence:` line then discriminate, instead of the substring alone.
    //
    // U+017F rather than U+0131: the heading carries `s` three times and its `i` is inside
    // `this`. U+017F has no canonical decomposition (NFC/NFD leave it alone); only NFKC maps it
    // to `s`, and a fixture so "fixed" makes the heading match in BOTH runtimes -- both skip,
    // both MISSING, both exit 1 -- so this case reds loudly rather than disarming. The guard at
    // the bottom of this file pins the code point in the heading itself.
    //
    // MEASURED in three states -- oracle / port / port with `re.A` removed at the rules-heading
    // match -- exit 0 / 0 / 1, `evidence:` present / present / MISSING.
    name: "a rules heading holding U+017F is boilerplate to nobody, the bullet under it is evidence",
    file: "tests/fixtures/rules-heading-fold.md",
    want: 0,
    rerun: true,
    artifacts: ["reports/unit-1-output.txt"],
    expect: ["evidence:    present", "artifacts:   1 cited, all present", "ledger complete"],
    reject: ["evidence:    MISSING", "UNBACKED verified rows"],
  },
  {
    // Guards `EXIT_CODE`'s `re.A` (TRDD-REJRD8V5 item 5). The fold fix at ledger_check.py:149
    // landed without a vector: MEASURED on the tree before this case, dropping that flag left
    // the port suite 147 green and the whitespace suite 70 green. Bare `re.I` folds U+0131 onto
    // `i`, so `ex<U+0131>t 0` satisfies `exit\s+\d+` in the port and not in node, and the port
    // scores strong a line the oracle calls MISSING. The line carries NO backticks, NO dot and
    // NO runner word (the exit-code writer's invariant in whitespace-diff.sh), so `EXIT_CODE`
    // is the sole route to strong evidence and the fold is the only thing this case can see.
    //
    // THE GATE IS THE `evidence:` SUBSTRING; the exit corroborates it. On the skip path evidence
    // is the only thing that can fail, so exit and `evidence:` are one fact read twice -- not
    // the two independent axes of the rules-heading case above (where a citation moved the
    // exit on its own). NO `rerun: true`: measured on this fixture, that path exits 1 / 1 / 1
    // with UNBACKED in every state, so the exit would be silent there and only `evidence:`
    // would differ.
    //
    // U+0131 for the reason the unit-header case gives: stable under all four normalization
    // forms, and its plausible corruption to ASCII `i` makes the line strong in BOTH runtimes
    // (both exit 0, both `present`), so this case reds loudly instead of disarming. One vector,
    // U+0131; not U+0130, which decomposes under NFD (see the unit-header case). The port
    // comment's `EX<U+0130>T 0` probe is what bare `re.I` did, not the gated set. The guard at
    // the bottom of this file pins the code point in the line.
    //
    // MEASURED oracle / port / port with `re.A` removed at EXIT_CODE: exit 1 / 1 / 0,
    // `evidence:` MISSING / MISSING / present.
    name: "an exit code spelled with U+0131 is not an exit code, in either runtime",
    file: "tests/fixtures/exit-code-fold.md",
    want: 1,
    expect: ["evidence:    MISSING"],
    reject: ["evidence:    present", "ledger complete"],
  },
  {
    // Every one of these exits 0 UNCONDITIONALLY, which is the actual bar. #4 is the one
    // that matters most: `pytest -q || true` is the canonical always-pass idiom, it has a
    // real left half, and no whole-command regex can see it because the cheat lives in the
    // operator. `pipefail` does not help either -- `||` is not a pipe.
    //
    // Rows 10-11 guard a SEPARATE defect from the other nine: isNoopAcceptance only peeled a
    // wrapping `()`/`{}` around the WHOLE command, never per-fragment after the `&&` split, so
    // ALWAYS_TRUE -- which has no alternative that matches a `(...)`-wrapped fragment -- never
    // saw a fragment like `(echo ok)`. Both rows are single-command no-ops (no `||`/`;`), so
    // they reach isNoopAcceptance directly rather than being refused upstream like rows 4/6-9.
    // MEASURED pre-fix: `AGENTS_DISCIPLINE_SKIP_RERUN=1` unset, row 10's `true && (echo ok)`
    // was NOT flagged a no-op, fell through to acceptanceCommand() and real execution, really
    // exited 0, and the row was certified `reran ... passed` -- the same fabricated-verified
    // outcome the other nine rows exist to prevent, reached through real execution instead of
    // a forged EVIDENCE line. `peelWrapping()` now runs on each split fragment too, closing it.
    name: "no-op acceptances are rejected",
    file: "tests/fixtures/noop-acceptance.md",
    want: 1,
    rerun: true,
    artifacts: ["reports/noop-1.txt", "reports/noop-2.txt", "reports/noop-3.txt", "reports/noop-4.txt", "reports/noop-5.txt", "reports/noop-6.txt", "reports/noop-7.txt", "reports/noop-8.txt", "reports/noop-9.txt", "reports/noop-10.txt", "reports/noop-11.txt", "reports/noop-12.txt"],
    // Two distinct verdicts, and the split matters. Rows 1-3, 5, 10, 11 are single commands
    // (possibly `&&`-chained) that happen to exit 0 always, so they are judged. Rows 4 and
    // 6-9 carry `||` or `;` and are REFUSED before judgement -- which is the point of the
    // USER's design decision: every one of those four was a defect that shipped, was found by
    // adversarial review, and was fixed by a parser that then leaked somewhere else. Refusing
    // the operators retires the class.
    expect: [
      "#1 $ : -> no-op acceptance",
      "#2 $ exit 0 -> no-op acceptance",
      "#3 $ /bin/true -> no-op acceptance",
      "#5 $ ( exit 0 ) -> no-op acceptance",
      "#10 $ true && (echo ok) -> no-op acceptance",
      "#11 $ (true) && (true) -> no-op acceptance",
      "#4 $ pytest -q || true -> acceptance uses `||` or `;`",
      "#6 $ true || pytest -q -> acceptance uses `||` or `;`",
      "#7 $ false; true -> acceptance uses `||` or `;`",
      "#8 $ ;true -> acceptance uses `||` or `;`",
      "#9 $ (false) ; (true) -> acceptance uses `||` or `;`",
      // A lone `&` discards the backgrounded command's status: `false & true` re-ran and
      // PASSED before BACKGROUND_OPERATOR existed.
      "#12 $ false & true -> acceptance backgrounds a command with `&`",
    ],
  },
  {
    // The `\s` divergence inside ALWAYS_TRUE, one vector per source occurrence. node's `\s` is
    // 25 code points and Python's is 29; U+001C is in Python's set and not in node's, so an
    // unconverted `\s` makes the port see a no-op where the oracle sees an ordinary command.
    //
    // MEASURED, and it is why these assert on the MESSAGE and never on the verdict: bash treats
    // U+001C as an ordinary character rather than a blank, so `echo<U+001C>ok` is a single word
    // naming a command that does not exist -- exit 127, in both runtimes. Both legs therefore
    // land in `repro_failed` and both print `-> ledger INCOMPLETE.`, so `want: 1` cannot
    // discriminate here; it is a control for "the ledger still parsed", nothing more.
    //
    // The `&& echo x` suffix is LOAD-BEARING and must not be tidied away as dead. It is
    // unreachable at runtime -- 127 short-circuits it -- and that is not its job: it puts
    // ORDINARY spaces in the cell so `acceptance_command`'s own still-unconverted `\s` matches
    // in BOTH runtimes. Without it node returns null there and the row diverges a second time,
    // at a site this case does not own, so the row would stay red after ALWAYS_TRUE is fixed
    // and read as a failed fix.
    //
    // NOT SELF-ARMING -- it needs the external guard at the bottom of this file, exactly as the
    // U+0131 case does. The expect strings carry the pad as a RAW BYTE, the same way the fixture
    // does; shown here between backticks: ``. Because BOTH sides are raw, ONE normalization pass over the
    // tree rewrites them together and this case keeps passing while testing nothing: the fixture
    // becomes `echook && echo x`, the expect becomes the same string, and `echook` is still a
    // command that does not exist -- still exit 127, still a match. Nothing inside the case can
    // see that, which is what the arming guard is for.
    //
    // The disarms the case DOES catch by itself, both loud: delete the byte from the fixture
    // alone and the printed command no longer matches an expect line that still holds it; turn
    // the pad into an ordinary space and the command exits 0, filing the row under `re-ran`
    // where it prints nothing at all, so every expect line reds.
    name: "U+001C in an acceptance is whitespace to Python and not to node, at all three ALWAYS_TRUE sites",
    file: "tests/fixtures/noop-pad-divergence.md",
    want: 1,
    rerun: true,
    artifacts: ["reports/pad-1.txt", "reports/pad-2.txt", "reports/pad-3.txt"],
    // One vector per SOURCE OCCURRENCE: `(?:\s+[^&|;]*)?`, `command\s+true`, `exit\s+0`. The
    // five verbs and two path prefixes all route through the FIRST occurrence -- one textually
    // shared fragment, so they are one site and one vector. A per-verb matrix would be testing
    // the alternation list, which is not what changed.
    expect: [
      "#1 $ echook && echo x -> exit 127",
      "#2 $ commandtrue && echo x -> exit 127",
      "#3 $ exit0 && echo x -> exit 127",
    ],
    // Before the conversion the port prints this instead, on all three rows.
    reject: ["no-op acceptance"],
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
    //
    // Row 7 is a CHARACTERIZATION guard (does not discriminate the peel-per-fragment fix
    // added alongside the "no-op acceptances are rejected" case above -- `node` was never in
    // ALWAYS_TRUE, so `.every()` already failed on the first fragment before or after that
    // fix, for the same reason both times). It still earns its place standing guard on the
    // opposite direction of that fix: a genuinely fallible first fragment (`node -e
    // "process.exit(0)"`, itself containing a MASKED-quoted paren from `process.exit(0)`)
    // combined with a real, parenthesized, always-true SECOND fragment (`(echo done)`) must
    // still be judged NOT a no-op and really executed -- catching a FUTURE change to
    // peelWrapping/ALWAYS_TRUE that widens per-fragment peeling into a false positive, which
    // this row's own trace could not construct against the fix as it stands today.
    artifacts: ["reports/weak-1.txt", "reports/weak-2.txt", "reports/weak-3.txt", "reports/weak-4.txt", "reports/weak-5.txt", "reports/weak-6.txt", "reports/weak-7.txt"],
    // `reject`, not `expect`: the property under test is an ABSENCE -- none of these three
    // is flagged a no-op. Asserting a re-ran COUNT instead would test something else and
    // did: `[ -f x ]` is not extracted as a runnable command at all (a separate, pre-existing
    // limitation of acceptanceCommand), so only two of the three ever execute.
    expect: ["re-ran:      6 acceptance command(s), all passed"],
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
    // It is ALSO the gate on `inRulesSection`'s PERSISTENCE, by the same property: the rules
    // bullets carry backticked commands, so a checker that stops skipping the section early --
    // after one non-blank line, or at the blank line under the heading -- leaks a strong span,
    // and `evidence:    MISSING` reds while `want: 1` still holds on UNBACKED. MEASURED: a
    // reset-after-one mutant in the port (`AD_RUNTIME=python node tests/ledger-tests.mjs`) and
    // in the oracle, and a reset-on-blank mutant in the port; each reddened this case and no
    // other case in the suite. The case below ARMS it.
    name: "copy the template and flip every status: still fails",
    file: "templates/DELEGATION.md",
    // The template's own `Created:` line is a placeholder ("<ISO 8601, e.g. ...>"), never a
    // real date. Flipping every row to `verified` makes the staleness branch run at all, and
    // since the DECISION a present-but-invalid `Created:` line now fails the ledger outright --
    // exactly what would happen to an unedited copy-paste, but not what THIS case is testing.
    // Filling in a real (past) stamp keeps the case on the evidence/UNBACKED assertion it exists
    // to make.
    mutate: (t) =>
      t
        .replace(/\| pending \|/g, "| verified |")
        .replace(/^Created:.*$/m, "Created: 2020-01-01T00:00:00Z"),
    want: 1,
    // Needed for the UNBACKED assertion -- that verdict lives inside the re-run block.
    // Safe: every Acceptance cell is the placeholder `<command>`, which has no whitespace,
    // so acceptanceCommand extracts nothing and nothing is executed.
    rerun: true,
    expect: ["evidence:    MISSING", "UNBACKED verified rows"],
  },
  {
    // ARMING CHECK for the persistence gate above. That gate rests on the template's rules text
    // scoring strong when it is NOT skipped -- a precondition nothing asserted, so shortening
    // the rules section, or narrowing `isStrongEvidence`, would disarm it with every check
    // green and the case above still passing. Rename the heading so the section is scanned and
    // require the evidence to be seen: same text, same predicate, must read `present`. If this
    // reds, the case above no longer gates persistence -- restore strong text or move the gate.
    // MEASURED: replacing the bullets with plain prose reads MISSING in both runtimes, so this
    // can fail; stripping every backtick does NOT disarm it, because filename-shaped prose such
    // as `tests/stats.py` still scores -- the gate rests on ANY strong route, not on the spans.
    // Not a regex over the template: a `[^`]*\s[^`]*` scan also matches the GAP between two
    // spans (`||` or `;` yields "` or `"), so it passed on text with every span de-whitespaced.
    // Going through the checker asserts the route it actually uses.
    name: "template rules text scores strong when not skipped (arms the persistence gate)",
    file: "templates/DELEGATION.md",
    // Same placeholder-`Created:` fix as the case above -- flipping rows to `verified` arms
    // the staleness branch, and the template's placeholder date must not be what this case is
    // measuring.
    mutate: (t) =>
      t
        .replace(/\| pending \|/g, "| verified |")
        .replace(/^Created:.*$/m, "Created: 2020-01-01T00:00:00Z")
        .replace("## Rules of this ledger", "## Notes on this ledger"),
    want: 0,
    expect: ["evidence:    present"],
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
    // Guards the named-timeout fix (d1011f0): a re-run that is genuinely IN FLIGHT when the
    // budget runs out used to print bare `exit 1`, indistinguishable from a real acceptance
    // failure -- unlike the case above, where the row never starts at all. A 300ms budget on
    // a `setTimeout(1000)` acceptance starts with `remaining > 0` (so it takes the execFileSync path,
    // not the pre-check above) and is killed mid-run by the child's own `timeout` option,
    // which is what raises Node's ETIMEDOUT / Python's TimeoutExpired this case pins.
    name: "an acceptance that sleeps past a small re-run budget is named 'timed out'",
    file: "tests/fixtures/timeout-budget.md",
    rerun: true,
    artifacts: ["reports/timeout-1.txt"],
    envExtra: { AGENTS_DISCIPLINE_RERUN_BUDGET_MS: "300" },
    want: 1,
    expect: ["timed out — the re-run budget ran out while this acceptance was running"],
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
    // both cases would then report stale, and only one of them should. Since the DECISION, an
    // out-of-grammar `Created:` line that is PRESENT fails the ledger outright instead of
    // silently skipping the rule, so this control now reads "invalid stamp", not "complete".
    name: "hour 24 with a non-zero second fails the ledger as an invalid Created stamp",
    file: "tests/fixtures/created-unparseable.md",
    want: 1,
    expect: ["is not a real date", "2099-01-01T24:00:01"],
    reject: ["older than the ledger", "artifacts:   1 cited, all present"],
  },
  {
    // Guards the port's fraction handling on the T24 rewrite:
    // `T24:00:00.000Z` is legal ISO 8601 -- hour 24 denotes exactly next-day midnight, and an
    // all-zero fraction still means that same instant -- and Date.parse accepts it. The old
    // rewrite regex only recognised `T24`, `T24:00`, or `T24:00:00`
    // with no fraction at all, so it failed to match and the whole `Created:` line fell
    // through unparsed -- the same silent-skip failure mode as the case above, reached by a
    // fraction instead of a bare hour 24. want:1 and the future date make an unfixed port's
    // silent skip visible the same way: unfixed, every artifact reads younger than an
    // unenforced rule and the ledger reports complete instead of stale.
    // Node needs no change here (Date.parse already accepts the fraction natively, measured),
    // so this case only reds the Python port when change 1 (the `(?:\.0+)?` group) is reverted.
    name: "a Created hour of 24 with an all-zero fraction still enforces the staleness rule",
    file: "tests/fixtures/created-hour-24-fraction.md",
    want: 1,
    expect: ["older than the ledger", "tests/fixtures/complete.md"],
  },
  // Table-driven: every `Created:` stamp shape parity.sh measured Node's Date.parse against.
  // Each stamp is dated 2099 so a parsed (non-NaN) stamp always makes `tests/fixtures/complete.md`
  // read "older than the ledger", and an out-of-grammar stamp now FAILS the ledger outright
  // (exit 1, naming the stamp) instead of silently skipping the staleness rule -- a `Created:`
  // line that is present but wrong is a ledger defect, not something to shrug past. Only a
  // ledger with NO `Created:` line at all gets the quieter "skipped" treatment, covered by its
  // own case below the table.
  ...[
    ["2099-09-24T24:00:00.000Z", "stale"],
    ["2099-09-24T24:00:00.0Z", "stale"],
    ["2099-09-24T24:00:00.0000000Z", "stale"],
    ["2099-09-24T24:00:00.5Z", "invalid"],
    ["2099-09-24T24:00.000Z", "invalid"],
    ["2099-09-24T24:00:00.000+02:00", "stale"],
    ["2099-09-24T24:00:00.000+0200", "stale"],
    ["2099-09-24T24.000Z", "invalid"],
    ["2099-09-24T24:00:00", "stale"],
    ["2099-09-24T24:00:00.000", "stale"],
    ["2099-09-24T10:00:00.5Z", "stale"],
    ["2099-09-24T10:00:00.1234567Z", "stale"],
    ["2099-09-24T10:00:00.1234567", "stale"],
    ["2099-09-24T10:00.5Z", "invalid"],
    ["2099-09-24T10:00:00.123+0200", "stale"],
    ["2099-09-24T10:00:00+0200", "stale"],
    ["2099-09-24 10:00:00.5Z", "stale"],
    ["2099-09-24T10Z", "invalid"],
    ["2099-09-24T10:00Z", "stale"],
    // Calendar-validity probes (TRDD: the Created grammar is now declared once and validated
    // the same way in both runtimes, instead of leaning on fromisoformat/Date.parse -- Node's
    // Date.parse used to roll an impossible date over instead of rejecting it).
    ["2026-02-30T10:00Z", "invalid"], // February never has a 30th
    ["2026-04-31T10:00Z", "invalid"], // April has 30 days
    ["2026-02-29T10:00Z", "invalid"], // 2026 is not a leap year
    // Dated 2096, not 2028: 2028 is a time bomb -- a leap year today, but the row would go
    // silently red once 2028 itself is in the past and the "future date" assumption above it
    // (every 2099-dated stamp is newer than "now") stops holding for THIS one row alone. 2096
    // buys the same century's worth of runway the 2099 rows already assume.
    ["2096-02-29T10:00Z", "stale"], // 2096 is a leap year (divisible by 4, not a century)
    // The century rule, both directions: 2100 is divisible by 4 but NOT by 400, so it is NOT a
    // leap year (a bare `y % 4 === 0` would wrongly accept Feb 29 here); 2400 is divisible by
    // both, so it IS one. Without both rows, a broken century check (`y % 4 === 0` alone) passes
    // every leap-year case in this table -- 2400 is the only one where the two rules disagree.
    ["2100-02-29T10:00Z", "invalid"], // 2100 is divisible by 100 but not by 400: not a leap year
    ["2400-02-29T10:00Z", "stale"], // 2400 is divisible by 400: a leap year
    ["2026-13-01T10:00Z", "invalid"], // month 13 does not exist
    // Shape probes: single-digit hour/minute/second, a double colon, an extra field, hour 24
    // without its mandatory minute, and malformed offsets -- each must fail the ledger, never
    // be guessed at.
    ["2099-09-24T25:00Z", "invalid"],
    ["2099-09-24T10:60Z", "invalid"],
    ["2099-09-24T1:00Z", "invalid"],
    ["2099-09-24T10:0Z", "invalid"],
    ["2099-09-24T10:00:0Z", "invalid"],
    ["2099-09-24T10::00Z", "invalid"],
    ["2099-09-24T10:00:00:00", "invalid"],
    ["2099-09-24T24Z", "invalid"],
    ["2099-09-24T24", "invalid"],
    // The extraction regex now requires the captured stamp to end at whitespace or end of line
    // (the truncation fix): a malformed 1-digit offset used to leave the bare, VALID
    // "...T10:00" behind and this row read "stale" on the truncated remainder. It now captures
    // the WHOLE malformed token, "...T10:00+2", which fails the grammar outright.
    ["2099-09-24T10:00+2", "invalid"],
    ["2099-09-24T10:00+25:00", "invalid"],
    ["2099-09-24T10:00+0200", "stale"],
    ["2099-09-24T10:00+02:00", "stale"],
    ["2099-09-24T10", "invalid"],
    ["2099-09-24T1000", "invalid"],
    ["2099-09-24T10:00", "stale"],
    // Review-flagged gaps (ADVERSARIAL-REVIEW on this TRDD): a year below 1970 is ambiguous
    // between engines (JS's `new Date(0, ...)` legacy-maps years 0-99 to 1900-1999; Python's
    // `datetime(0, ...)` raises) or can crash the arithmetic below (Python's next-day rollover,
    // a naive `.timestamp()` on Windows), so the grammar excludes the whole 1970-2999 border
    // outright rather than letting either engine guess or fault.
    ["0000-01-01T10:00Z", "invalid"],
    // A non-ASCII decimal digit (Arabic-Indic zero, U+0660) in place of the hour's leading "1":
    // the CREATED extraction regex only ever captures ASCII `[0-9]`/`\d` characters in EITHER
    // runtime, so this digit never reaches CREATED_STAMP_RE at all -- this row pins that
    // extraction-level rejection, not a `re.ASCII` flag (the grammar regex already spells every
    // digit as the literal class `[0-9]`, which is ASCII-only with or without that flag).
    [`2099-09-24T٠0:00Z`, "invalid"],
  ].map(([stamp, outcome]) => ({
    name: `Created stamp '${stamp}' ${outcome === "stale" ? "is parsed and enforces staleness" : "fails the ledger as an invalid Created stamp"}`,
    file: "tests/fixtures/created-table-placeholder.md",
    mutate: (t) => t.replace("CREATED_PLACEHOLDER", stamp),
    want: 1,
    expect: outcome === "stale" ? ["older than the ledger", "tests/fixtures/complete.md"] : ["is not a real date"],
    reject: outcome === "stale" ? ["is not a real date"] : ["older than the ledger"],
  })),
  {
    // Guards the `Z`/fraction capture in `Created:` (d1011f0): dropping the `Z` made a UTC
    // stamp parse as LOCAL time, so west of UTC a fresh artifact could read "older than the
    // ledger". TZ=America/New_York (UTC-4 or UTC-5 depending on DST, at least 4 hours either way) is the west-of-UTC probe; the
    // `Created:` stamp is generated an hour before "now" so a 4-hour misreading would place
    // the false Created time THREE HOURS AFTER the artifact that was just written into the
    // temp dir -- reddening `staleArtifacts` on the bug and staying green on the fix, which
    // parses the trailing `Z` and treats the stamp as UTC regardless of the process TZ.
    name: "a Z-stamped Created is read as UTC even under a west-of-UTC TZ",
    file: "tests/fixtures/created-z-tz.md",
    mutate: (t) => t.replace("CREATED_PLACEHOLDER", new Date(Date.now() - 60 * 60 * 1000).toISOString()),
    rerun: true,
    artifacts: ["reports/tz-artifact.txt"],
    envExtra: { TZ: "America/New_York" },
    want: 0,
    expect: ["ledger complete"],
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
  {
    // Regression for the receipt guard added alongside 73b386b's `&` fix: that commit re-read
    // the ledger before the WRITE but computed the printed verdict -- and the exit code -- from
    // bytes captured at start-up, so a ledger edited mid-run still printed "ledger complete" and
    // exited 0 while silently skipping the receipt. This row's own acceptance is the mutator: it
    // appends to the ledger FILE ITSELF while the checker holds it open for the re-run, which is
    // the cheapest reproduction of "the coordinator edited DELEGATION.md while a worker's check
    // was still running" available inside a synchronous test. `tee -a ... <<< ...` is real and
    // non-no-op (not in ALWAYS_TRUE, no `&&`/`;`/`||`/lone `&`), so it reaches BACKGROUND_OPERATOR
    // and CHAIN_OPERATORS honestly and passes them -- the row would otherwise be `complete`.
    name: "the ledger changing mid-run VOIDS the verdict and signs no receipt",
    file: "tests/fixtures/mid-run-edit.md",
    rerun: true,
    artifacts: ["reports/mid-run-edit-1.txt"],
    want: 1,
    expect: [
      "re-ran:      1 acceptance command(s), all passed",
      "-> verdict VOID: the ledger changed while it was being checked; re-run the checker.",
    ],
    // The old code's line -- removed as redundant once the verdict itself says VOID -- and the
    // claim a changed ledger must never make: that it is a finished, verified ledger. Whether a
    // receipt got signed is not a stdout claim at all (the stamp is written to the FILE, never
    // printed), so that half of the guard lives in `fileAfter` below, not here.
    reject: ["receipt:     NOT WRITTEN", "ledger complete"],
    // `agents-discipline-check:` is the receipt's own marker text -- it is written to the ledger
    // FILE, never to stdout, so asserting its absence from `out` above was vacuous: it always
    // passed, receipt or no receipt. Read the file the acceptance actually mutated (the tmp
    // `target`, not the tracked fixture) and check both halves for real: the mutator's own edit
    // ("appended", written by the fixture's `tee -a ... <<< appended`) must have landed --
    // otherwise this case would prove nothing changed at all -- and the receipt marker must be
    // absent, which is the fact `reject` above cannot see.
    fileAfter: {
      // The bare word "appended" is VACUOUS: it already sits in the fixture's own Acceptance
      // cell (`<<< appended >/dev/null`), so this assertion would pass even if the mutator's
      // `tee -a` never ran at all -- it is a substring of the START bytes, the very thing this
      // case exists to prove got overwritten by a lost update. `tee -a` appends "appended\n" as
      // its OWN line at EOF, preceded by the newline that already ends the file; the cell text
      // has "appended" flanked by spaces, never by newlines on both sides, so `\nappended\n`
      // is absent from the start-of-run bytes and present only once the append has landed.
      includes: ["\nappended\n"],
      excludes: ["agents-discipline-check:"],
    },
  },
  {
    // Positive control for the SAME commit's `&` refusal, split off the `|&` half (below) so this
    // one runs unconditionally: `2>&1`, `>&2` and `&>/dev/null` are all bash-3.2-safe (verified
    // with `/bin/bash -c`), unlike `|&` which needs bash >= 4 and is never gated behind a probe
    // here on purpose -- an always-run case is worth more than a needsPipeAmp-skipped one.
    name: "the allowed & forms (2>&1, >&2, &>/dev/null) still pass BACKGROUND_OPERATOR",
    file: "tests/fixtures/background-forms-safe.md",
    rerun: true,
    artifacts: ["reports/background-safe-1.txt"],
    want: 0,
    expect: ["re-ran:      1 acceptance command(s), all passed"],
    reject: ["acceptance backgrounds a command with"],
  },
  {
    // `|&` alone, isolated from the always-run case above because it needs bash >= 4 to even
    // parse -- `/bin/bash` on macOS is Apple's own build, frozen at 3.2 for GPLv3 avoidance, and
    // cannot run this command at all. `needsPipeAmp` gates it on the EXACT binary the checker
    // invokes (`/bin/bash`), not whatever `bash` resolves to on PATH -- see the probe above;
    // without the gate this case would falsely fail on every Apple-shipped bash.
    name: "the allowed & form |& still passes BACKGROUND_OPERATOR",
    file: "tests/fixtures/background-forms.md",
    rerun: true,
    needsPipeAmp: true,
    artifacts: ["reports/background-1.txt"],
    want: 0,
    expect: ["re-ran:      1 acceptance command(s), all passed"],
    reject: ["acceptance backgrounds a command with"],
  },
  {
    // Regression for the PERMISSIONS defect in the receipt guard: writeAtomic's temp file is
    // created 0o600 and renamed over the target, so a naive receipt write would silently reset
    // every checked ledger's mode to owner-only on every passing run. `chmodBefore`/`modeAfter`
    // (harness fields, see the loop below) set the temp copy to 0644 before the run and assert
    // it is STILL 0644 after -- a receipt genuinely got written (this ledger passes and is
    // writable) without leaking writeAtomic's internal temp-file mode past the caller.
    // POSIX-only: win32 has no group/other mode bits to preserve or lose.
    name: "receipt write preserves the ledger's own mode bits (0644)",
    file: "tests/fixtures/background-forms-safe.md",
    rerun: true,
    posixOnly: true,
    chmodBefore: 0o644,
    modeAfter: 0o644,
    artifacts: ["reports/background-safe-1.txt"],
    want: 0,
    fileAfter: { includes: ["agents-discipline-check:"] },
  },
  {
    // The other half of the same PERMISSIONS defect: a read-only ledger (0444) used to get
    // silently skipped by the OLD truncating write (it failed and the failure was swallowed),
    // and writeAtomic's rename-over-target would now succeed regardless of the ledger's own
    // mode -- turning a historically no-op write into a real one on a file the user marked
    // read-only. The pre-write accessSync(W_OK) probe must refuse before writeAtomic ever runs,
    // so the ledger is untouched: same mode, same bytes, no receipt.
    name: "a read-only ledger (0444) gets no receipt and is left byte-for-byte unchanged",
    file: "tests/fixtures/background-forms-safe.md",
    rerun: true,
    chmodBefore: 0o444,
    modeAfter: 0o444,
    artifacts: ["reports/background-safe-1.txt"],
    want: 0,
    fileAfter: { excludes: ["agents-discipline-check:"], unchanged: true },
  },
  {
    // Regression for the RE-READ FAILURE defect: `ledgerChanged` used to be computed with no
    // try/catch, so a ledger deleted mid-run made readStableRegularFile THROW instead of
    // returning unequal bytes -- an uncaught exception (Node) / traceback (Python) and no
    // verdict line at all. This row's own acceptance is the mutator: it deletes the ledger
    // file itself (by the same fixed relative name the mid-run-edit.md case uses -- the
    // checker's acceptance commands run with cwd = the ledger's own directory, so the fixture
    // can name itself without knowing the mkdtempSync-generated temp path) while the checker
    // still holds the pre-delete bytes in memory for the re-read.
    name: "the ledger disappearing mid-run VOIDS the verdict instead of crashing",
    file: "tests/fixtures/mid-run-delete.md",
    rerun: true,
    artifacts: ["reports/mid-run-delete-1.txt"],
    want: 1,
    expect: [
      "re-ran:      1 acceptance command(s), all passed",
      "-> verdict VOID: the ledger changed while it was being checked; re-run the checker.",
    ],
    // No stack trace / traceback text, and no receipt claim -- a crash would print neither the
    // VOID line above nor a clean exit; these are the negative half of that same assertion.
    reject: ["Traceback (most recent call last)", "Error:", "receipt:     NOT WRITTEN", "ledger complete"],
  },
];

for (const c of cases) {
  if (c.needsPipeAmp && !bashSupportsPipeAmp) {
    report(true, `${c.name} (skipped: this machine's /bin/bash cannot run \`|&\`, e.g. Apple's frozen bash 3.2)`);
    continue;
  }
  // posixOnly: win32 has no group/other mode bits, and chmod there only toggles a single
  // read-only attribute -- a POSIX mode-preservation assertion has nothing to preserve there.
  if (c.posixOnly && process.platform === "win32") {
    report(true, `${c.name} (skipped on win32: POSIX mode bits)`);
    continue;
  }
  // A chmod-0444-then-assert-untouched case is meaningless run as root: POSIX access(2) grants
  // W_OK to euid 0 regardless of the mode bits, so the checker's own write-guard would (rightly)
  // decide the file IS writable and this case's premise never holds. Skip rather than red a
  // correct implementation for a test environment it cannot control.
  if (c.chmodBefore === 0o444 && typeof process.getuid === "function" && process.getuid() === 0) {
    report(true, `${c.name} (skipped: running as root, W_OK is unconditional)`);
    continue;
  }
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
  let writtenContent = null;
  if (c.rerun || c.mutate) {
    const dir = mkdtempSync(join(tmpdir(), "ledger-rerun-"));
    target = join(dir, basename(c.file));
    // `mutate` derives the case's input from a LIVE repo file rather than a frozen copy,
    // so the assertion keeps tracking that file as it changes. That is the point for the
    // template: a fixture snapshot would go on passing after the template drifted.
    const src = readFileSync(resolve(root, c.file), "utf8");
    writtenContent = c.mutate ? c.mutate(src) : src;
    writeFileSync(target, writtenContent);
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
    // chmodBefore: set the temp copy's mode BEFORE the checker ever sees it, so the receipt
    // guard's pre-write accessSync/os.access probe reads the mode the PERMISSIONS regression
    // cases exist to exercise, not whatever mkdtempSync's default happens to be.
    if (c.chmodBefore !== undefined) chmodSync(target, c.chmodBefore);
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
  // Some facts (the receipt stamp) are never printed -- they land on the ledger FILE, so a
  // stdout `reject` for them is vacuous by construction. `fileAfter` reads the temp `target`
  // the rerun actually mutated (never the tracked fixture at `c.file`, which the rerun branch
  // above never touches) and checks it directly. Gated on `c.rerun` because only that branch
  // copies the fixture to a real temp file first -- the plain path still points at `root`.
  if (c.fileAfter && c.rerun) {
    const fileContent = readFileSync(target, "utf8");
    for (const want of c.fileAfter.includes ?? []) {
      report(fileContent.includes(want), `${c.name} -> file has "${want}"`);
    }
    for (const no of c.fileAfter.excludes ?? []) {
      report(!fileContent.includes(no), `${c.name} -/-> file has "${no}"`);
    }
    // `unchanged`: the PERMISSIONS regression's read-only half. writeAtomic must never have run
    // at all, so the file on disk must equal the exact bytes written before the checker touched
    // it -- not merely "no receipt marker substring", which a write that happened to omit the
    // marker text could satisfy by accident.
    if (c.fileAfter.unchanged) report(fileContent === writtenContent, `${c.name} -> file byte-for-byte unchanged`);
  }
  // modeAfter: the mode-preservation half of the same regression. Checked outside `fileAfter`
  // because it reads filesystem metadata, not content, and must still run when the case has no
  // `fileAfter` block of its own.
  if (c.modeAfter !== undefined && c.rerun) {
    const mode = statSync(target).mode & 0o777;
    report(mode === c.modeAfter, `${c.name} -> ledger mode preserved`, `want 0${c.modeAfter.toString(8)}, got 0${mode.toString(8)}`);
  }
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

// ARMING CHECK for the fold case above, guarding a NARROWER thing than "the fixture is intact"
// -- named as three modes, because the case covers one of them and is blind to the other two:
//
//   U+0131 -> ASCII `i`      the case FAILS LOUDLY. The header then matches in both states, the
//                            row comes back backed, and all four assertions red. Redundant here.
//   U+0131 deleted           `**Unt 1` matches in NEITHER state, so the verdict is identical to
//                            a correct run and the case PASSES. This line is the only guard.
//   U+0131 -> a non-folding  same shape: no fold, no match, same verdict, case PASSES. Only
//     character              guard.
//
// The silent disarms are the ones that REMOVE the fold rather than complete it, and nothing
// inside the case can see them -- a disarmed vector and a correct vector produce byte-identical
// output. That is what this line is for, and it is not general fixture insurance.
//
// It pins the HEADER rather than the code point's presence anywhere in the file: the vector
// surviving in a comment while the header went ASCII would satisfy a bare presence test.
//
// SPELLED AS AN ESCAPE, never as the character: written literally, this assertion is
// vulnerable to the very pass it guards against -- normalize this file too and it would
// compare against ASCII `i`, which a disarmed fixture also contains, and pass.
const foldFixture = readFileSync(resolve(root, "tests/fixtures/unit-header-fold.md"), "utf8");
report(
  foldFixture.includes("**Un" + String.fromCharCode(0x131) + "t 1"),
  "fold fixture: U+0131 header vector intact (not normalized, deleted, or substituted)"
);
// Same guard, same blind spot, for the rules-heading fold vector: pins the HEADING, spelled as
// an escape, because a normalized fixture reads `## Rules of this ledger` in both runtimes and
// its case then fails on the wrong axis (both skip the section) instead of testing the fold.
const rulesFoldFixture = readFileSync(resolve(root, "tests/fixtures/rules-heading-fold.md"), "utf8");
report(
  rulesFoldFixture.includes("## Rule" + String.fromCharCode(0x17f) + " of this ledger"),
  "fold fixture: U+017F rules-heading vector intact (not normalized, deleted, or substituted)"
);
// And for the exit-code fold vector: pins the whole token, digit included, spelled as an escape.
const exitFoldFixture = readFileSync(resolve(root, "tests/fixtures/exit-code-fold.md"), "utf8");
report(
  exitFoldFixture.includes("ex" + String.fromCharCode(0x131) + "t 0"),
  "fold fixture: U+0131 exit-code vector intact (not normalized, deleted, or substituted)"
);

// ARMING CHECK for the three ALWAYS_TRUE pad vectors, load-bearing for the same reason as the
// fold guard above and with the same single blind spot. That case's expect strings hold U+001C
// as a RAW BYTE, exactly as its fixture does, so a normalization pass over the tree rewrites
// BOTH: the fixture's `echo<U+001C>ok` becomes `echook`, the expect string becomes `echook`,
// and `echook` is still a command that does not exist -- still exit 127, still a match. The
// case passes having tested nothing about `\s`, and nothing inside it can tell.
//
// Three assertions, one per SOURCE OCCURRENCE of `\s` in ALWAYS_TRUE, so a partial disarm names
// which vector died instead of reporting one vague failure.
const padFixture = readFileSync(resolve(root, "tests/fixtures/noop-pad-divergence.md"), "utf8");
for (const [vector, occurrence] of [
  [`echo${FS}ok`, "(?:\\s+[^&|;]*)?, reached through the verb alternation"],
  [`command${FS}true`, "command\\s+true"],
  [`exit${FS}0`, "exit\\s+0"],
]) {
  report(padFixture.includes(vector), `pad fixture: U+001C vector intact -- ${occurrence}`);
}

// STRESS: a 1 MB `Created:` line of digits and colons must be rejected (it is not a real
// stamp) in well under 2s, in both runtimes -- proving the extraction and grammar regexes have
// no catastrophic-backtracking path. Every quantifier on this path (`[\d:]+`/`[0-9:]+`, the
// optional fraction/offset groups) is a single, non-nested repetition, so a blow-up here would
// mean a REGRESSION in that shape, not an inherent property of "big input." Built as a
// standalone timed check, not a `cases` table row: the table has no timing budget field, and a
// fixed 2s bound is a property of THIS case, not something every other row should carry.
{
  const hugeCreated = "1:".repeat(500000) + "00Z"; // ~1 MB, never a valid stamp
  const dir = mkdtempSync(join(tmpdir(), "ledger-stress-"));
  const target = join(dir, "stress.md");
  writeFileSync(
    target,
    `# Delegation plan\n\nUnits: 1\nCreated: ${hugeCreated}\n\n` +
      "| # | Unit | Files (mine) | Worker | Acceptance | Status |\n" +
      "|---|------|--------------|--------|------------|--------|\n" +
      "| 1 | stats | app/a.py | worker-1 | measured by hand | verified |\n\n" +
      "## Evidence\n\n**Unit 1 —** ran the suite by hand.\n"
  );
  const startedAt = Date.now();
  let stressCode = null;
  try {
    execFileSync(runtimeBin, [...runtimeArgs, target], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 10000, // safety net only -- the real bound is the elapsedMs assertion below
      env: process.env,
    });
    stressCode = 0;
  } catch (err) {
    stressCode = err.status;
  }
  const elapsedMs = Date.now() - startedAt;
  report(stressCode === 1, "a 1 MB Created line is rejected as an invalid stamp, not accepted", `exit ${stressCode}, want 1`);
  report(elapsedMs < 2000, "a 1 MB Created line is rejected in well under 2s (no regex blow-up)", `${elapsedMs}ms`);
}

console.log(failed ? `${failed} failing` : "all pass");
process.exit(failed ? 1 : 0);
