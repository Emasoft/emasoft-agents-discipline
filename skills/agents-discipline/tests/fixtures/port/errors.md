# Gates: errors

CHECK: orphaned attribute with no gate above it

- [ ] : no id at all

- [ ] ok-gate: a VALID gate, so the unconditional zero-live-gates error cannot fire
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending

ABANDON: ghost-never-declared
