# emasoft-agents-discipline

<!--BADGES-START-->
<!--BADGES-END-->

**One ant is a bug. A team ships.**
**Completion discipline for substantial AI-agent work, backed by runnable gates.**

Your agent keeps doing ten jobs in one thread when it could fan out into ten fresh workers, and the work it does keep lands 80% done. This skill makes the split mandatory when the work calls for it and makes every unit finish against a ledger you can check with a command.

> **You do not have to do the work. You have to make sure the work gets done.**

## Why one skill

The agent that stops at 80% and the agent that never starts a team are two different failure modes. One half of this skill fixes the first: it splits a task and gives every leaf finish-line discipline, so nothing lands half-done. The other half fixes the second: it makes there be workers at all, owned by fresh subagents, coordinated through a ledger.

Together they close both failure modes of long-horizon agent work: the agent that never splits and the split work that never lands complete. Run the tree with the completion half, run the fan-out with the delegation half, keep the ledger either way.

The delegation half's quality comes from two properties that prose delegation never had: fresh context per unit and non-overlapping ownership.

The lone-agent failure is not a *time* problem, it is an *attention* problem. A single thread that carries ten units gets ten units' worth of drift and compression. A fresh subagent per unit starts with full attention on exactly one job, a narrow brief, and a clear acceptance line.

This is the same mechanism the completion half identifies for its orchestrated mode ("attention, not time, was always the scarce resource"), applied at the task level rather than the leaf level. The two halves are orthogonal: the completion half makes each leaf finish; the delegation half makes the leaves exist, owned by workers, coordinated through a ledger.

## Installation

```text
claude plugin marketplace add Emasoft/emasoft-plugins
claude plugin marketplace update emasoft-plugins
claude plugin install emasoft-agents-discipline@emasoft-plugins
```

Restart Claude Code after installing. Invoke it as `/agents-discipline` (or `/emasoft-agents-discipline:agents-discipline`), or let it trigger on the work itself. The core is `skills/agents-discipline/SKILL.md`; the checkers and the optional Stop hook require Node 16 or newer and use no third-party runtime packages.

## Usage

Invoke it in plain language, or let it trigger on the work itself:

```text
/agents-discipline implement the 12 modules
```

```text
split the migration into workers, one per shard
```

```text
delegate this to subagents, one per feature
```

When the gate is open (3+ independent units, 5+ files, or 30+ minutes), the agent writes `DELEGATION.md` before any artifact work, spawns one subagent per unit, verifies each unit itself, and refuses to report done against a partial ledger.

```text
/agents-discipline tree 5 refactor the payment module and verify every migration path
```

For a solo task, copy [templates/gates-leaf.md](skills/agents-discipline/templates/gates-leaf.md) to `GATES.md`, replace every placeholder, and inspect it without executing commands:

```text
node <path-to-skill>/scripts/gate-check.mjs --status GATES.md
```

`--status` is the only mode that is always non-executing. On a new oracle with no exact approval record, a normal run prints its resolved command, expectation, working directory, shell, and `PATH` without executing it:

```text
node <path-to-skill>/scripts/gate-check.mjs GATES.md
```

Do not treat normal mode as a permanent dry run: once the exact oracle is approved, normal mode can execute it.

`CHECK:` lines are shell code. After reading every command and called script, approve and run the ledger:

```text
node <path-to-skill>/scripts/gate-check.mjs --approve GATES.md
```

Re-run all runnable gates, including gates already marked complete:

```text
node <path-to-skill>/scripts/gate-check.mjs --reverify GATES.md
```

Use `--help` for the complete current CLI.

## Why it works

Two identical tasks, eight identical agents. Four got this skill's rules. Four got nothing.

| Condition | Wrote DELEGATION.md | Spawned subagents | Tests passed |
|---|---|---|---|
| baseline | 0 / 4 | 0 each | 93 / 93 |
| **ledger rules** | **4 / 4** | **12 each, in parallel** | **93 / 93** |

The prose version of this idea, a rule that says "delegate!", was tested first and changed nothing: 0 of 12 agents delegated, across 3 tasks, even when a skill they read literally said "spin up a background agent." They read it and did the work themselves.

