# Delegation plan
Units: 3

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | stats | app/stats.py | worker-1 | tests pass | verified |
| 2 | finance | app/finance.py | worker-2 | tests pass | abandoned |
| 3 | strings | app/strings.py | worker-3 | tests pass | done |

Verified unit 1 myself.

Unit 2 is abandoned: the upstream ledger API it needed was withdrawn, so the unit
cannot be finished within this task. That is terminal, not pending — the checker
must report a required handoff rather than an indefinitely incomplete ledger.
