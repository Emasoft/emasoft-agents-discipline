# Delegation plan
Units: 1

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | stats | app/stats.py | worker-1 | tests pass | verified |

- Ran `node test/run-tests.mjs`: all pass

## Rules of this ledger

- Each unit gets exactly one row. File sets never overlap.
- Acceptance must be checkable. "Works" is not acceptance.
- Status transitions: pending to done to verified.
- No completion until every row is verified.
