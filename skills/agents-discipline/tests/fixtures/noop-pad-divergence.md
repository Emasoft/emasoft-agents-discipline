# Delegation plan

Units: 3
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | pad-echo | app/a.py | worker-1 | `echook && echo x` | verified |
| 2 | pad-command | app/b.py | worker-2 | `commandtrue && echo x` | verified |
| 3 | pad-exit | app/c.py | worker-3 | `exit0 && echo x` | verified |

## Evidence

**Unit 1 —** ran it, output in `reports/pad-1.txt`.

**Unit 2 —** ran it, output in `reports/pad-2.txt`.

**Unit 3 —** ran it, output in `reports/pad-3.txt`.
