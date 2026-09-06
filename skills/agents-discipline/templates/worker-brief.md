# Worker brief: <unit name>

You are worker-<N> on a team. You own one unit. Finish it, verify it, report it. Do not touch anything outside your scope.

## You are a leaf

Do not delegate. Do not split this unit. Do not write a `DELEGATION.md` or spawn subagents of
your own — the coordinator already did the splitting, and a second one collides on the same
filename and the same file leases. If the unit is genuinely too large for one worker, **say so
in your report and stop**; resizing it is the coordinator's call, not yours.

## Goal

<One sentence. What done looks like.>

## Scope

- **You own:** <exact files, comma-separated>
- **You must NOT touch:** <exact files/dirs, comma-separated, especially anything another worker owns>
- <any new files you may create, or "none">

## Context

<Dependencies pasted in full, pointers to specs/relevant files, upstream reports. Workers cannot see the coordinator's thread. Anything you need is written here.>

## Acceptance

- <checkable criterion 1, one line>
- <checkable criterion 2, one line>

## Gates

Your leaf gate ledger: `<path, e.g. .agents-discipline/<scope>/gates/leaf-<id>.md>`

Each gate is written out in full below, because you cannot see the coordinator's thread and
cannot read their approval store.

| Gate | CHECK | EXPECT |
|------|-------|--------|
| G1 | `<command, wrapped in a timeout — see below>` | `<the exact string success prints>` |
| G2 | `<command>` | `<string>` |

**Bound every CHECK yourself.** The 120s limit, the output cap and the process-group kill all
live inside `gate-check.mjs`; a command you run by hand in a shell inherits none of them, so an
unbounded check hangs you instead of failing. Prefer a runner's own flag (`pytest --timeout=120`,
`go test -timeout 120s`). `timeout 120 <command>` works where GNU coreutils is installed — it is
NOT present on a stock macOS, where it is `gtimeout` from `brew install coreutils`, so do not
put a bare `timeout` in a CHECK that has to run on someone else's machine.

You MAY validate the ledger's shape without running anything:

```bash
node <skill-dir>/scripts/gate-check.mjs --status <your leaf ledger>
```

`--status` is read-only: it executes no CHECK and needs no approval. It catches a malformed
ledger before you waste a turn on it.

You do NOT approve gates and you do NOT run `--reverify`. Approval is bound to the
coordinator's path, shell and environment, so it cannot transfer to you; the coordinator
re-runs every gate themselves after you report. Running your CHECKs is for **your** benefit —
catching your own failure before you hand the work back.

## Verify (run these before reporting done)

```bash
<exact commands with expected output>
```

Do not export any of these — they silently corrupt verification:
`AGENTS_DISCIPLINE_SKIP_RERUN` (skips re-runs entirely), `AGENTS_DISCIPLINE_DIR` (a different
lock tree, so lease-overlap protection stops working), `AGENTS_DISCIPLINE_SCOPE` (verifies the
wrong pipeline), `AGENTS_DISCIPLINE_APPROVAL_DIR` (pre-seeded approvals).

## Work this unit in four passes

1. Implement the complete deliverable. Leave no placeholders or deferred remainder.
2. Re-read it as a domain expert and replace the cheap version of each part.
3. Hunt correctness, integration, portability, performance, and evidence defects. Fix what you find.
4. Apply low-cost polish, then repeat until a full improvement pass finds nothing.

Finish only after the pass is clean and every gate above is met with evidence. A gate you
cannot meet ends your work honestly: say which one, and why, in your report.

## Isolation

- Worktree/branch: <e.g. git worktree add -b agent/<slug> ../wt-<slug> main>
- One writer per worktree. Do not merge. The coordinator integrates.

## Report back

- What you implemented
- The output of your Verify commands, and of each gate's CHECK
- Which gates are met, and any you could not meet, with the reason
- Anything you could not finish and why

Your pasted output is a claim, not proof — the coordinator re-runs every gate independently.
Its value is that you catch your own failure before handing the work back, not that anyone
takes your word for it.
