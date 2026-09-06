---
trdd-id: REJRD8V5
title: Port all nine agents-discipline scripts from JS to Python against the JS suite as oracle
column: dev
created: 2026-09-07T00:18:33+0200
updated: 2026-09-07T00:35:07+0200
current-owner: main
task-type: refactor
scope: project
---

# Port all nine scripts to Python

## ⏵ STATE — READ THIS FIRST ON RESUME (authoritative; supersedes the body) — 2026-09-07

**Method (do not vary it).** The JS suite is held FIXED as the ORACLE; only the implementation
varies. Any divergence is a porting defect, never a re-specified test. `AD_RUNTIME=python`
switches `dispatch-tests.mjs`, `lint-tests.mjs` and `ledger-tests.mjs` to the port. Paired
drivers (`X-drive.mjs` / `X_drive.py`) dump every observable effect as JSON; the diff is the
test.

**Every check pairs with a control proving it CAN fail.** A green run is the weakest evidence in
this project — the recurring defect all session has been an assertion satisfied by something
other than the property it names. Mutate the implementation, confirm the intended row reddens,
revert. If no mutation isolates a row, that row does not earn its place.

### DONE (verified by differential + mutation control)
- `lib/gates.py`: `read_stable_regular_file`, `write_atomic`, `with_file_lock`, `append_status`,
  `parse_gates`, `validate_scope_id`, `scope_root`, `normalize_owns_glob`, `_write_all`
- `lib/dispatch.py` + `dispatch_check.py` — 21/21 under the oracle's own suite
- `lib/jsapi.py`: `js_object_key_order`, `js_json_object`, `js_length`, `js_slice`,
  `locale_compare_key`, `parse_date`, `js_trim`, `js_truthy`, `js_string`/`_js_number`
- `gates.py`: `gate_definition_digest`, `automatic_evidence_prefix`, `classify_gate_evidence`,
  `gate_state`, `tail`, `format_document`, `qualify`, `js_basename`
- `gates.py`: `globs_overlap`, `literal_prefix` — 18 pairs, each dumped in BOTH directions.
  Six mutation controls all redden. The doubling is EARNED, not defensive: dropping the `or`
  from the wildcard test changes only ODD indices (measured `[1, 3, 15, 17, 19]`), so a
  one-direction corpus ships that defect. Symmetry is a discriminating property here — it
  holds for the oracle and BREAKS under that mutant. The astral rows are a CONTROL, not a
  catch: this function only tests EQUALITY, where UTF-16 and code points agree; the `<`
  divergence has no site here. The measured `OWNS:` placeholder disjointness is PINNED as a
  row, so the port reproduces the defect rather than quietly diverging from the oracle.

- `gates.py`: `list_scopes`, `scope_files`, `legacy_files`, `resolve_target`,
  `same_file_identity`, and the private `_named_entry` / `_real_directory_inside` /
  `_markdown_discovery` / `_scope_discovery` / `_legacy_discovery`. Eight tree shapes
  (`tests/discovery-diff.sh` + `build-discovery-tree.py`), nine mutation controls, six redden.
  **Found a real defect on the first run**: JS `Array.sort()` orders by UTF-16 code UNITS and
  Python `sorted()` by code POINTS, so a `gates/` directory holding U+1F600 and U+FFFD came
  back reversed — `markdownDiscovery` sorts filenames with NO id filter, so nothing upstream
  prevents it. Fixed with `jsapi.js_sort_key` (UTF-16-BE bytes; big-endian is the property
  that makes byte order equal unit order) at both sort sites.

### NEXT ACTION
1. **`statCurrentNamedFile`** — the last non-dead `gates.mjs` export, consumed by
   `gate-check.mjs:422,435`. Its logic is already INLINE inside the ported
   `read_stable_regular_file`; the work is extracting it as a public name with the oracle's
   own option surface (`maxBytes`, `openFlags`, `label`, `stableSnapshot`) and its
   non-creating/non-truncating flag assertion. The Windows fstat-bracketing branch is
   unreachable on this machine — port it, mark it UNWITNESSED, do not pretend otherwise.
