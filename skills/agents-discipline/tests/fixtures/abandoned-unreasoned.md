# Delegation plan

Units: 3

| # | Unit | Files (mine) | Worker | Acceptance | Status |
|---|------|--------------|--------|------------|--------|
| 1 | alpha | app/alpha.py | worker-1 | tests pass | abandoned |
| 2 | beta | app/beta.py | worker-2 | tests pass | abandoned |
| 3 | gamma | app/gamma.py | worker-3 | tests pass | abandoned |

## Evidence

**Unit 2 —** abandoned. The vendor endpoint this unit wrapped was retired with no
replacement, so the work cannot be finished under the authorized scope.

THREE rows, and the shape is the assertion. Unit 2 is the only one carrying an
attributed block, and it sits in the MIDDLE: a check keyed on row position, or on
"the first row", or on "any evidence block exists anywhere in the file", passes a
two-row fixture by accident and fails this one. Two unreasoned rows on either side
of one reasoned row is the smallest shape that kills all three families at once.

Unit 2's block is also the positive control. If the marker appeared on every
abandoned row the check would not be reading anything, and a suite where the marker
is always present cannot tell a working check from a constant.

The Acceptance cells are deliberately NOT runnable commands, for the reason
`abandoned-row.md` records: this fixture names files that do not exist, so anyone
later adding `rerun: true` would execute a real command against them. No case here
needs the re-run loop — the abandoned listing sits outside it.

Note the wording being gated. The marker says the evidence block is not
attributable, NOT that no reason was written. Those differ: a pooled paragraph
covering all three units, a `#1`-style header, or a header indented inside a list
all carry a real reason this check cannot attribute. The checker must report what it
measured, because the ledger's own output is what a human reads.
