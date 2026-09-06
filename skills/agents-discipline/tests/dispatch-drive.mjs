#!/usr/bin/env node
/**
 * Drive the ORACLE's dispatch state machine and dump every observable effect as JSON.
 * Pair of tests/dispatch_drive.py.
 *
 * A fixed `now` is threaded through every transition so the two runtimes produce byte-identical
 * state files; without it the timestamps differ and every row diverges on the clock.
 */
import { readFileSync } from "node:fs";
import { writeFileSync, mkdirSync, symlinkSync } from "node:fs";
import { dirname } from "node:path";
import { dispatchStatePath, dispatchStatus, getDispatchWave, updateDispatch }
  from "../scripts/lib/dispatch.mjs";

const root = process.argv[2];
const out = [];
const redact = (v) => String(v).split(root).join("<ROOT>");
const record = (name, value) => out.push([name, value]);
async function attempt(name, fn) {
  try { record(name, { ok: await fn() }); }
  catch (error) {
    if (error && error.code) record(name, { oserror: error.code });
    else record(name, { error: redact(error && error.message || error) });
  }
}
const T = (n) => `2024-03-0${n}T12:00:00.000Z`;
const up = (spec) => updateDispatch(root, spec).then((r) => ({ ...r, wave: r.wave }));

// 1. The happy lifecycle, with a DIGIT wave id so JS key ordering is exercised in the file.
await attempt("open", () => up({ scope: "api", wave: "1", action: "open", leaves: ["a", "b"], now: T(1) }));
await attempt("start a", () => up({ scope: "api", wave: "1", action: "start", leaf: "a", handle: "h-a", now: T(2) }));
await attempt("start b", () => up({ scope: "api", wave: "1", action: "start", leaf: "b", handle: "h-b", now: T(2) }));
await attempt("seal", () => up({ scope: "api", wave: "1", action: "seal", now: T(3) }));
await attempt("return a", () => up({ scope: "api", wave: "1", action: "return", leaf: "a", now: T(4) }));
await attempt("return b", () => up({ scope: "api", wave: "1", action: "return", leaf: "b", now: T(5) }));
record("state file", readFileSync(dispatchStatePath(root, "api"), "utf8"));

// 2. Every refusal, which is where the messages live.
await attempt("reopen", () => up({ scope: "api", wave: "1", action: "open", leaves: ["a"], now: T(6) }));
await attempt("unknown wave", () => up({ scope: "api", wave: "nope", action: "seal", now: T(6) }));
await attempt("open no leaves", () => up({ scope: "api", wave: "w2", action: "open", leaves: [], now: T(1) }));
await attempt("open dup leaf", () => up({ scope: "api", wave: "w2", action: "open", leaves: ["a", "a"], now: T(1) }));
await attempt("bad scope", () => up({ scope: "-bad", wave: "w2", action: "open", leaves: ["a"], now: T(1) }));
await attempt("nonstring wave", () => up({ scope: "api", wave: 7, action: "open", leaves: ["a"], now: T(1) }));
await attempt("bad action", () => up({ scope: "api", wave: "1", action: "explode", now: T(6) }));
await attempt("open w2", () => up({ scope: "api", wave: "w2", action: "open", leaves: ["x", "y"], now: T(1) }));
await attempt("seal unstarted", () => up({ scope: "api", wave: "w2", action: "seal", now: T(2) }));
await attempt("return unsealed", () => up({ scope: "api", wave: "w2", action: "return", leaf: "x", now: T(2) }));
await attempt("start unknown leaf", () => up({ scope: "api", wave: "w2", action: "start", leaf: "zz", handle: "h", now: T(2) }));
await attempt("start x", () => up({ scope: "api", wave: "w2", action: "start", leaf: "x", handle: "h-x", now: T(2) }));
await attempt("restart x", () => up({ scope: "api", wave: "w2", action: "start", leaf: "x", handle: "h2", now: T(2) }));
await attempt("reuse handle", () => up({ scope: "api", wave: "w2", action: "start", leaf: "y", handle: "h-x", now: T(2) }));
// The UTF-16 length bound: 200 emoji is 400 code units, so the oracle REJECTS this handle.
await attempt("emoji handle", () => up({ scope: "api", wave: "w2", action: "start", leaf: "y", handle: "\u{1F600}".repeat(200), now: T(2) }));
await attempt("blank handle", () => up({ scope: "api", wave: "w2", action: "start", leaf: "y", handle: "   ", now: T(2) }));
await attempt("abandon no reason", () => up({ scope: "api", wave: "w2", action: "abandon", now: T(3) }));
await attempt("abandon", () => up({ scope: "api", wave: "w2", action: "abandon", reason: "worker died", now: T(3) }));
await attempt("abandon twice", () => up({ scope: "api", wave: "w2", action: "abandon", reason: "again", now: T(4) }));

