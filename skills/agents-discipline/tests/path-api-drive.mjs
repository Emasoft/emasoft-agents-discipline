#!/usr/bin/env node
/**
 * Dump the ORACLE's answers for every `node:path` function gate-check.mjs imports.
 *
 * WHY A COMMITTED DRIVER AND NOT A /tmp PROBE. The first version of this measurement was a
 * pair of throwaway scripts. Their results were then cited in the TRDD as measurements that
 * nothing in the repo could reproduce -- the same defect as an instruction whose inputs have
 * vanished, which this project had already caught once ("re-run them through mutate-probe.sh",
 * pointing at mutations that existed only in a discarded transcript).
 *
 * Pair: tests/path_api_drive.py. Compared by tests/path-api-diff.sh.
 */
import { delimiter, dirname, basename, isAbsolute, join, relative, resolve, sep } from "node:path";

// One case list per function, shared verbatim with the Python driver. Chosen for the shapes
// where the two libraries are KNOWN to make different choices -- trailing slashes, doubled
// separators, empty strings, `..` segments, and absolute segments in the middle of a join.
export const CASES = {
  // `//a` and `//a/b` are here because a MUTATION CONTROL exposed their absence: breaking
  // js_dirname's double-slash-root branch (`return "//"`) reddened NOTHING against the
  // original corpus. `//` alone does not reach it -- that path has no non-slash character, so
  // the scan ends without a candidate separator and returns "/" by the other arm.
  dirname: ["/a/b", "/a/", "a", "/", "", "//", "//a", "//a/b", "/a//b//", "/a/b/.", "a/b", "./a"],
  basename: ["/a/b", "/a/", "a", "/", "", "//", "/a//b//", "a/b/", ".", ".."],
  isAbsolute: ["/a", "a", "", "./a", "//a", "../a", "/"],
  join: [["/a", "b"], ["/a", "/b"], ["a", ""], ["", "b"], ["/a", ".."], ["/a", "b/"],
         ["a", "..", "..", "b"], [""], ["/"], ["a/", "/b"]],
  relative: [["/a/b", "/a/b/c"], ["/a/b", "/a"], ["/a/b", "/a/b"], ["/a/b", "/x"],
             ["/a/b", "/a/b/../c"], ["/a//b", "/a/b/c"], ["/a/b/", "/a/b/c/"],
             ["/a/b", "/a/bc"], ["/a", "/a/../a/x"], ["/", "/a"],
             // `["/a","/"]` closes a REAL gap: js_relative's to-is-root arm
             // (`i == 0` -> last_common_sep = 0) had no row reaching it. MEASURED by running
             // that mutation against the PREVIOUS corpus, where it was SILENT.
             //
             // `["/a/bc","/a/b"]` does NOT close a gap and is kept only as a distinct shape.
             // It was added in the same commit under the same claim, and the same measurement
             // showed the from-longer branch was ALREADY covered -- the mutation reddened
             // against the old corpus too. Two rows were asserted as gap-closing on the
             // strength of one having been verified; running the control against the OLD
             // corpus is the step that separates "this row is needed" from "this row is new".
             ["/a", "/"], ["/a/bc", "/a/b"]],
  // The `//` rows are the ones that matter most and were MISSING from the first corpus.
  // POSIX gives exactly two leading slashes implementation-defined meaning: Python's
  // posixpath PRESERVES them (collapsing three or more), node collapses to one. `resolve`
  // feeds `approvalPath`'s sha256 identity (:372) and `oracle().cwd`, so a one-character
  // difference here is total, silent approval failure -- the highest-ranked hazard in the
  // TRDD. A five-row single-slash corpus reported `resolve` as AGREEING.
  // The `.`-segment rows likewise come from a control: blanking `_normalize_string`'s "."
  // case reddened NOTHING, because no row fed a `.` segment through resolve.
  resolve: [["/a", "b"], ["/a", "/b"], ["a"], ["/a", ".."], ["/", ".."], ["/a", "b", "../c"],
            ["//a"], ["///a"], ["//"], ["//a/b"], ["//a/../b"],
            ["/a", "./b"], ["/a/./b"], ["a/./b"], ["/a/b/."], ["."], ["./."]],
};

const answers = {
  // `String(...)` on every result: a divergence must show as a VALUE difference, never as a
  // JSON type difference that the comparison would report identically for "" and null.
  dirname: CASES.dirname.map((p) => String(dirname(p))),
  basename: CASES.basename.map((p) => String(basename(p))),
  isAbsolute: CASES.isAbsolute.map((p) => isAbsolute(p)),
  join: CASES.join.map((parts) => String(join(...parts))),
  // resolve() FIRST, matching gate-check's own two call sites (:336 and :363) -- both feed
  // relative()/dirname() an already-resolved path, so a corpus of raw relative inputs would
  // measure a code path production never takes.
  relative: CASES.relative.map(([a, b]) => String(relative(resolve(a), resolve(b)))),
  resolve: CASES.resolve.map((parts) => String(resolve(...parts))),
  // PLATFORM CONSTANTS. Reported, not asserted equal across platforms -- they are supposed to
  // differ by OS. What the differential checks is that the two RUNTIMES agree on the SAME box.
  sep,
  delimiter,
  platform: process.platform,
};

process.stdout.write(JSON.stringify(answers, null, 2) + "\n");
