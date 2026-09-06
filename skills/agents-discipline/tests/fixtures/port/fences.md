# Gates: fence

```
text
`
- [ ] sneaky1: a single-backtick line must not close a ``` fence
```

~~~
text
```
- [ ] sneaky2: a mismatched fence character must not close it either
~~~

```a`b
- [ ] sneaky3: a backtick in the info string means NO fence opened, so this IS a gate
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending
```
inside the fence that line opened
```

- [ ] real: a genuine gate, outside every fence
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending
