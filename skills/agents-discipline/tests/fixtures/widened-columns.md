# Delegation plan

Units: 2
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status | Gates | Attempts |
|---|------|--------------|--------|------------|--------|-------|----------|
| 1 | stats | app/a.py | worker-1 | `pytest -q tests/a.py` | verified | gates/leaf-1.md | 1/3 |
| 2 | finance | app/b.py | worker-2 | `pytest -q tests/b.py` | verified | gates/leaf-2.md | 0/3 |

## Evidence

**Unit 1 —** ran `pytest -q tests/a.py`, 12 tests passed.

**Unit 2 —** ran `pytest -q tests/b.py`, 8 tests passed.
