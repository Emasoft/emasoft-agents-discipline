# The eval

The delegation half is a measured result, not a guess. This is the writeup of the evaluation that produced it. The decisive round's raw results are vendored in this repo under [evidence/eval-t4-ledger-gate.md](../evidence/eval-t4-ledger-gate.md): the design, the 8 candidates, and the per-candidate spawn counts, test results, and ledger state. The full transcript evidence lived in the author's local harness at `~/agent-ops/evidence/` (task fixtures, sanitized per-candidate work directories, transcripts, chain evidence, blind judge packages) and is summarized here; the numbers below are directly checkable against the vendored table.

## The question

Two behaviors were observed in production agents and hypothesized as rule-fixable: agents don't use skills, and agents don't delegate. The second is the one the delegation half targets. The eval asked: **can delegation be induced by rules in the agent's context, and if not, what structural mechanism changes the behavior?**

## Round 1: rules in context do not induce delegation

12 candidate agents, 3 organic tasks (a bug-fix, a feature add, a research brief), 4 rule variants each:

- a-baseline: the standard rule set
- b-delegation: baseline + a detailed "Delegation" rule (fan-out math, unit contracts, coordination, scale guidance)
- c-skill: baseline + a "Skill Loading" rule (mandatory SKILL.md read)
- d-both: both

Tasks were presented as organic work in sanitized directories, no labels, no "test" framing, so agents would not behave differently under observation. Objective chain evidence was extracted from each transcript: subagent spawns (Task-tool uses), skill-file reads, worktree/branch commands, test results.

**Result:**

| Variant | Subagent spawns |
|---|---|
| all 4 variants | **0 across all 12 candidates** |

Not one candidate spawned a subagent. Not one created a worktree or branch. Quality was uniformly high (all coding candidates passed their suites, blind judges scored task success 4-5/5, verification honesty 5/5), but delegation was uniformly absent.

The strongest control in this round: the `research` skill, which literally instructs "spin up a background agent to do the research, so you keep working while it reads." All four research-task candidates read the skill. None of them did what it said. **An explicit instruction to delegate, in a skill the agent read in full, produced zero delegation.**

Verdict: prose, rules, skills, descriptions, cannot compel delegation. Advice is read and ignored.

## Round 2: structure induces it

Design principle from Round 1: prose cannot enforce prose. A model that under-executes instructions also under-executes the instruction not to under-execute. So the intervention had to be **structural**: a gate that delegation is the only way to satisfy.

The gate has three parts:

1. **Threshold.** 3+ independent units, 5+ files, or 30+ minutes → the gate opens. Below it, single-agent is explicitly correct. This prevents the opposite failure (force-splitting small tasks).
2. **Mandatory ledger.** If the gate is open, the FIRST artifact written must be `DELEGATION.md`: one row per unit, non-overlapping file ownership, checkable acceptance, status column. Written to disk before any artifact work.
3. **Report gate.** A final report without a complete verified ledger is a failed report. No "done" until every row is verified.

Tested on a genuinely parallel task: a Python library with 12 independent stubbed modules and a 93-test suite. The fixture was validated both ways before the run, 0/93 on the stubs, 93/93 against a reference implementation, so any candidate failure was a real implementation gap, not a broken test.

8 candidates, 2 conditions:

| Condition | Candidates | Wrote DELEGATION.md | Spawned subagents | Tests |
|---|---|---|---|---|
| baseline | willow, spruce, birch2, acacia | 0 / 4 | 0 each | 93/93 all |
| **ledger rules** | elm2, oak2, maple2, juniper2 | **4 / 4** | **12 each, in parallel** | **93/93 all** |

Every ledger-rule candidate: wrote the ledger first, split into 12 units with non-overlapping file ownership, spawned 12 parallel subagents (one per module, each with a goal/scope/acceptance/verify brief), then verified every unit itself, read each module, ran the suite, updated the ledger to `verified`, and pasted the ledger in the final report.

Every baseline candidate: implemented all 12 modules in one thread, zero spawns, matching every Round-1 candidate.

**Quality was identical across conditions.** Delegation did not trade correctness for parallelism. The orchestrators' verification was real, not theater: chain evidence shows suite runs after worker completion and per-module reads.

## The numbers

- Round 1: **0 / 12** candidates delegated (prose rules).
- Round 2: **4 / 4** candidates delegated with the ledger gate, **0 / 4** without.
- Test quality: **93/93** for every candidate in both conditions.
- Ledger fidelity: 4/4 ledger-rule candidates produced a complete, verified, pasted-in-report ledger.

## The lesson

**Rules that describe delegation do nothing. A rule that requires a ledger changes behavior 4/4 vs 0/12.**

The "4/4" is round 2's gate condition and the "0/12" is round 1's prose-rule candidates, across different tasks; the within-round contrast (4/4 vs 0/4, same task) is the controlled comparison, and the round-1 number shows the same behavior under prose rules in the round that tested prose. See [evidence/eval-t4-ledger-gate.md](../evidence/eval-t4-ledger-gate.md) for the round-2 table.

The mechanism works because it moves enforcement out of goodwill and into files and checks, the same move the completion half makes for completion, applied to decomposition. That is the entire intellectual content of the delegation half, and the entire reason it is structured the way it is.

## Limitations

- n=4 per condition on the decisive test. Direction is strong and mechanism-consistent but not statistical proof; a larger sample or a second large task would harden it.
- The eval used the subagent/Task tool available in the author's harness. The ledger itself is harness-agnostic; the spawn mechanism varies by agent.
- Small-task behavior (gate closed) was not stress-tested; the threshold encodes the orchestrate lesson but has not been adversarially evaluated for false-opens or false-closes at the boundary.
