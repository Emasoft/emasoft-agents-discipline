#!/usr/bin/env node
/**
 * Drive the ORACLE regex worker as a subprocess so it can be compared with the Python port.
 *
 * regex-worker.mjs is a node:worker_threads Worker: it talks over `parentPort`, so running it
 * with `node regex-worker.mjs` and a line of stdin produces nothing at all. The port moved the
 * same one-shot single-message contract onto stdio, and that transport difference is exactly
 * what makes a literal-asserting test look like parity while comparing nothing — this shim
 * exists so the differential check can drive both sides on the same input.
 *
 * Reads one JSON message on stdin, writes the worker's one reply as JSON on stdout.
 */
import { Worker } from "node:worker_threads";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const message = JSON.parse(readFileSync(0, "utf8"));
const worker = new Worker(
  resolve(dirname(fileURLToPath(import.meta.url)), "../scripts/lib/regex-worker.mjs"),
);
worker.once("message", (reply) => {
  process.stdout.write(JSON.stringify(reply) + "\n");
  worker.terminate();
});
worker.once("error", (err) => {
  process.stdout.write(JSON.stringify({ error: err.message }) + "\n");
  worker.terminate();
});
worker.postMessage(message);
