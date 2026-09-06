# Gates: warn

- [ ] g1: a path-shaped EXPECT read as a regex
  CHECK: echo src/a/b
  EXPECT: /src/a/b/
  EVIDENCE: pending

- [ ] g2: an escaped one, which must NOT warn
  CHECK: echo ok
  EXPECT: /a\/b/
  EVIDENCE: pending

```
- [ ] not-a-gate: inside a fence
  CHECK: nope
```