The difference is structure. This skill does not ask the agent to delegate. It makes delegation the only way to satisfy a checkable artifact: the ledger must be written first, the rows must be filled by workers, the verification must be done by the coordinator, and a report that skips the ledger is a failed report. **Advice is a suggestion. A ledger is a contract.**

## The delegation method

1. **Count the units before any work.** Independent pieces of work, files touched, time. 3+ units, 5+ files, or 30+ minutes → the gate is open.
2. **Write `DELEGATION.md` first.** One row per unit, non-overlapping file ownership, checkable acceptance per row. This is the contract, fixed before fan-out.
3. **Spawn one subagent per unit, in parallel.** Each gets a written brief: goal, owned files, context pasted in full, acceptance, verify commands, worktree/branch isolation.
4. **Verify every unit yourself.** A worker report is a self-report. Run its checks, read its artifact, update the ledger to `verified`.
5. **Report gate.** No "done" without a complete verified ledger. The report pastes the ledger, N of N, with what you verified and the evidence.

Full method: [references/delegation-method.md](skills/agents-discipline/references/delegation-method.md) · orchestration and isolation: [references/delegation-orchestration.md](skills/agents-discipline/references/delegation-orchestration.md) · cost and thresholds: [references/delegation-token-economy.md](skills/agents-discipline/references/delegation-token-economy.md)

## The gate contract

```markdown
# Gates: pricing behavior

- [ ] G1: pricing fixtures render the expected tiers
  CHECK: node scripts/verify-pricing.mjs
  EXPECT: pricing verification passed
  EVIDENCE: pending

- [ ] G2: checkout integration succeeds from its package
  CHECK: node scripts/verify-checkout.mjs
  EXPECT: checkout verification passed
  CWD: packages/checkout
  EVIDENCE: pending
```

A runnable gate passes only when its process exits `0` and `EXPECT:` matches combined output. Both the captured stdout/stderr payload and the canonical UTF-8 combined string used by `EXPECT:` and its fingerprint must fit the 1 MiB limit; the checker never truncates a larger matcher string into success. Canonical automatic evidence begins with a versioned full SHA-256 digest of the parsed `CHECK:`, `EXPECT:`, and raw `CWD:` definition, followed by the exit and successful-output fingerprint before capped environment details. A checked runnable gate with missing, ordinary prose, legacy, malformed, or definition-mismatched evidence is stale and unmet. Existing manual gates with ordinary human evidence remain compatible. This unkeyed binding detects structural drift, not ledger tampering: anyone who can edit a ledger can forge canonical-looking evidence. `--status` and Stop detect definition drift without resolving a shell or executing a check, but old evidence is not re-execution; parent verification uses `--reverify`.

The parser rejects zero-gate ledgers, duplicate ids, incomplete runnable gates, invalid expectations, and abandonment with a missing reason or unknown gate id. It ignores fenced examples, preserves CRLF or LF when updating, and inserts a missing evidence line when needed. A valid abandonment is terminal handoff rather than success: the checker exits `1` with `HANDOFF REQUIRED`, and Stop allows exit while reporting qualified ids.

The checker can prove only the command oracle you declare. It cannot infer that an English title and arbitrary shell code mean the same thing. Good gates therefore:

- read the artifact or service named by the outcome
- print a success-only marker after all assertions pass
- test an absence check against a known positive control
- measure supplied figures instead of copying them into `EXPECT:`
- review consequential manual outcomes with evidence proportional to risk

Use the advisory, non-executing [gate-lint.mjs](skills/agents-discipline/scripts/gate-lint.mjs) to catch mechanically weak ledger patterns; add `--strict` when warnings should fail. Full specification: [references/gates.md](skills/agents-discipline/references/gates.md).

## Shell and PATH

The checker uses `--shell` first, then `AGENTS_DISCIPLINE_SHELL`, then Node's platform default shell. That default is `/bin/sh` on Unix and `process.env.ComSpec` on Windows with the platform fallback. Checks inherit the launch environment, including `PATH`.

