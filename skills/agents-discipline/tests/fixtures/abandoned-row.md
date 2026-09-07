# Delegation plan
Units: 2

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | stats | app/stats.py | worker-1 | tests pass | verified |
| 2 | finance | app/finance.py | worker-2 | tests pass | abandoned |

## Evidence

**Unit 1 —** ran `pytest -q tests/stats.py`: 12 passed in 0.4s.

**Unit 2 —** abandoned. The upstream ledger API this unit needed was withdrawn, so the
work cannot be finished within the authorized task. No partial result is shippable.

Note the shape this fixture is built to prove: with evidence present and no `pending` or
`done` row, the abandonment is the ONLY thing between this ledger and `complete`. So the
closing line is TERMINAL rather than `complete` — not merely rather than `INCOMPLETE`.

The Acceptance cells are deliberately NOT runnable commands. `evidence: present` comes from
the `## Evidence` section above, never from the Acceptance column, so making these real
commands bought nothing — and it would have armed a landmine: this fixture names files that
do not exist here, so anyone later adding `rerun: true` would execute pytest against them.
Every existing `rerun: true` case is safe precisely because its acceptance cells contain no
runnable command, and a fixture that quietly breaks that invariant fails whenever someone
takes the suite at its word.
