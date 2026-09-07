# Delegation plan
Units: 2

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | stats | app/stats.py | worker-1 | `pytest -q tests/stats.py` | verified |
| 2 | finance | app/finance.py | worker-2 | `pytest -q tests/finance.py` | abandoned |

## Evidence

**Unit 1 —** ran `pytest -q tests/stats.py`: 12 passed in 0.4s.

**Unit 2 —** abandoned. The upstream ledger API this unit needed was withdrawn, so the
work cannot be finished within the authorized task. No partial result is shippable.

Note the shape this fixture is built to prove: with evidence present and no `pending` or
`done` row, the abandonment is the ONLY thing between this ledger and `complete`. So the
closing line is TERMINAL rather than `complete` — not merely rather than `INCOMPLETE`.