This matters on Windows: a checker launched from Git Bash can see Unix-like tools that the same checker launched from PowerShell does not. `--shell` changes the interpreter; it does not install `grep`, `tail`, `tr`, or other external programs. Portable examples call repository-owned Node scripts.

Parent re-verification should use the same declared shell and required toolchain. A shell or PATH mismatch is a failed verification to resolve, not successful evidence.

## Security boundary

Approval records live under `~/.agents-discipline/approved` by default. `AGENTS_DISCIPLINE_APPROVAL_DIR` may select another owner-private real directory, but its canonical target must remain outside the checked repository. Symlinked stores and linked, replaced, or non-private records fail closed. Each record is specific to the absolute ledger and gate, exact `CHECK:` and `EXPECT:`, resolved `CWD:` and shell, timeout, output and regex limits, regex worker limits, platform, and full inherited `PATH`. Editing any bound input requires approval again.

Approval is consent, not a sandbox. Approval storage is a canonical, owner-private directory outside the repository; records are accepted only as single-link private regular files. The environment-independent definition digest is separate from the runtime approval identity, which also binds resolved ledger/runtime context. Approval does not hash called scripts, fixtures, dependencies, or other transitive inputs. `--status` and Stop validate the recorded definition binding but do not inspect those artifacts; reinspect changed dependencies and run `--reverify`. See [SECURITY.md](skills/agents-discipline/SECURITY.md) for the bounded digest pattern when user-designed dependency identity is needed. Checks run with ambient filesystem, environment, credential, and network access. Scopes and ownership leases coordinate cooperating processes but do not restrict what a process can read or write.

## Orchestration and parallel work

For work that needs fresh contexts, create one scoped pipeline under `.agents-discipline/<scope>/`:

```text
.agents-discipline/<scope>/PLAN.md
.agents-discipline/<scope>/GATES.md
.agents-discipline/<scope>/gates/leaf-*.md
.agents-discipline/<scope>/gates/node-*.md
```

The driver rereads the current request and maintains a revisioned contract inventory that maps each independently required outcome or acceptance-changing constraint to an owner and observation. It fixes interfaces, dependencies, conventions, and file ownership before dispatch. Leaves use declared `WAITING`, `READY`, `IN-FLIGHT`, `VERIFIED`, or `ABANDONED` states. Branches use `OPEN`, `VERIFIED`, or `ABANDONED`.

Ready leaves may run together only after each declares complete, disjoint, repository-relative `OWNS:` paths and claims them:

```text
node <path-to-skill>/scripts/gate-check.mjs --scope api --leaf leaf-1.2.1 --claim
```

Lease matching is conservative and may reject a safe-looking pair. It is a coordination guard, not write isolation. Use separate worktrees for colliding worktree-local output, and configure separate cache locations when cache writes can conflict.

Dispatch is rolling: when a verified leaf unblocks another, start the newly ready leaf without waiting for unrelated work. Gate checks remain sequential by default. `--jobs <N>`, where `N` is an integer from 1 through 64, is an opt-in rolling limit for independent checks and keeps reporting in ledger order.

For every independent READY set, open a native launch wave, record each host agent handle, and seal before the first wait. If a partial launch cannot recover, use the audited `abandon --reason` transition; never invent a handle or delete state. Read [references/method.md](skills/agents-discipline/references/method.md), [references/orchestration.md](skills/agents-discipline/references/orchestration.md), [references/dispatch.md](skills/agents-discipline/references/dispatch.md), and [references/parallel.md](skills/agents-discipline/references/parallel.md) before parallel fan-out.

`gate-check.mjs --scope <id>` reduces the scope's ledgers and dispatch waves together. It prints `ALL MET` only when every gate is met and every wave is complete; an abandoned wave remains a non-successful `HANDOFF REQUIRED` outcome.

## Optional Claude Code Stop hook

The hook scans the current session's resolved ledger and dispatch state and returns Claude Code's documented top-level `decision: "block"` response while gates remain unmet or launch waves remain incomplete. It does not execute checks. Its own session-keyed progress guard releases after six consecutive blocks without semantic gate/dispatch progress; metadata-only edits do not reset it. Abandonment stays visible as an explicit bounded handoff in pure, mixed-blocking, and final-release messages, without echoing free-form reasons.

