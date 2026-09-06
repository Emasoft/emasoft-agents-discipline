#!/usr/bin/env node
/**
 * Dump the ORACLE `parseGates` result as JSON, so the Python port can be compared field by
 * field rather than through one consumer's lens.
 *
 * gate-lint reads only `gates`, `abandoned` and `errors`. `parseGates` also returns `lines`,
 * `eol`, `finalNewline`, `owns` and `warnings` — ported, and touched by nothing in either
 * runtime. `gate-check` is exactly what depends on them: `eol`/`finalNewline` to rewrite a
 * ledger without changing its line endings, `owns` for lease claims. Verifying them after
 * that port is built on them is the expensive order.
 *
 * Usage: node parse-gates-drive.mjs <ledger.md>
 */
import { readFileSync } from "node:fs";
import { parseGates } from "../scripts/lib/gates.mjs";

const doc = parseGates(readFileSync(process.argv[2], "utf8"));
// Sets and Maps do not survive JSON.stringify; both sides emit sorted arrays so the
// comparison is on content, not on iteration order.
process.stdout.write(JSON.stringify({
  lines: doc.lines,
  eol: doc.eol,
  finalNewline: doc.finalNewline,
  gates: doc.gates,
  abandoned: [...doc.abandoned].sort(),
  owns: doc.owns,
  errors: doc.errors,
  warnings: doc.warnings,
}, null, 2) + "\n");
