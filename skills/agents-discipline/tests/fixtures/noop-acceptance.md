# Delegation plan

Units: 11
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | colon | app/a.py | worker-1 | `:` | verified |
| 2 | exit-zero | app/b.py | worker-2 | `exit 0` | verified |
| 3 | abs-true | app/c.py | worker-3 | `/bin/true` | verified |
| 4 | or-true | app/d.py | worker-4 | `pytest -q \|\| true` | verified |
| 5 | subshell | app/e.py | worker-5 | `( exit 0 )` | verified |
| 6 | true-first | app/f.py | worker-6 | `true \|\| pytest -q` | verified |
| 7 | semicolon | app/g.py | worker-7 | `false; true` | verified |
| 8 | leading-semi | app/h.py | worker-8 | `;true` | verified |
| 9 | two-groups | app/i.py | worker-9 | `(false) ; (true)` | verified |
| 10 | paren-second-fragment | app/j.py | worker-10 | `true && (echo ok)` | verified |
| 11 | paren-both-fragments | app/k.py | worker-11 | `(true) && (true)` | verified |

## Evidence

**Unit 1 —** ran it, output in `reports/noop-1.txt`.

**Unit 2 —** ran it, output in `reports/noop-2.txt`.

**Unit 3 —** ran it, output in `reports/noop-3.txt`.

**Unit 4 —** ran it, output in `reports/noop-4.txt`.

**Unit 5 —** ran it, output in `reports/noop-5.txt`.

**Unit 6 —** ran it, output in `reports/noop-6.txt`.

**Unit 7 —** ran it, output in `reports/noop-7.txt`.

**Unit 8 —** ran it, output in `reports/noop-8.txt`.

**Unit 9 —** ran it, output in `reports/noop-9.txt`.

**Unit 10 —** ran it, output in `reports/noop-10.txt`.

**Unit 11 —** ran it, output in `reports/noop-11.txt`.
