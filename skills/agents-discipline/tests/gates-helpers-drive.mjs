#!/usr/bin/env node
/**
 * Drive the ORACLE's scope/write/lock/status helpers and dump every observable effect as JSON.
 *
 * Pair of tests/gates_helpers_drive.py. One process, one fresh root, so the two runtimes are
 * compared on the same sequence rather than on assertions I chose to write about each.
 *
 * WHY a driver at all: these five are what lib/dispatch.mjs imports, and four of them are
 * SIDE-EFFECTING (a file appears, a lock is taken and released, a log grows). A unit assertion
 * per function would test the behaviours I thought to name; dumping the state each one leaves
 * behind lets a divergence I did NOT predict surface as a diff.
 *
 * Usage: node gates-helpers-drive.mjs <empty dir>
 */
import {
  existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, symlinkSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import {
  appendStatus, readStableRegularFile, scopeRoot, statusLogPath, validateScopeId, withFileLock,
  writeAtomic,
} from "../scripts/lib/gates.mjs";

const root = process.argv[2];
const out = [];
// Redact BOTH the lexical root and its canonical form: on macOS /var is a symlink to
// /private/var, so a message built from a realpath carries a prefix the lexical replace misses.
// The keys need it as much as the values -- an unredacted key made every scopeRoot row differ
// on the temp directory name alone, which is the harness reporting a divergence that is its own.
const redact = (value) => String(value)
  .split(realpathSync(root)).join("<ROOT>").split(root).join("<ROOT>");
const record = (name, value) => out.push([redact(name), value]);

// A thrown Error is an outcome like any other, and the MESSAGE is the artifact a human reads --
// comparing only "did it throw" would let the two runtimes disagree about why.
//
// ONE DECLARED NORMALIZATION, and it is not the harness editing itself to agree: an error the
// OPERATING SYSTEM produced is recorded by its errno NAME rather than its prose. Node renders
// ENOENT as "ENOENT: no such file or directory, lstat '<path>'" and CPython as "[Errno 2] No
// such file or directory: '<path>'"; no port can turn one into the other, and neither string is
// authored by this codebase. Every message these modules DO author has no errno attached and is
// compared verbatim, which is where a real divergence would live.
async function attempt(name, fn) {
  try { record(name, { ok: await fn() }); }
  catch (error) {
    if (error && error.code) record(name, { oserror: error.code });
    else record(name, { error: redact(error && error.message || error) });
  }
}

// 1. validateScopeId: the boundary cases of ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ plus the shapes
//    that only differ between the two regex dialects (trailing newline, non-ASCII word chars).
// Labelled explicitly, not by String(id): JS renders the empty-ish values as "null"/"undefined"
// and Python as "None", so keying on the stringified input would diff on the LABEL while the
// answers agreed. The falsy cases matter (the oracle's `String(value || "")` folds 0 to "").
const ids = [["api", "api"], ["a", "a"], ["empty", ""], ["dot", "."], ["dotdot", ".."],
  ["lead-dash", "-lead"], ["lead-underscore", "_lead"], ["space", "a b"],
  ["trailing-newline", "api\n"], ["embedded-newline", "api\nx"], ["non-ascii-latin", "café"],
  ["non-ascii-symbol", "ⓐ"], ["len64", "a".repeat(64)], ["len65", "a".repeat(65)],
  ["punctuation", "a.b-c_d"], ["zero-string", "0"], ["slash", "a/b"], ["backslash", "a\\b"],
  ["nul", "a\0b"], ["falsy-number", 0], ["empty-value", null]];
for (const [label, id] of ids) record("validateScopeId " + label, validateScopeId(id));
record("validateScopeId label", validateScopeId("a b", "leaf"));

// 2. scopeRoot / statusLogPath: pure path shape, including the un-normalized spellings where
//    path.join and os.path.join part ways.
for (const [r, s] of [[root, "api"], ["a", "api"], ["a//", "api"], ["./a", "api"], ["a/b/..", "api"]]) {
  record("scopeRoot " + r, redact(scopeRoot(r, s)));
}
record("statusLogPath scoped", redact(statusLogPath(root, "api")));
record("statusLogPath bare", redact(statusLogPath(root, null)));

// 3. writeAtomic, with and without a root, over a directory that does not exist yet.
await attempt("writeAtomic nested", () => {
  writeAtomic(join(root, "deep", "nest", "file.txt"), "hello\n");
  return readFileSync(join(root, "deep", "nest", "file.txt"), "utf8");
});
await attempt("writeAtomic rooted", () => {
  writeAtomic(join(scopeRoot(root, "api"), "dispatch.json"), '{"schema":1}\n', { root });
  return readFileSync(join(scopeRoot(root, "api"), "dispatch.json"), "utf8");
});
await attempt("writeAtomic overwrite", () => {
  writeAtomic(join(root, "deep", "nest", "file.txt"), "again\n");
  return readFileSync(join(root, "deep", "nest", "file.txt"), "utf8");
});
// The state directory is 0700 in the rooted branch and umask-default in the other. Node applies
// the mode to every directory it creates; a port that used os.makedirs would apply it only to
// the leaf, so compare the mode of the INTERMEDIATE too.
record("mode .agents-discipline", (lstatSync(join(root, ".agents-discipline")).mode & 0o777).toString(8));
record("mode scope dir", (lstatSync(scopeRoot(root, "api")).mode & 0o777).toString(8));
// No temporaries left behind on the success path.
record("scope dir entries", readdirSync(scopeRoot(root, "api")).sort());

symlinkSync(join(root, "deep", "nest", "file.txt"), join(root, "link.txt"));
await attempt("writeAtomic onto symlink", () => {
  writeAtomic(join(root, "link.txt"), "nope\n");
  return "wrote";
});

// 4. The containment guard: a state file symlinked out of the root must be refused when a root
//    is supplied and read when it is not. This is the guarantee gates.py shipped without.
// The link must be an ANCESTOR DIRECTORY, not the file. A symlinked final component is refused
// by O_NOFOLLOW before the root is ever consulted -- a first version of this case linked the
// file, got "must be one unchanged regular single-link file" from both runtimes, and would have
// certified a containment guard that never executed. Linking the directory lets the open
// succeed, so the realpath comparison is the only thing that can reject it.
mkdirSync(join(root, "outside"), { recursive: true });
writeFileSync(join(root, "outside", "escaped.json"), '{"schema":1}\n');
symlinkSync(join(root, "outside"), join(scopeRoot(root, "api"), "via-link"));
await attempt("read escaped without root", () =>
  readStableRegularFile(join(scopeRoot(root, "api"), "via-link", "escaped.json"),
    { label: "dispatch state" }));
await attempt("read escaped with root", () =>
  readStableRegularFile(join(scopeRoot(root, "api"), "via-link", "escaped.json"),
    { root: join(root, ".agents-discipline"), label: "dispatch state" }));
// Positive control for the guard: an ordinary file INSIDE the root must still read with the
// same root supplied. Without this, a guard that rejected everything would look correct.
await attempt("read contained with root", () =>
  readStableRegularFile(join(scopeRoot(root, "api"), "dispatch.json"),
    { root: join(root, ".agents-discipline"), label: "dispatch state" }));
symlinkSync(join(root, "outside", "escaped.json"), join(scopeRoot(root, "api"), "escape.json"));
await attempt("read symlinked file", () =>
  readStableRegularFile(join(scopeRoot(root, "api"), "escape.json"), { label: "dispatch state" }));

// 5. appendStatus: creation, append, CR/LF folding, and the linked-log refusal.
await attempt("appendStatus first", () => {
  appendStatus(root, "api", "2020-01-01T00:00:00.000Z started");
  return readFileSync(statusLogPath(root, "api"), "utf8");
});
await attempt("appendStatus folds newlines", () => {
  appendStatus(root, "api", "line one\r\nline two\n\n\nline three");
  return readFileSync(statusLogPath(root, "api"), "utf8");
});
await attempt("appendStatus bare scope", () => {
  appendStatus(root, null, "no scope");
  return readFileSync(statusLogPath(root, null), "utf8");
});
mkdirSync(scopeRoot(root, "linked"), { recursive: true });
symlinkSync(join(root, "deep", "nest", "file.txt"), join(scopeRoot(root, "linked"), "status.log"));
await attempt("appendStatus onto symlink", () => appendStatus(root, "linked", "nope"));

// 6. withFileLock: the return value passes through, the lock file is gone afterwards, a
//    re-entrant attempt with a zero timeout fails with the oracle's message, and the lock NAME
//    is a function of the canonical target -- so two spellings of one path must collide.
await attempt("withFileLock returns", () => withFileLock(root, join(root, "target"), () => "value"));
record("lock dir after release", readdirSync(join(root, ".agents-discipline", "locks")).sort());
// The inner target is the SAME file spelled through the root's canonical path. `deep/../target`
// would not have tested this: path.join normalizes it away at the call site while os.path.join
// keeps it, so the two runtimes were locking on different strings and the message differed for a
// reason that had nothing to do with the lock. A realpath'd prefix is a spelling neither
// language's join collapses, so only canonicalLockTarget can make the two collide.
await attempt("withFileLock nested times out", () =>
  withFileLock(root, join(root, "target"), () =>
    withFileLock(root, join(realpathSync(root), "target"), () => "unreachable", { timeoutMs: 0 })));
record("lock dir after timeout", readdirSync(join(root, ".agents-discipline", "locks")).sort());
// A throw from fn must still release the lock.
await attempt("withFileLock releases on throw", () =>
  withFileLock(root, join(root, "target"), () => { throw new Error("boom"); }));
record("lock dir after throw", readdirSync(join(root, ".agents-discipline", "locks")).sort());
await attempt("withFileLock missing root", () =>
  withFileLock(join(root, "no-such-root"), join(root, "target"), () => "value"));

// Regressions for the four findings of the b263dad review. Each row is a case that PASSED the
// 55-row driver before the fix, which is the point: they were all in branches nothing drove.
// statusLogPath bare, over the un-normalized spellings the scoped branch already covered. The
// original row passed an absolute mkdtemp path -- the one shape that cannot expose it.
for (const r of ["a//", "./a", "a/b/..", "a"]) record("statusLogPath bare " + r, redact(statusLogPath(r, null)));
// A well-formed but NON-OBJECT lock file. The port raised AttributeError out of the finally and
// replaced the caller's error; both runtimes leave the lock file itself behind, by design.
await attempt("withFileLock non-object lock file", () =>
  withFileLock(root, join(root, "corrupt"), () => {
    const dir = join(root, ".agents-discipline", "locks");
    for (const n of readdirSync(dir)) if (n.endsWith(".filelock")) writeFileSync(join(dir, n), "[]");
    throw new Error("invalid dispatch state: boom");
  }));
await attempt("withFileLock unparseable lock file", () =>
  withFileLock(root, join(root, "corrupt2"), () => {
    const dir = join(root, ".agents-discipline", "locks");
    for (const n of readdirSync(dir)) if (n.endsWith(".filelock")) writeFileSync(join(dir, n), "{not json");
    return "returned anyway";
  }));
// A FILE where a directory must be created: the oracle's mkdirSync raises EEXIST rather than
// returning, and the port swallowed it and leaned on each caller's follow-up assert.
writeFileSync(join(root, "blocker"), "");
await attempt("writeAtomic through a file", () => {
  writeAtomic(join(root, "blocker", "child.txt"), "x");
  return "wrote";
});
// ROOTED too, and this is the case that matters: the non-root branch above passes no mode, so
// the port routes it through os.makedirs and never reaches the mkdir loop the fix is in. A
// mutation control caught that — the row above was unchanged with the fix reverted, i.e. it was
// testing a different code path than the one it was written for.
writeFileSync(join(scopeRoot(root, "api"), "blocked"), "");
await attempt("writeAtomic rooted through a file", () => {
  writeAtomic(join(scopeRoot(root, "api"), "blocked", "child.json"), "x", { root });
  return "wrote";
});

record("final tree", readdirSync(root).sort());
record("state tree", readdirSync(join(root, ".agents-discipline")).sort());
record("existsSync temp leak", readdirSync(root).filter((n) => n.endsWith(".tmp")).length);
record("sanity", existsSync(join(root, "deep", "nest", "file.txt")));

process.stdout.write(JSON.stringify(out, null, 2) + "\n");
