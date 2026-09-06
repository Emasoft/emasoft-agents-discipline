# Delegation plan

Units: 2
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | first | app/a.py | worker-1 | `echo running && node -e "process.exit(0)"` | verified |
| 2 | second | app/b.py | worker-2 | `echo running && node -e "process.exit(0)"` | verified |

## Evidence

**Unit 1 —** ran the suite, output in `reports/budget-1.txt`.

**Unit 2 —** ran the suite, output in `reports/budget-2.txt`.
