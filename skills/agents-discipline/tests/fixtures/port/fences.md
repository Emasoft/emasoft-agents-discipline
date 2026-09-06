# Gates: fence

```
text
`
- [ ] sneaky1: after a single-backtick line
```

~~~
text
```
- [ ] sneaky2: after a mismatched closer
~~~

```a`b
- [ ] sneaky3: a backtick in the info string means no fence opened
```

- [ ] real: a genuine gate
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending
