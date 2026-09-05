# Delegation plan

Units: <N>
Created: <ISO 8601, e.g. 2026-08-29T22:50:00+0200 — the checker dates artifacts against this>

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | <one line: what done looks like> | <paths, comma-separated> | worker-1 | <checkable: command / test / measure> | pending |
| 2 | <one line> | <paths> | worker-2 | <checkable> | pending |
| 3 | <one line> | <paths> | worker-3 | <checkable> | pending |
| ... | ... | ... | ... | ... | ... |

## Rules of this ledger

- One row per unit. No two rows share a file.
- Acceptance is checkable: a command, a test, a measurable criterion. "Works" is not acceptance.
- Status: `pending` → `done` (worker reported) → `verified` (coordinator checked it themselves).
- At creation EVERY row is `pending` and `## Evidence` is empty. Nothing has run yet; those are the only honest values.
- One criterion per row. A disjunction ("either X passes or the card explains why") is unfalsifiable and breaks the re-run.
- `node <skill-dir>/scripts/ledger-check.mjs <this file>` RUNS each verified row's acceptance command and appends a content-bound receipt. No receipt = never checked.
- No "done" for the task until every row is `verified`.

## Evidence

- Write one block per unit, headed `**Unit N —**`, each citing a real artifact path in backticks.
- Evidence is attributed per row: an artifact under Unit 1 does not back Unit 2.