// 2b. KEY ORDER, and the reason this scope exists: a LETTER wave inserted BEFORE a DIGIT wave.
// The scope above opens "1" first, so its dict insertion order already matches JS enumeration
// order and a port serializing a raw dict produces the same bytes -- measured, that mutation
// changed nothing. Here JS must hoist "7" above "zz" while insertion order says otherwise.
await attempt("ord zz", () => up({ scope: "ord", wave: "zz", action: "open", leaves: ["a"], now: T(1) }));
await attempt("ord 7", () => up({ scope: "ord", wave: "7", action: "open", leaves: ["a"], now: T(1) }));
record("ord state file", readFileSync(dispatchStatePath(root, "ord"), "utf8"));
record("ord status", dispatchStatus(root, "ord"));

// 2c. The DEFAULT clock. Every row above passes an explicit `now`, so _iso_now never ran --
// the double-now() bug fixed this turn could not have been caught by any test. Only the SHAPE is
// comparable (the two runtimes call the clock microseconds apart), so the row records a regex
// verdict rather than the stamp.
await attempt("default clock", async () => {
  const r = await up({ scope: "clock", wave: "c1", action: "open", leaves: ["a"] });
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(r.wave.openedAt);
});
// 2d. The logWarning branch: a committed transition whose audit append FAILS. Nothing else
// reaches it, so neither the warning text nor safeDiagnostic on that path was exercised.
mkdirSync(dispatchStatePath(root, "warn").replace("/dispatch.json", ""), { recursive: true });
symlinkSync("/nonexistent-target", dispatchStatePath(root, "warn").replace("dispatch.json", "status.log"));
await attempt("logWarning", async () => {
  const r = await up({ scope: "warn", wave: "w1", action: "open", leaves: ["a"], now: T(1) });
  return { state: r.wave.state, warned: r.logWarning.startsWith("state transition committed") };
});

// 3. Reporting.
record("status", dispatchStatus(root, "api"));
record("status no scope", dispatchStatus(root, ""));
await attempt("getDispatchWave", async () => getDispatchWave(root, "api", "1"));
await attempt("getDispatchWave unknown", async () => getDispatchWave(root, "api", "ghost"));

// 4. validateState directly, on shapes no transition can produce.
const bad = [
  ["not an object", null],
  ["wrong schema", { schema: 2, waves: {} }],
  ["waves is an array", { schema: 1, waves: [] }],
  ["bad wave shape", { schema: 1, waves: { w: { state: "open" } } }],
  ["bad state name", { schema: 1, waves: { w: { state: "nope", leaves: ["a"], openedAt: T(1), started: {}, returned: {} } } }],
  ["seal metadata on open", { schema: 1, waves: { w: { state: "open", leaves: ["a"], openedAt: T(1), sealedAt: T(2), started: {}, returned: {} } } }],
  ["return before start", { schema: 1, waves: { w: { state: "sealed", leaves: ["a"], openedAt: T(1), sealedAt: T(2), started: { a: { handle: "h", at: T(3) } }, returned: { a: { at: T(1) } } } } }],
  ["start before open", { schema: 1, waves: { w: { state: "open", leaves: ["a"], openedAt: T(5), started: { a: { handle: "h", at: T(1) } }, returned: {} } } }],
  ["bad timestamp", { schema: 1, waves: { w: { state: "open", leaves: ["a"], openedAt: "not a date", started: {}, returned: {} } } }],
  // TWO problems, and a DIGIT key alongside a letter key: which error fires first depends on
  // JS enumeration order, which puts "1" before "w" however they were inserted.
  ["two problems, digit key first", { schema: 1, waves: { w: { state: "nope", leaves: ["a"], openedAt: T(1), started: {}, returned: {} }, 1: { state: "open", leaves: [], openedAt: T(1), started: {}, returned: {} } } }],
];
for (const [name, value] of bad) {
  const path = dispatchStatePath(root, "bad");
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value) + "\n");
  record("validateState " + name, dispatchStatus(root, "bad"));
}
process.stdout.write(JSON.stringify(out, null, 2) + "\n");
