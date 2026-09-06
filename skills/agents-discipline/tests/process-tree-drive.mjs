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

let membersBefore = 0;
for (let i = 0; i < 40 && membersBefore < 3; i++) {
  membersBefore = members(pgid);
  if (membersBefore < 3) await sleep(50);
}

const result = terminateProcessTree(child);
await new Promise((done) => child.once("exit", done));

// Polled: `exit` reaps only the direct child, so a reparented sleep lingers as <defunct>
// carrying the pgid for a moment after a correct kill.
let survivors = 1;
for (let i = 0; i < 40 && survivors > 0; i++) {
  survivors = members(pgid);
  if (survivors > 0) await sleep(50);
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
