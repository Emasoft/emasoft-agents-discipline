#!/usr/bin/env node
/**
 * Drive the ORACLE `terminateProcessTree` on a real detached group and report the outcome as
 * JSON, so the Python port can be compared against it rather than against a belief about it.
 *
 * The group kill is what this module IS, and it was the last case in python-lib-checks.py
 * still asserted rather than compared — the port's own self-check proved the port works, which
 * is not the question a port asks. Three real defects had already been found in exactly this
 * code path (`child.exitCode`, `child.kill(sig)`, the send_signal window), all of them invisible
 * to a check that only drives one side.
 *
 * Prints one JSON line: { membersBefore, ok, fallback, diagnostic, survivors }.
 *
 * With `--fail-group-kill`, the process-group kill is replaced by one that throws ESRCH, so
 * the FALLBACK arm runs. That arm holds `_child_kill`, which has been fixed three times
 * (`child.exitCode`, `child.kill(sig)`, the send_signal window) and, until this flag, had
 * never been executed by any test in either runtime: the happy path never reaches it.
 */
import { spawn, execFileSync } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import { terminateProcessTree } from "../scripts/lib/process-tree.mjs";

const members = (pgid) =>
  execFileSync("ps", ["-eo", "pgid,command"], { encoding: "utf8" })
    .split("\n")
    .filter((l) => {
      const head = l.trim().split(/\s+/)[0];
      return /^\d+$/.test(head) && Number(head) === pgid;
    }).length;

// Both sleeps backgrounded and bash blocking in `wait`: three processes by construction,
// independent of any shell fork-suppression optimisation.
const child = spawn("/bin/bash", ["-c", "sleep 47 & sleep 47 & wait"], {
  detached: true,
  stdio: "ignore",
});
const pgid = child.pid;
// ESRCH by name, matching what the oracle reads out of a failed kill (`error.code`).
const killGroup = process.argv.includes("--fail-group-kill")
  ? () => { const e = new Error("No such process"); e.code = "ESRCH"; throw e; }
  : null;

try {
  let membersBefore = 0;
  for (let i = 0; i < 40 && membersBefore < 3; i++) {
    membersBefore = members(pgid);
    if (membersBefore < 3) await sleep(50);
  }

  const result = terminateProcessTree(child, killGroup ? { killGroup } : undefined);
  // Bounded, because an unbounded wait on a kill that did not work turns a failing case into
  // a HANG: the parent's 60s timeout then kills node and prints a traceback instead of a FAIL
  // row naming the case, 60 seconds late. The Python side asserts in 5s; both fail alike now.
  await Promise.race([
    new Promise((done) => child.once("exit", done)),
    sleep(10000).then(() => { throw new Error(`child ${pgid} did not exit within 10s`); }),
  ]);

  // Polled: `exit` reaps only the direct child, so a reparented sleep lingers as <defunct>
  // carrying the pgid for a moment after a correct kill. Skipped under --fail-group-kill:
  // the fallback signals only the DIRECT child, so the two backgrounded sleeps are MEANT to
  // survive it. That arm is about `_child_kill` reporting honestly, not about reaping, and
  // the `finally` clears the group either way.
  let survivors = null;
  if (!killGroup) {
    survivors = 1;
    for (let i = 0; i < 40 && survivors > 0; i++) {
      survivors = members(pgid);
      if (survivors > 0) await sleep(50);
    }
  }

  process.stdout.write(
    JSON.stringify({
      membersBefore,
      ok: result.ok,
      fallback: result.fallback,
      diagnostic: result.diagnostic ?? null,
      survivors,
    }) + "\n",
  );
} finally {
  // The port grew this guard in 80c2554 after its own self-check leaked two `sleep 47`
  // processes on every failure; writing the oracle driver without one re-created the same
  // defect on the other side four commits later. A detached group outlives the node that
  // spawned it, so killing node is not cleanup — and the injected-failure case below is
  // SUPPOSED to leave the group alive for the fallback to handle.
  try { process.kill(-pgid, "SIGKILL"); } catch { /* already gone: the expected case */ }
}