Install only with the user's consent:

```text
node <path-to-skill>/scripts/install-hooks.mjs
node <path-to-skill>/scripts/install-hooks.mjs --scope api
node <path-to-skill>/scripts/install-hooks.mjs --uninstall
```

Default installation writes `.claude/settings.local.json`. Keep that file, `.agents-discipline/`, and `.agents-discipline-hook-state.json` in the project's ignore rules. `--shared` writes absolute Node and hook-script paths into project settings, so it is usually not portable and can expose local directory names. `--global` writes the current user's Claude settings.

The installer preserves unrelated hooks, refuses malformed settings shapes, and identifies moved agents-discipline entries without depending on the install directory name. It writes settings atomically and creates `<settings-file>.agents-discipline.bak` beside an existing settings file before replacing it.

## The eval

This skill is not a vibe; it is a measured result. Full writeup in [references/eval.md](skills/agents-discipline/references/eval.md), raw round-2 results in [evidence/eval-t4-ledger-gate.md](skills/agents-discipline/evidence/eval-t4-ledger-gate.md).

- **Round 1: prose fails.** 12 agents, 3 tasks, 4 rule variants (baseline, +delegation rule, +skill-loading rule, +both). Zero of 12 delegated. Even an explicit skill instruction to "spin up a background agent" was read and ignored by all four research agents. Quality was uniformly high; delegation was uniformly absent.
- **Round 2: structure works.** 8 agents on a genuinely parallel task (12 independent modules, 93 tests). Baseline: 0 of 4 delegated, all passed. With this skill's ledger rules: 4 of 4 wrote the ledger first, spawned 12 parallel workers each, verified every unit, and passed 93/93.
- **Fixture integrity.** The test fixture was validated both ways before running: 0/93 on the stubs, 93/93 against a reference implementation. Failures in the eval are real implementation gaps, not broken tests.

The lesson, in one line: **rules that describe delegation do nothing; a rule that requires a ledger changes behavior 4/4 vs 0/12.** (The 4/4 is round 2's gate candidates; the 0/12 is round 1's prose-rule candidates across three different tasks. The within-round, same-task contrast is 4/4 vs 0/4.)

## What it costs

- Solo, below threshold: one line in the report ("single-agent: N units"). Zero overhead.
- Gate open: the cost of N fresh subagent contexts. That is the whole point. Attention, not time, is the scarce resource, and a fresh context per unit is where the quality comes from.
- The ledger checker is a zero-dependency Node script, milliseconds per run, no tokens spent re-reading the agent's own work.

## Repository map

```text
.claude-plugin/plugin.json       plugin manifest
skills/agents-discipline/
  SKILL.md                       core instructions and mode routing
  SECURITY.md                    CHECK, shell, approval, hook, and lease threat model
  agents/openai.yaml             skill UI metadata
  package.json                   Node package manifest
  references/                    gates, method, orchestration, dispatch, parallel, token-economy,
                                  delegation-method, delegation-orchestration, delegation-token-economy, ledger-discipline, eval
  templates/                     PLAN, gate leaf/node, DELEGATION ledger, worker-brief
  scripts/                       checker, linter, dispatch recorder, installer, Stop hook, ledger checker
  research/                      historical limitations and rerun protocol
  evidence/                      raw eval results
  tests/                         deterministic behavior and regression tests
tests/test_node_suite.py         pytest bridge to the Node test suites
scripts/publish.py               Emasoft release pipeline
```

Run the test suites:

```text
npm --prefix skills/agents-discipline test
uv run pytest tests/
```

## Research basis

Research supports the failure modes that motivate explicit structure; it does not prove that this skill produces a fixed improvement.

