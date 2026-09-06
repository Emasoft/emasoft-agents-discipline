#!/usr/bin/env node
/**
 * Dump the ORACLE's lease behaviour: a SEQUENCE of claims and releases against one root, with
 * the lock directory's contents after each step.
 *
 * Unlike the discovery driver this is STATEFUL -- each step's answer depends on every step
 * before it -- so the two runtimes cannot share a tree. Each is given its own root and the
 * paths are stripped to `<R>`, which is why every row reports a shape rather than a path.
 *
 * This is also the first place `globsOverlap` is exercised against real lock FILES rather than
 * a string corpus: the conflict rows below are its production call site.
 *
 * Usage: node lease-drive.mjs <root>
 */
import { mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { claimLeases, readLeases, releaseLeases, sha256 } from "../scripts/lib/gates.mjs";

const root = process.argv[2];
mkdirSync(root, { recursive: true });
const locks = join(root, ".agents-discipline", "locks");

const scrub = (value) => JSON.parse(JSON.stringify(value, (key, inner) =>
  // `pid` is the ONE field that cannot agree across runtimes -- it is this process's id. It is
  // replaced rather than dropped so its PRESENCE is still compared: a port that forgot to
  // write it would show as null vs "<PID>", not as two matching absences.
  key === "pid" ? "<PID>" : typeof inner === "string" ? inner.split(root).join("<R>") : inner));

const steps = [];
const step = async (label, run) => {
  let outcome;
  try { outcome = scrub(await run()); }
  catch (error) { outcome = "THREW: " + String(error.message).split(root).join("<R>"); }
  // The DIRECTORY is dumped after every step, not just the return value: a claim that answers
  // ok:true while writing nothing, or writes under the wrong name, is invisible from the
  // return value alone. Lease filenames are a digest of scope::leaf, so they compare directly.
  let files = [];
  try { files = readdirSync(locks).sort(); } catch { files = ["(no lock dir)"]; }
  steps.push({ label, outcome, files, leases: scrub(readLeases(root)) });
};

const claim = (scope, leaf, globs) => () => claimLeases(root, { scope, leaf, globs });
const release = (scope, leaf) => () => releaseLeases(root, { scope, leaf });

await step("read on a virgin root", () => readLeases(root));
await step("claim api/leaf-a src/a/**", claim("api", "leaf-a", ["src/a/**"]));
await step("claim api/leaf-b src/b/** (disjoint)", claim("api", "leaf-b", ["src/b/**"]));
// The conflict rows -- globsOverlap's real call site.
await step("claim api/leaf-c src/a/deep/** (overlaps a)", claim("api", "leaf-c", ["src/a/deep/**"]));
await step("claim api/leaf-d src/** (overlaps everything)", claim("api", "leaf-d", ["src/**"]));
await step("claim web/leaf-a src/a/** (other scope, same glob)", claim("web", "leaf-a", ["src/a/**"]));
await step("re-claim api/leaf-a (identity conflict)", claim("api", "leaf-a", ["totally/other/**"]));
// Validation refusals, which must not touch the directory at all.
await step("claim with a bad scope id", claim("-bad", "leaf-x", ["src/x/**"]));
await step("claim with a bad leaf id", claim("api", "-bad", ["src/x/**"]));
await step("claim with no globs", claim("api", "leaf-x", []));
await step("claim with an absolute glob", claim("api", "leaf-x", ["/abs/**"]));
await step("claim with a traversal glob", claim("api", "leaf-x", ["../escape/**"]));
await step("claim with a placeholder glob", claim("api", "leaf-x", ["<repository-relative globs>"]));
// Releases.
await step("release api/leaf-b", release("api", "leaf-b"));
await step("release api/leaf-b again (idempotent)", release("api", "leaf-b"));
await step("claim api/leaf-c now that b is gone", claim("api", "leaf-c", ["src/b/**"]));
await step("release whole scope api", release("api", null));
await step("release a scope that holds nothing", release("nothing", null));

// A TAMPERED record must fail CLOSED: the placeholder lease carries globs ["**"], so it
// overlaps everything and blocks every subsequent claim. Written after the releases so it
// cannot be confused with a real conflict from a live lease.
mkdirSync(locks, { recursive: true });
writeFileSync(join(locks, "not-a-digest.lease"), "{}\n");
await step("claim beside a misnamed lease", claim("api", "leaf-z", ["src/z/**"]));
writeFileSync(join(locks, "not-a-digest.lease"), "{ this is not json\n");
await step("claim beside an unparseable lease", claim("api", "leaf-z", ["src/z/**"]));

// The two records below isolate checks the tampered files above CANNOT reach: both of those
// fail the SHAPE test first (no scope/leaf), so the filename-identity and glob-normalization
// checks were never the sole reason a record was rejected -- measured, dropping either
// reddened NOTHING. Each record here is well-formed in every OTHER respect.
const leaseFor = (scope, leaf, globs) =>
  JSON.stringify({ scope, leaf, globs, pid: 1 }, null, 2) + "\n";

// (1) A VALID record under the WRONG filename. This is the rename-to-impersonate case: the
// filename is the identity, so a correct-looking record parked under another owner's digest
// must be refused rather than honoured.
writeFileSync(join(locks, "0000000000000000000000ff.lease"), leaseFor("api", "leaf-a", ["src/q/**"]));
await step("claim beside a valid record under a WRONG filename", claim("api", "leaf-q", ["src/q/**"]));
rmSync(join(locks, "0000000000000000000000ff.lease"));

// (2) A record at its CORRECT digest filename whose glob is valid but NOT canonical.
// normalizeOwnsGlob("./src/r/**") returns "src/r/**", so the stored text and the normalized
// text disagree -- a reader comparing the stored form would police a different glob than the
// one overlap-checking uses.
const digest = sha256("api" + "::" + "leaf-r").slice(0, 24) + ".lease";
writeFileSync(join(locks, digest), leaseFor("api", "leaf-r", ["./src/r/**"]));
await step("claim beside a record with a NON-CANONICAL glob", claim("api", "leaf-s", ["src/r/**"]));
rmSync(join(locks, digest));

process.stdout.write(JSON.stringify(steps, null, 2) + "\n");