2. **`gate-check.mjs`** (950 lines) — last, because it consumes all of the above.

`readLeases` needs `releaseLeases`/`claimLeases` context and is dumped by no driver yet;
`scopeFiles`/`legacyFiles` have ZERO callers in `scripts/` or `tests/` (verified) — ported
because they are exported and would ship, not because anything exercises them.

### GOTCHAS THAT HAVE ALREADY COST TIME — do not rediscover these
- **Stale `.pyc` makes two measurements of the same code disagree.** A `.pyc` validates on
  `(source mtime, source size)` ALONE, so a SAME-SIZE rewrite inside one mtime second — exactly
  what a mutation probe does — runs the OLD bytecode while the `.py` reads correct.
  `inspect.getsource()` cannot reveal it. Prefix ad-hoc probes with `PYTHONDONTWRITEBYTECODE=1`
  (or `python3 -B`). The suites and drivers are already guarded; a hand-typed
  `python3 -c "...import gates..."` is not.
- **Mutation probes need a UNIQUE anchor.** A first-match `replace(old, new, 1)` reverted into a
  DIFFERENT function once, silently editing code the probe never targeted.
- **Escapes typed into a tool call materialise into literal characters.** Writing `U+2028` put a
  real bidi control into source twice, and the escape for U+0085 became U+2026 in a fixture, making the row
  inert. Build hostile characters with `chr()` / `String.fromCodePoint()`; never type them.
- **`str.splitlines()` splits on U+2028/U+2029/U+0085/VT/FF** — an invisible-character scanner
  built on it consumes the very characters it searches for and reports files clean that are not.
- The JS/Python trap list, each already found live: UTF-16 length & slice; JS object key order
  (integer-like keys first); `$` vs `\Z`; ASCII-vs-Unicode regex classes; `json.dumps`
  separators + `ensure_ascii`; `.trim()` vs `.strip()` (differs BOTH ways); `String()` vs
  `str()`; JS truthiness (NaN falsy, `{}`/`[]` truthy); `Math.max([])`; errno names vs strerror;
  `path.basename` strips trailing separators.

### KNOWN GAPS, recorded rather than hidden
- `str(cwd)` in `gate_definition_digest` is not `String(cwd)` for a non-str. Unreachable from
  `parse_gates`; goes live when `hardening-tests.mjs` (which hand-builds gates) is ported.
- `js_basename` on a Windows UNC root: `ntpath.basename` answers `""` where node answers
  `"share"`. Pre-existing in the delegate; no test here runs on Windows.
- `with_file_lock` CONTENTION has zero coverage (needs two processes).
- `re.ASCII` on `_MD_SUFFIX_RE` is defensive and UNWITNESSED — measured, no character folds onto
  "m" or "d", so no row can distinguish its presence.
- Three spellings in the discovery port are DEFENCE IN DEPTH, not verified behaviour, and a
  green `discovery-diff.sh` must not be read as covering them: `is_dir(follow_symlinks=False)`,
  `realpath(..., strict=True)`, and `os.path.exists` (vs `lexists`). All three mutations
  survive every variant, for ONE cause — `_real_directory_inside` lstats and rejects symlinks
  itself, subsuming each guard ahead of it. The ORACLE carries the same redundancy, so the
  port is faithful; the gap is in what any test can witness, not in the code.
- A mutation probe must CHECK ITS EDIT LANDED. One reported "NOTHING REDDENED" for an anchor
  that appeared 3 times, so the assert fired, the file was never mutated, and the suite ran
  clean against unmutated code — a green that meant nothing. Compare the file before and after
  the edit, not just the exit status of the probe.

## Why this TRDD exists

The ordered plan lived only in `docs_dev/gate-check-port-plan.md`, which is gitignored working
state — correct for scratch, wrong for the one artifact a future session must resume from. The
decisions now live here, where they are tracked, survive a clone, and are captured verbatim in
every compaction handoff (which reads in-flight TRDD STATE blocks). The scratch file stays put.
