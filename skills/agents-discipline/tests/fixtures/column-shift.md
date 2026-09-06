# Delegation plan

Units: 2
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | ok | app/a.py | worker-1 | `pytest -q tests/a.py` | verified |
| 2 | shifted | app/b.py | worker-2 | `grep -c x f | wc -l` | verified |

## Evidence

**Unit 1 —** ran the suite, output in `reports/shift-1.txt`.

**Unit 2 —** ran the suite, output in `reports/shift-2.txt`.