- Detailed multi-part prompts still see partial compliance and premature truncation in tested models ([Quantifying Laziness](https://arxiv.org/abs/2512.20662)).
- Reasoning can stop exploration too early or continue beyond useful compute, depending on task and model ([Thoughts Are All Over the Place](https://arxiv.org/abs/2501.18585), [When More Thinking Hurts](https://arxiv.org/abs/2604.10739), [OptimalThinkingBench](https://arxiv.org/abs/2508.13141)).
- SlopCodeBench reports that no tested agent fully solved a problem end to end and that the best agent passed `14.8%` of checkpoints. Checkpoint success is not task completion ([SlopCodeBench](https://arxiv.org/abs/2603.24755)).
- s1's budget forcing lengthens reasoning by appending `Wait` multiple times when the model tries to stop; it is not a claim that one token always improves work ([s1](https://arxiv.org/abs/2501.19393)).
- METR's Time Horizon 1.1 reports a `196.5` day overall P50 doubling-time fit and `130.8` days for the post-2023 fit. The shorter figure must not be described as the all-years estimate ([METR Time Horizon 1.1](https://metr.org/blog/2026-1-29-time-horizon-1-1/)).
- Closed-book knowledge-intensive tasks can hallucinate more with additional test-time compute, and compute-only post-processing cannot add ground-truth information that was not present ([Test-Time Scaling in Reasoning Models Is Not Effective for Knowledge-Intensive Tasks Yet](https://arxiv.org/abs/2509.06861), COLM 2026).

Earlier README versions also cited a six-run internal comparison. The raw artifacts needed to reproduce those exact ratios and counts are not in this repository. Treat the results as historical design input, not a benchmark guarantee. The limitations and a protocol for a defensible rerun are in [research/validation-protocol.md](skills/agents-discipline/research/validation-protocol.md).

### Sources, newest first

Ordered by the most recent public version or publication date known on 2026-08-23. Undated material appears last.

- 2026-08-06: [Test-Time Scaling in Reasoning Models Is Not Effective for Knowledge-Intensive Tasks Yet](https://arxiv.org/abs/2509.06861), v3, COLM 2026
- 2026-07-10: [Measuring AI Ability to Complete Long Software Tasks](https://arxiv.org/abs/2503.14499), v4, NeurIPS 2025
- 2026-05-07: [SlopCodeBench: Benchmarking How Coding Agents Degrade Over Long-Horizon Iterative Tasks](https://arxiv.org/abs/2603.24755), v2
- 2026-04-13: ["Should I Give Up Now?" Investigating LLM Pitfalls in Software Engineering](https://arxiv.org/abs/2411.09916), v3
- 2026-04-12: [When More Thinking Hurts: Overthinking in LLM Test-Time Compute Scaling](https://arxiv.org/abs/2604.10739)
- 2026-01-29: [METR Time Horizon 1.1](https://metr.org/blog/2026-1-29-time-horizon-1-1/)
- 2025-12-19: [Quantifying Laziness, Decoding Suboptimality, and Context Degradation in Large Language Models](https://arxiv.org/abs/2512.20662)
- 2025-10-04: [OptimalThinkingBench: Evaluating Over and Underthinking in LLMs](https://arxiv.org/abs/2508.13141), v2
- 2025-10-03: [Context Anxiety: How AI Agents Panic About Their Perceived Context Windows](https://inkeep.com/blog/context-anxiety)
- 2025-03-01: [s1: Simple test-time scaling](https://arxiv.org/abs/2501.19393), v3
- 2025-02-18: [Thoughts Are All Over the Place: On the Underthinking of o1-Like LLMs](https://arxiv.org/abs/2501.18585), v2
- Undated page: [Unified diffs make GPT-4 Turbo 3X less lazy](https://aider.chat/docs/unified-diffs.html)

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](skills/agents-discipline/SECURITY.md). Behavioral claims need current, directly supporting sources; executable changes need regression coverage.

## Acknowledgments

agents-discipline is born from the fusion of two skills by their original authors: [unlazy](https://github.com/Leonxlnx/unlazy) (completion discipline: gates, checker, Stop hook, Depth Tree) and [colony](https://github.com/kartikkabadi/colony) (delegation discipline: the unit gate, the delegation ledger, worker briefs). Both are MIT licensed; their copyright notices are preserved in LICENSE.

## License

[MIT](LICENSE)
