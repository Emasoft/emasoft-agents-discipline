# Worker brief: <unit name>

You are worker-<N> on a team. You own one unit. Finish it, verify it, report it. Do not touch anything outside your scope.

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

## Verify (run these before reporting done)

```bash
<exact commands with expected output>
```

## Isolation

- Worktree/branch: <e.g. git worktree add -b agent/<slug> ../wt-<slug> main>
- One writer per worktree. Do not merge. The coordinator integrates.

## Report back

- What you implemented
- The output of your Verify commands
- Anything you could not finish and why
