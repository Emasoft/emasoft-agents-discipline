# Gates: crlf

OWNS: src/a/**

- [x] g1: done already
  CHECK: echo one
  CHECK: echo two
  CWD: packages/x
  EXPECT: one
  EVIDENCE: pending

- [ ] g2: live
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending

ABANDON: g2 the reason text
