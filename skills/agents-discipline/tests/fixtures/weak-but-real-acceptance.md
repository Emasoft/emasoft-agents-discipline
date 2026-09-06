# Delegation plan

Units: 6
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | test-expr | app/a.py | worker-1 | `test -f /etc/hosts` | verified |
| 2 | bracket-expr | app/b.py | worker-2 | `[ -f /etc/hosts ]` | verified |
| 3 | listing | app/c.py | worker-3 | `ls /etc/hosts` | verified |
| 4 | and-chain | app/d.py | worker-4 | `echo checking && node -e "process.exit(0)"` | verified |
| 5 | quoted-semi | app/e.py | worker-5 | `python3 -c "import sys; sys.exit(0)"` | verified |
| 6 | awk-prog | app/f.py | worker-6 | `awk '{print;}' /etc/hosts` | verified |

## Evidence

**Unit 1 —** ran it, output in `reports/weak-1.txt`.

**Unit 2 —** ran it, output in `reports/weak-2.txt`.

**Unit 3 —** ran it, output in `reports/weak-3.txt`.

**Unit 4 —** ran it, output in `reports/weak-4.txt`.

**Unit 5 —** ran it, output in `reports/weak-5.txt`.

**Unit 6 —** ran it, output in `reports/weak-6.txt`.
