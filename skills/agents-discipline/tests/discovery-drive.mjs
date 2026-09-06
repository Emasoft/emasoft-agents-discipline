#!/usr/bin/env node
/**
 * Dump the ORACLE's filesystem discovery -- listScopes, scopeFiles, legacyFiles and every
 * resolveTarget branch -- as JSON, against a tree built by build-discovery-tree.py.
 *
 * Both drivers are handed the SAME absolute root, so absolute paths in the output compare
 * directly and nothing has to be rewritten (a rewrite step is itself somewhere a divergence
 * can hide). AGENTS_DISCIPLINE_SCOPE is cleared for every call except the row that exists to
 * test it: resolveTarget reads it as a fallback, so a value inherited from the caller's shell
 * would silently steer every other row.
 *
 * Usage: node discovery-drive.mjs <root>
 */
import { existsSync, linkSync, lstatSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { legacyFiles, listScopes, resolveTarget, sameFileIdentity, scopeFiles, statCurrentNamedFile }
  from "../scripts/lib/gates.mjs";

const root = process.argv[2];
// statCurrentNamedFile targets, created here so BOTH drivers see one tree: the mjs runs first
// and builds them, the python driver finds them already present. mkdir/symlink/link are all
// idempotent-guarded so a re-run against the same root is a no-op rather than a crash.
const statDir = join(root, "stat-probe");
mkdirSync(statDir, { recursive: true });
const statFile = join(statDir, "regular");
if (!existsSync(statFile)) writeFileSync(statFile, "hi");
const statSymlink = join(statDir, "link");
if (!existsSync(statSymlink)) symlinkSync(statFile, statSymlink);
const statHardlink = join(statDir, "hard");
if (!existsSync(statHardlink)) linkSync(statFile, statHardlink);
const statFifo = join(statDir, "fifo");
if (!existsSync(statFifo)) execFileSync("mkfifo", [statFifo]);
const statMissing = join(statDir, "absent");
delete process.env.AGENTS_DISCIPLINE_SCOPE;

const target = (options) => {
  const result = resolveTarget({ root, ...options });
  // Key order is fixed here rather than inherited from the object literal each branch happens
  // to return: the branches build DIFFERENT shapes (some carry `error`, one `ambiguous`, most
  // `discoveryErrors`), and JSON.stringify emits insertion order, so a port that returned the
  // same values in a different order would diff as a failure it is not. `undefined` is dropped
  // by JSON.stringify, which is exactly the "absent key" semantics both sides need.
  return {
    mode: result.mode, scope: result.scope, files: result.files,
    discoveryErrors: result.discoveryErrors, error: result.error, ambiguous: result.ambiguous,
  };
};

const withEnv = (value, run) => {
  process.env.AGENTS_DISCIPLINE_SCOPE = value;
  try { return run(); } finally { delete process.env.AGENTS_DISCIPLINE_SCOPE; }
};

process.stdout.write(JSON.stringify({
  listScopes: listScopes(root),
  scopeFiles: ["api", "keep", "missing", "web"].map((s) => scopeFiles(root, s)),
  legacyFiles: legacyFiles(root),
  // scopeFiles/legacyFiles have ZERO callers in scripts/ or tests/ -- they are exported and
  // dead. Dumped anyway because they are the only public reader of _scopeDiscovery's file
  // list, and a port of a dead export still ships in the module every caller imports.
  targets: {
    bare: target({}),
    explicit: target({ files: ["GATES.md", "gates/extra.md"] }),
    explicitAbsolute: target({ files: [root + "/GATES.md"] }),
    scopeApi: target({ scope: "api" }),
    scopeMissing: target({ scope: "definitely-absent" }),
    scopeInvalid: target({ scope: "-bad" }),
    scopeDotDot: target({ scope: ".." }),
    scopeEmptyString: target({ scope: "" }),
    // NON-CANONICAL ROOTS. node's path.join NORMALIZES and os.path.join does not, so a root
    // that is not already canonical produced different path STRINGS -- measured, root+"//"
    // gave "<R>//GATES.md" against the oracle's "<R>/GATES.md". Every other row feeds the
    // already-absolute root the builder prints, so the corpus could not express this at all.
    rootDoubleSep: target({ root: root + "//" }),
    rootDotSegment: target({ root: root + "/./" }),
    rootTrailingSep: target({ root: root + "/" }),
  },
  // Same three roots through the PUBLIC exports, which take a raw root and are where the
  // normalization gap is actually reachable -- resolveTarget resolves its root first.
  legacyFilesNonCanonical: [root + "//", root + "/./", root + "/"].map(legacyFiles),
  scopeFilesNonCanonical: [root + "//", root + "/./"].map((r) => scopeFiles(r, "api")),
  // The env fallback fires ONLY when no explicit scope is passed, and an explicit scope must
  // WIN over it. Two rows, because a port that read the env first would pass the first alone.
  envScope: withEnv("api", () => target({})),
  envScopeOverriddenByExplicit: withEnv("api", () => target({ scope: "web" })),
  // The row that makes scopeEmptyString mean something. "" is FALSY, so the oracle's
  // `options.scope || env` falls through to the env and resolves "api"; a port reading the
  // key by PRESENCE would stop at "" and take the no-scope path. That mutation survived all
  // eight variants before this row existed.
  envScopeWithEmptyExplicit: withEnv("api", () => target({ scope: "" })),
  // path.join itself, because the port reimplements it. os.path.join differs in THREE ways
  // (it DISCARDS a prefix before an absolute segment, keeps a separator an empty segment
  // contributed, and raises on no arguments), and normpath adds a fourth: POSIX preserves a
  // leading "//" where node collapses it. That last one reddened NO variant in this file --
  // no tree here has a "//"-leading root -- so without these rows the fix could be reverted
  // silently. Found by a 3000-case randomized differential; these are its distinct classes.
  jsJoinCorpus: [["a", "b"], ["a//", "b"], ["a/.", "b"], ["a", "b/"], ["/a", "/b"], ["a", "/b"], ["a", ""], ["", ""], ["//"], ["///"], ["///", "a"], ["/", "x.md"], ["/", "."], [".."], ["../a", "b"], ["a", "..", "b"], ["a", "..", "..", "b"], ["/a/", "/b/", "/c/"], ["a/", "/b/"], ["", "a"], ["a", ".", "b"], [".", ""], ["/", ""], ["a/b/", "c"], [], ["é", "à"], ["😀", "a"], ["xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "b"]],
  jsJoin: [["a", "b"], ["a//", "b"], ["a/.", "b"], ["a", "b/"], ["/a", "/b"], ["a", "/b"], ["a", ""], ["", ""], ["//"], ["///"], ["///", "a"], ["/", "x.md"], ["/", "."], [".."], ["../a", "b"], ["a", "..", "b"], ["a", "..", "..", "b"], ["/a/", "/b/", "/c/"], ["a/", "/b/"], ["", "a"], ["a", ".", "b"], [".", ""], ["/", ""], ["a/b/", "c"], [], ["é", "à"], ["😀", "a"], ["xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "b"]]
    .map((parts) => {
    try { return join(...parts); } catch (error) { return "THREW:" + error.constructor.name; }
  }),
  // statCurrentNamedFile: the MESSAGES are the interesting half. Each row reports either
  // "ok:<size>" or the thrown message, so an option-validation divergence shows as text
  // rather than as a silently different Stats object. The paths are built by the caller
  // (see the statTargets note in the runner) so both runtimes see the same tree.
  statCurrentNamedFile: [
    [statFile, undefined], [statFile, {}], [statFile, { label: "ledger" }],
    [statFile, { maxBytes: 10 }], [statFile, { maxBytes: 1 }],
    [statFile, { maxBytes: 0 }], [statFile, { maxBytes: 2.5 }], [statFile, { maxBytes: -1 }],
    [statFile, { maxBytes: null }],        // Number(null) === 0, NOT the absent default
    [statFile, { maxBytes: "10" }],        // Number("10") === 10
    [statFile, { maxBytes: "1" }],         // renders the message from a STRING maxBytes
    [statFile, { maxBytes: "ten" }],       // NaN
    [statFile, { maxBytes: true }],        // Number(true) === 1
    [statFile, { label: "" }],             // falsy label falls back to "file"
    [statFile, { label: 0 }], [statFile, { label: 7 }],
    [statFile, { openFlags: 0 }],
    [statSymlink, undefined], [statHardlink, undefined], [statDir, undefined],
    [statFifo, { maxBytes: 100 }], [statMissing, undefined],
  ].map(([p, o]) => {
    try { const st = statCurrentNamedFile(p, o); return "ok:" + st.size; }
    catch (error) { return String(error.message).replace(root, "<R>"); }
  }),
  // sameFileIdentity takes plain objects here exactly as hardening-tests.mjs:108 does.
  sameFileIdentity: [
    sameFileIdentity({ dev: 7, ino: 11 }, { dev: 7, ino: 11 }),
    sameFileIdentity({ dev: 7, ino: 11 }, { dev: 7, ino: 12 }),
    sameFileIdentity({ dev: 8, ino: 11 }, { dev: 7, ino: 11 }),
    // REAL stat results, not just plain objects. The three rows above all take the port's
    // dict branch, so its getattr branch -- the one every production caller uses -- had zero
    // coverage. Same file twice must be true; two different files must be false.
    sameFileIdentity(lstatSync(root), lstatSync(root)),
    sameFileIdentity(lstatSync(root), lstatSync(dirname(root))),
  ],
}, null, 2) + "\n");
