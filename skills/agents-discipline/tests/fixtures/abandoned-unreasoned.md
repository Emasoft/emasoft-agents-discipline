# Delegation plan

Units: 3

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | alpha | app/alpha.py | worker-1 | tests pass | abandoned |
| 2 | beta | app/beta.py | worker-2 | tests pass | abandoned |
| 3 | gamma | app/gamma.py | worker-3 | tests pass | abandoned |

## Evidence

**Unit 2 —**

**Unit 3 —** abandoned. The vendor endpoint this unit wrapped was retired with no
replacement, so the work cannot be finished under the authorized scope.

THREE rows, and the shape is the whole assertion. Each row is a DIFFERENT way of failing
to record a reason, and the reasoned row sits LAST:

- **#1 has no `**Unit 1 —**` header at all** — nothing was written.
- **#2 has a header and nothing under it.** This is the shape that matters, because the
  block a header-only unit produces is NOT EMPTY: the scanner appends the header line
  itself to its own block. A check that tested the block for emptiness passed #2 silently,
  so anyone wanting the marker gone could type six characters and have it. That is the
  cheapest possible defeat of a check, and it is why the predicate strips the marker and
  tests what remains rather than testing the block.
- **#3 carries a real reason**, and is the positive control: a marker on every abandoned
  row would mean the check reads nothing, and a suite where the marker is always present
  cannot tell a working check from a constant.

The reasoned row is LAST rather than in the middle, and that placement is load-bearing.
With it at #2 the fixture was passable by `unit % 2 === 1` — mark the odd units, skip the
even one, every assertion green. "Middle", "even" and "not-first-not-last" are the same
set in a 3-row fixture, so that shape could not tell them apart. At #3 a parity rule has
to mark #2, which the `reject` forbids.

The Acceptance cells are deliberately NOT runnable commands, for the reason
`abandoned-row.md` records: this fixture names files that do not exist, so anyone later
adding `rerun: true` would execute a real command against them. No case here needs the
re-run loop — the abandoned listing sits outside it.

Note the wording being gated. The marker says no reason was found IN A `**Unit N**` BLOCK,
which is the only place it looked. It does not claim no reason was written: a pooled
paragraph covering all three units, a `#1`-style header, or a header indented inside a list
all carry a real reason this check cannot see. Claiming absence would put a false
accusation in the ledger's own output against an author who did the right thing.
