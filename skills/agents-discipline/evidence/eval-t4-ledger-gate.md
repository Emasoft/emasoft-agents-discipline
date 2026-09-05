# t4-toolkit: delegation gate eval (completed)

## Design

Task: implement 12 independent stubbed Python modules (stats, finance, strings, dates, files, network, validation, conversion, hashing, formatting, collections, environment) so a 93-test suite passes. Genuinely large parallel job: 12 independent units, 12+ files.

Variants compared (8 candidates, blind labels V1 to V8):
- **a-baseline** (4 candidates): willow, spruce, birch2, acacia: current ~/.cursor/rules only
- **e-gate** (4 candidates): elm2, oak2, maple2, juniper2: baseline + structural delegation gate rule

The gate rule (`harness/delegation-gate.mdc`) differs from the failed prose delegation rule in three ways:
1. **Threshold**: only fires on 3+ independent units / 5+ files / 30+ min. Below it, single-agent is explicitly correct.
2. **Mandatory artifact**: first artifact must be `DELEGATION.md` (ledger: one row per unit, non-overlapping file ownership, acceptance, status).
3. **Report gate**: final report without a complete verified ledger is a failed report.

## Results

| Candidate | Variant | Subagent spawns | DELEGATION.md | Tests | Ledger verified |
|---|---|---|---|---|---|
| willow | a-baseline | 0 | no | 93/93 | — |
| spruce | a-baseline | 0 | no | 93/93 | — |
| birch2 | a-baseline | 0 | no | 93/93 | — |
| acacia | a-baseline | 0 | no | 93/93 | — |
| elm2 | e-gate | 12 | yes | 93/93 | 12/12 |
| oak2 | e-gate | 12 | yes | 93/93 | 13/13 (incl. NOTE) |
| maple2 | e-gate | 12 | yes | 93/93 | 12/12 |
| juniper2 | e-gate | 12 | yes | 93/93 | 12/12 |

## Findings

1. **The gate caused delegation, 100% adoption.** All 4 gate candidates wrote `DELEGATION.md` first, split into 12 units, spawned one subagent per module (in parallel), and verified each unit themselves. All 4 baseline candidates did the work themselves, 0 spawns, matching every prior eval.
2. **Prior evals: 0/12 candidates delegated** across 3 tasks with prose rules and even an explicit skill instruction ("spin up a background agent"). This eval: 4/4 delegated with a structural gate. The mechanism, not the wording, is what changed behavior.
3. **Quality was identical, and high.** All 8 candidates passed 93/93. Delegation didn't trade quality for parallelism; the orchestrators verified every unit by reading the code and running the suite.
4. **The gate scaled correctly.** Its threshold prevented force-splitting on small tasks (not tested here but encoded) while making the large task unmistakably a split job. Gate candidates correctly identified "12 independent units, 12 files → gate open."
5. **Ledger fidelity was real, not theater.** Chain evidence shows orchestrators ran the suite after workers, read every module, updated status to verified, and reported what they verified. Workers got per-module briefs with owned-file constraints and were told not to touch other files or the tests.

## Conclusion

The structural delegation gate is the first intervention in this eval program that changes delegation behavior. It works. Recommended: promote `delegation-gate.mdc` to `~/.cursor/rules/`, with the threshold wording kept so it only fires on genuinely parallelizable work.

## Caveat

n=4 per variant; results are consistent and directionally strong but not statistical proof. The mechanism's success is plausible from the design (artifact + ledger + report gate turn advice into a checkable procedure), not just from this sample.
