# Delegation plan

Units: 5
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | colon | app/a.py | worker-1 | `:` | verified |
| 2 | exit-zero | app/b.py | worker-2 | `exit 0` | verified |
| 3 | abs-true | app/c.py | worker-3 | `/bin/true` | verified |
| 4 | test-expr | app/d.py | worker-4 | `[ -f package.json ]` | verified |
| 5 | listing | app/e.py | worker-5 | `ls` | verified |

## Evidence

**Unit 1 —** ran it, output in `reports/noop-1.txt`.

**Unit 2 —** ran it, output in `reports/noop-2.txt`.

**Unit 3 —** ran it, output in `reports/noop-3.txt`.

**Unit 4 —** ran it, output in `reports/noop-4.txt`.

**Unit 5 —** ran it, output in `reports/noop-5.txt`.
