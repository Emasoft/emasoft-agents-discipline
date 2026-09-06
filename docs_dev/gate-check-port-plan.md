# gate-check port — next steps (written before auto-compact, 2026-09-06)

State: 8 of 9 scripts ported. HEAD = e1bc43a. All green:
`npm test` 168 PASS; `AD_RUNTIME=python` → ledger 102/102, lint 29/29, dispatch 21/21.

## Remaining work, in the order the reviews established

1. **`gateDefinitionDigest` FIRST, not last.** It is a sha256 over a canonical form and it
   backs every approval token. A one-byte difference in the canonical form means an approval
   written by `gate-check.mjs` is rejected by `gate_check.py` — and the symptom is
   "gate not approved", which reads as a workflow problem, not a port bug. Port it, then a
   byte-equality driver row against the oracle over a matrix of gates, BEFORE anything
   depends on it.
2. **`classifyGateEvidence` + `automaticEvidencePrefix` + `MAX_AUTOMATIC_EVIDENCE_CHARS`
   (900)** as one unit. That bound is applied with `.length`/`.slice()` in the oracle, so it
   needs `js_length`/`js_slice` — and evidence is written INTO the ledger file, so a mismatch
   produces two different ledger bytes, not just two different verdicts. Drive with a non-BMP
   evidence string.
3. **`globsOverlap` + `literalPrefix`** — the lease-overlap guarantee, whose failure was
   already measured as SILENT (a placeholder OWNS and a real glob were disjoint, so two
   workers both claimed successfully). Add a SYMMETRY row: `globsOverlap(a,b) ==
   globsOverlap(b,a)` over a ~15-glob matrix. Asymmetry is exactly what a hand-transcribed
   early-return loop introduces.
4. **`formatDocument`** — the only WRITER of ledger bytes. Add a round-trip row (parse →
   format → assert byte-identical) across all 8 port fixtures BEFORE it is needed; a wrong
   `eol` join silently converts a CRLF ledger to LF for every future reader.
5. Then the rest of `gate-check.mjs` (950 lines): `resolveTarget`, `listScopes`, `scopeFiles`,
   `readLeases`/`claimLeases`/`releaseLeases`, `gateState`, `qualify`, `tail`,
   `statusLogPath`, `MAX_CHECK_OUTPUT_BYTES`.

## Known-uncovered, stated rather than buried

- `with_file_lock` CONTENTION — zero coverage in either runtime; needs two processes.
  Highest-traffic untested path; `update_dispatch` takes it on every call.
- The mid-read replacement guard in `read_stable_regular_file` has no Python-side coverage:
  `dispatch-tests.mjs` injects it with a Node `fs.lstatSync` preload that cannot drive a
  Python process (skipped under AD_RUNTIME=python, with the reason in the test).
- `tail(output, max=240)` uses `.slice()` and a JS `trim()` — needs `js_slice`, and JS/Python
  whitespace differ on U+FEFF (measured; the only reachable disagreement).

## Conventions this port has established — keep them

- Hold the oracle FIXED; a divergence is a porting defect, never a re-specified test.
- Every differential check pairs with a control PROVING it can fail, asserted of BOTH sides.
- Mutation-control every new assertion: revert the fix, confirm THAT row reddens.
  (Caught three of my own rows testing the wrong code path this session.)
- Declared divergences are recorded in the compared output, never stripped to make the
  diff agree.
