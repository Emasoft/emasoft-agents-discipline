# Delegation plan
Units: 3

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | stats | app/stats.py | worker-1 | tests pass | verified |
| 2 | finance | app/finance.py | worker-2 | tests pass | abandoned |
| 3 | strings | app/strings.py | worker-3 | tests pass | pending |

Verified unit 1 myself.

Unit 2 is abandoned: the upstream ledger API it needed was withdrawn. Unit 3 has not
been started. Both facts must survive into the closing line — a ledger that is terminal
AND still has unfinished work is not described by either fact alone.
