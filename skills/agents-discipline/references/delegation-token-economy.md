# Token economy and thresholds

The delegation threshold is not arbitrary. It exists so the gate costs nothing when it should stay closed and pays for itself when it opens.

## The threshold, and why it is where it is

The gate opens at **3+ independent units, 5+ files, or 30+ minutes**. All three are proxies for the same thing: enough parallelizable work that subagent overhead is amortized.

| Task size | Gate | Why |
|---|---|---|
| 1 unit, quick fix | closed | One ledger row is ceremony, not coordination |
| 2 units, 2 files | closed | Parallelism buys minutes; overhead costs minutes |
| 3+ independent units | open | Fresh context per unit now beats one long thread |
| 5+ files touched | open | Same operation across N files is N natural units |
| 30+ minutes | open | Long-horizon attention decay justifies fresh contexts |

The measured reference point: the orchestrate lesson that ceremony on a half-hour, 12-unit job can cost more than it saves, and the reverse finding from this skill's own eval, that a genuinely parallel 12-unit task is where the ledger structure changes behavior entirely ([eval, round 2](eval.md)). The threshold sits between those two data points.

## What it costs when the gate is closed

One line in the final report:

```
single-agent: 2 units, below threshold
```

That is the entire cost. The gate does not force splitting; it forces the *decision* to be made and stated.

## What it costs when the gate is open

The cost of N fresh subagent contexts. This is the point, not an overhead to minimize: fresh context per unit is where the quality comes from. What the ledger and checklist do is make sure that spend is directed:

- Each worker context is a narrow brief (goal, scope, acceptance, verify), not the coordinator's full history. Token cost per worker is bounded by the brief plus its own work.
- The coordinator re-reads artifacts and runs checks, which are cheap compared to the alternative: carrying ten units in one thread with full detail.
- The ledger checker (<skill-dir>/scripts/ledger-check.mjs) is a zero-dependency Node script, milliseconds, no model tokens at all. It replaces "the agent re-reading its own work to decide if it's done" with a free subprocess.

## When splitting is the wrong call even with the gate open

- **Dependent units.** If every piece needs every other piece's result before starting, the task is one unit however large. Splitting it just moves serialization into coordination overhead. Split at natural joints only.
- **Tiny acceptance surface.** A unit whose acceptance is "looks right" and has no command to run is a unit that cannot be verified cheaply. Prefer fewer, larger, checkable units over many unverifiable ones.
- **The task is a conversation.** The delegation half is for work with artifacts and acceptance. Conversational replies and trivial edits stay solo, by design.

## Scaling beyond a dozen units

- **2-4 units:** spawn directly.
- **5-15 units:** spawn per unit, verify each, integrate as they land.
- **15+ units or a long program:** decompose into phases or tracks, spawn sub-coordinators (each with their own ledger section), keep the top-level ledger as the single source of truth. A hierarchy of coordinators is still one ledger.

The point of the ledger at every scale: a report is a set of claims backed by a ledger, never a vibe of completion.
