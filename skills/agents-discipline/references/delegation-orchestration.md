# Orchestration and isolation

The delegation half's quality comes from two properties that prose delegation never had: **fresh context per unit** and **non-overlapping ownership**. This document is about making both mechanically true.

## Why fresh context wins

The lone-agent failure is not a time problem, it is an attention problem. A single thread that carries ten units gets ten units' worth of drift and compression. A fresh subagent per unit starts with full attention on exactly one job, a narrow brief, and a clear acceptance line.

This is the same mechanism the completion half identifies for its orchestrated mode ("attention, not time, was always the scarce resource"), applied at the task level rather than the leaf level. The two halves are orthogonal: the completion half makes each leaf finish; the delegation half makes the leaves exist, owned by workers, coordinated through a ledger.

## The worker brief contract

Every spawned worker receives a brief with exactly these sections. Template in [templates/worker-brief.md](../templates/worker-brief.md).

| Section | What it must contain | Why it is non-negotiable |
|---|---|---|
| Goal | One sentence: what done looks like | The worker's entire frame |
| Scope | Owned files; forbidden files | Workers collide exactly where scope is unstated |
| Context | Dependencies pasted in full | Workers cannot see the coordinator's thread |
| Acceptance | Checkable criteria, one line each | The worker knows when it is done |
| Verify | Exact commands to run before reporting done | Turns "I think it works" into a runnable claim |
| Isolation | Worktree/branch per worker | Parallel writers to the same tree corrupt each other |

Scope is the most important line. A worker told "touch app/stats.py only" cannot corrupt the other eleven modules even if it tries. The coordinator's verification burden drops accordingly: check the owned file, check the integration, done.

## Worktree and branch isolation

In a shared repo, parallel workers must not write the same working tree. Two options, in order of preference:

```bash
# Worktree: full isolation, independent working directories.
# HEAD, not a hardcoded `main`: the default branch may be `master` or `trunk`, and the
# worker must start from the coordinator's current commit, not from the default branch.
git worktree add -b agent/stats ../wt-stats HEAD

# Shared tree: no branch per worker. One checkout can hold only one branch at a time, so
# `git checkout -b` by one worker switches the tree under every other. All workers write
# the same branch; safe only if they touch disjoint files and never run git themselves.
```

Worktrees are strictly safer and are the default recommendation. One writer per worktree. The ledger's "Files (mine)" column is what makes shared-tree parallelism safe when worktrees are impractical.

The coordinator is the only one who merges, and only it pushes. A worker in its own worktree may commit on its own branch; a worker in a shared tree never runs git at all, per above. Verify each unit in its own worktree, then merge it before running `ledger-check.mjs`: the checker re-runs every `verified` row's acceptance from the ledger's repository root, where an unmerged unit's work does not exist.

## The verification hierarchy

1. **Worker self-checks.** Runs its own `Verify` commands before reporting. Weakest layer; the worker is motivated and fallible.
2. **Coordinator re-verification.** Runs the same checks itself and reads the artifact. This is the layer that makes worker self-reports safe.
3. **Integration checks.** Whole-suite run, interface match, scope diff. Catches the "thirty perfect units, broken product" failure.

Layers 2 and 3 are not optional. A coordinator that trusts worker reports is a coordinator that never delegated at all. It just outsourced the thinking and skipped the checking.

## What the coordinator's context is for

Judgment and verification, not bulk work. The coordinator:

- reads worker briefs and specs (short),
- runs checks and reads diffs (mechanical, cheap),
- holds the ledger (the contract),
- integrates and decides.

The coordinator does NOT: implement units, re-read whole files line-by-line, or carry each unit's detail forward. That is what the ledger and the workers are for.

## Failure handling

- **Worker reports done but acceptance fails.** The coordinator runs the check, sees it fail, and either fixes it or spawns a follow-up worker with the failure output as context. Never silently accept.
- **Worker cannot finish.** Return to the coordinator with what was done, what failed, and why. The coordinator decides: reassign, re-scope, or take the unit itself (and update the ledger to name itself as worker).
- **Two workers drift into each other's scope.** The ledger catches it at integration, a diff touching a file outside the row's ownership column. Revert the trespass, re-run the owner's checks.
