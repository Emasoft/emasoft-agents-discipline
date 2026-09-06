# Delegation plan

Units: 2
Created: 2020-01-01T00:00:00+0000

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | signature | src/a.rs | worker-1 | `cargo test -q sig` | verified |
| 2 | handler | src/b.rs | worker-2 | `cargo test -q handler` | verified |

## Evidence

**Unit 1 —** changed the signature to Vec<String>, 12 tests passed.

**Unit 2 —** the handler now returns Result<(), Error>, 8 tests passed.
