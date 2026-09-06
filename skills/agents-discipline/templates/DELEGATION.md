# Delegation plan

Units: <N>
Created: <ISO 8601, e.g. 2026-08-29T22:50:00+0200 — the checker dates artifacts against this>

| # | Unit | Files (mine) | Worker | Acceptance | Status | Gates | Attempts |
|---|------|--------------|--------|------------|--------|-------|----------|
| 1 | <one line: what done looks like> | <paths, comma-separated> | worker-1 | `<command>` | pending | <leaf ledger path> | 0/3 |
| 2 | <one line> | <paths> | worker-2 | `<command>` | pending | <leaf ledger path> | 0/3 |
| 3 | <one line> | <paths> | worker-3 | `<command>` | pending | <leaf ledger path> | 0/3 |
| ... | ... | ... | ... | ... | ... | ... | ... |

## Rules of this ledger

- One row per unit. No two rows share a file.
- **Acceptance must be a runnable command inside a code span** — `pytest -q tests/stats.py`, not
  "tests pass". The checker extracts and re-runs only backticked commands containing whitespace;
  bare prose is never re-run, so a row backed by prose alone is unverified by construction.
- **No `||` and no `;` in an acceptance.** Both let a chain report success whatever the code
  did — `pytest -q || true` and `false; true` are green always — so the checker refuses them
  outright rather than guessing which are honest. `&&` is fine: every link has to succeed, so
  `cd packages/x && pytest -q` is exactly as strong as `pytest -q`. Need real sequencing? Put
  it in a script and name the script.
- Escape any literal `\|` inside a cell. An unescaped pipe adds a column and shifts every later
  cell, so `Status` would be read from the wrong place.
- Status: `pending` → `done` (worker reported) → `verified` (coordinator checked it themselves).
  `abandoned` is terminal-but-unsuccessful: the unit cannot be finished, the reason is written in
  its Evidence block, and the ledger reports a required handoff rather than completion.
- At creation EVERY row is `pending` and `## Evidence` is empty. Nothing has run yet; those are
  the only honest values.
- One criterion per row. A disjunction ("either X passes or the card explains why") is
  unfalsifiable and breaks the re-run.
- `Gates` names the leaf gate ledger for that row; `Attempts` counts re-dispatches against a cap.
  The cap is advisory — nothing enforces it but you, so do not treat it as a guarantee.
- `node <skill-dir>/scripts/ledger-check.mjs <this file>` RUNS each verified row's acceptance
  command and appends a content-bound receipt. No receipt = never checked.
- No "done" for the task until every row is `verified`.

### How to write the Evidence section

- One block per unit. Head each block at the START of a line with the unit marker — two
  asterisks, the word Unit, the number, an em dash — then say what you ran and saw.
- Cite the artifact you produced as a **bare path in a code span**: a citation must be a path,
  not a command. A span containing a command is not read as a citation.
- Evidence is attributed per row: an artifact under one unit does not back another.

## Evidence
