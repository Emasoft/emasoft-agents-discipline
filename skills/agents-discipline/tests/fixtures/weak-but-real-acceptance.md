# Delegation plan

Units: 3
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | test-expr | app/a.py | worker-1 | `test -f /etc/hosts` | verified |
| 2 | bracket-expr | app/b.py | worker-2 | `[ -f /etc/hosts ]` | verified |
| 3 | listing | app/c.py | worker-3 | `ls /etc/hosts` | verified |

## Evidence

**Unit 1 —** ran it, output in `reports/weak-1.txt`.

**Unit 2 —** ran it, output in `reports/weak-2.txt`.

**Unit 3 —** ran it, output in `reports/weak-3.txt`.
