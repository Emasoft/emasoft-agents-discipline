# The delegation method

The method has five steps. The first and last are gates; the middle three are the work.

## Rule zero: count the units before any artifact work

A **unit** is an independent piece of work: one module, one feature, one bug, one migration, one file group, one research thread. Independence is the test: two pieces are independent if neither needs the other's in-progress state to make progress.

The gate opens when any of these hold:

- **3 or more independent units**
- **5 or more files will be touched**
- **estimated 30+ minutes of work**

State the decision either way before work starts: "gate open: N units" or "single-agent: N units, below threshold." The decision belongs in the final report too.

Why a floor and a ceiling: force-splitting a one-unit task pays subagent overhead for nothing (the lone-ant failure in reverse), and refusing to split a twelve-unit task forfeits parallelism and fresh attention. Both are the same error: failing to match structure to the work.

## Step 1: Write DELEGATION.md before any artifact work

The first artifact is the ledger. A file, not a plan in prose, not a mental checklist. Format in [templates/DELEGATION.md](../templates/DELEGATION.md).

The ledger fixes, before anyone starts:

- **Unit count.** One row per unit, numbered.
- **File ownership.** Non-overlapping paths per row. Two workers never touch the same file. This is what makes parallel work safe in a shared repo.
- **Acceptance.** A checkable criterion per row: a command, a test, a measurable threshold. "Works" is not acceptance; `tests/test_x.py passes` is.
- **Status.** `pending` until a worker claims it, `done` when the worker reports, `verified` when the coordinator has checked it themselves.

The ledger is why delegation survives long contexts. A checklist written at minute 2 is still exactly as sharp at minute 90, when the pull toward wrapping up is strongest. Your memory is not a ledger.

## Step 2: Spawn one subagent per unit, in parallel

One subagent per row. Independent rows spawn in the same message, not serially. Serial spawning forfeits the entire point.

Every worker brief is a written contract. Template in [templates/worker-brief.md](../templates/worker-brief.md). Non-negotiables:

- **Goal.** One sentence. What done looks like.
- **Scope.** Exact files the worker owns; exact files it must not touch. Workers cannot see your thread; the boundary has to be in the brief.
- **Context.** Dependencies pasted in full, pointers to specs and upstream reports. Never "see my other worker's output". The worker cannot.
- **Acceptance.** The checkable criteria from the ledger row.
- **Verify.** The exact commands the worker runs before reporting done.
- **Isolation.** Shared repo → one worktree or branch per worker (`git worktree add -b agent/<slug> ../wt-<slug> main`). One writer per worktree.

You are the coordinator. You do not do the workers' work. If you catch yourself implementing a unit you assigned, either reassign it or change the ledger to name you as the worker, never hold it invisibly.

## Step 3: Verify every unit yourself

A worker report is a **self-report**: a claim, not proof. The coordinator's job is the verification hierarchy, in order:

1. **Run the worker's acceptance check yourself.** Not "looks right", run the command, read the artifact.
2. **Update the ledger** to `verified`, or fix the unit and then mark it verified. Never silently accept a claim; never mark `verified` without checking.
3. **Record the evidence in the ledger file.** Write what you actually ran and saw under the `## Evidence` section of `DELEGATION.md`: commands, test output, files read. The ledger checker requires this. A ledger with every row `verified` but no evidence in the file fails `node scripts/ledger-check.mjs`. The file, not the chat, is the record.
4. **Integrate.** Interfaces match, tests pass together, nothing outside the declared scope changed. Branch-level and whole-task checks are the coordinator's, because thirty perfect units can still be a broken product.

## Step 4: Report gate

The final report contains, structurally:

- The gate decision (units counted, threshold met or not)
- The ledger, N of N, with status per row and who did it
- What you verified yourself, with evidence (commands run, tests passed, files read)
- What remains, if anything

**No "done" without a complete verified ledger.** A completion report that skips the ledger is a failed report, mechanically, not rhetorically. If you catch yourself drafting a summary while rows are `pending`, that is the solo reflex firing. Open the ledger.

## When the gate stays closed

- One unit, a quick fix, a tiny change → do it yourself, say so.
- A task that is large but not decomposable → it is one unit, however big. Split at natural joints only; a task where every piece needs every other piece's result is sequential work, and pretending otherwise just moves the serialization into coordination overhead.
