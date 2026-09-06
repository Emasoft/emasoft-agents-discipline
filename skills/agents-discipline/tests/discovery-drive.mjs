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
import { lstatSync } from "node:fs";
import { dirname } from "node:path";
import { legacyFiles, listScopes, resolveTarget, sameFileIdentity, scopeFiles }
  from "../scripts/lib/gates.mjs";

const root = process.argv[2];
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
