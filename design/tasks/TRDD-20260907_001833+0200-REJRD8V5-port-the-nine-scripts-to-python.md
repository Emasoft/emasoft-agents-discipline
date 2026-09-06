---
trdd-id: REJRD8V5
title: Port all nine agents-discipline scripts from JS to Python against the JS suite as oracle
column: dev
created: 2026-09-07T00:18:33+0200
updated: 2026-09-07T01:35:41+0200
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

- `gates.py`: `stat_current_named_file` (36e3785) and `claim_leases` / `release_leases` /
  `read_leases` / `sleep` (f3a4c86). The leases have their own STATEFUL 22-step differential
  (`tests/lease-diff.sh`) — the first place `globs_overlap` runs against real lock FILES. Four
  controls redden, two only after adding records that ISOLATE the filename-identity and
  glob-normalization checks: the tampered records already there fail the SHAPE test first, so
  neither check was ever the sole reason for a rejection.
- `gates.py`: `list_scopes`, `scope_files`, `legacy_files`, `resolve_target`,
  `same_file_identity`, and the private `_named_entry` / `_real_directory_inside` /
  `_markdown_discovery` / `_scope_discovery` / `_legacy_discovery`. Eight tree shapes
  (`tests/discovery-diff.sh` + `build-discovery-tree.py`), now NINE shapes; nine mutation
  controls in the first round, six redden.
  **Found a real defect on the first run**: JS `Array.sort()` orders by UTF-16 code UNITS and
  Python `sorted()` by code POINTS, so a `gates/` directory holding U+1F600 and U+FFFD came
  back reversed — `markdownDiscovery` sorts filenames with NO id filter, so nothing upstream
  prevents it. Fixed with `jsapi.js_sort_key` (UTF-16-BE bytes; big-endian is the property
  that makes byte order equal unit order) at both sort sites.

### ROUNDS 5-8 — the reviews moved off the PORT and onto the TEST HARNESS
Rounds **5-8** all fixed `tests/mutate-probe.sh`, not the port — the harness round 4 CREATED
alongside the lease port. (An earlier version of this line said "rounds 4-7", which was wrong
twice: round 4 ported four functions into `gates.py`, and it created the harness rather than
fixing it. The section heading and its own first sentence disagreed.) `gate-check.mjs` remained
UNSTARTED throughout — not stalled, never begun.

That is worth knowing before deciding how much more of this to run: the reviews kept finding
real defects, but in infrastructure built to review the port rather than in the port.
**Recommendation to the USER, not yet approved: narrow the review gate to commits touching
`scripts/lib/`.** The durable evidence is the per-fork cost, which rose monotonically across
the nine forks — roughly 315k, 326k, 327k, 390k, 419k, 459k, 516k, 532k, 552k subagent tokens,
each fork inheriting the full context. (A "12.3x spike over the session median" figure cited
here before was a 5-minute burstiness statistic from one heartbeat, session-local and not
re-derivable by any future reader.)

**Bullet convention, because these labels have been used both ways:** R5/R6 name the round
whose REVIEW found the defect; R7/R8 describe what that round's commit DID.

- **R5** — the harness reported REDDENS for ANY anchor while the tree was already red (no
  baseline check), and my invocations passed anchors through `"$(eval echo $old)"`, which
  word-splits and strips ALL leading whitespace. Four lease controls ran on anchors I did not
  write and matched only by luck of uniqueness.
- **R6** — every verdict exited 0, so nothing could gate on it; dropping the runner argument
  entirely reported a confident `NOTHING REDDENED` about code never executed (`"$@"` empty is
  a no-op exiting 0, exempt from `set -u`).
- **R7 — a NET DELETION, and the lesson is about judgment.** The 0/1/2/3 exit-code protocol
  added in R6 had NO caller anywhere in the repo, and its `DIFFERS` branch shared exit 0 with
  `REDDENS` — reinstating in the same commit the "distinction lives only in prose" flaw the
  codes were added to remove. **I implemented a review's PREMISE ("nothing can gate on this")
  instead of a need.** Deleted, along with a success-marker guard that was exactly redundant
  with the exit-status check. `PROBE FAILED` keeps exit 1.
- **R8** (`3d85eaa`) — Ctrl-C made the probe print a verdict about UNMUTATED source: the EXIT
  trap restored the file, then execution fell through to the verdict block. INT/TERM now exit
  130. Two lessons beyond the fix. First, my initial "it works" reading was wrong — a
  background job from a non-interactive shell has SIGINT ignored, so the absent verdict proved
  nothing; SIGTERM is the valid test. Second, verifying it exposed a defect introduced in the
  SAME edit: EXIT still runs after the signal handler, so `restore()` ran twice and the backup
  cleanup added beside it made the second call fail and falsely report "may still be MUTATED".
  `restore()` is now idempotent. **A fix and its own verification landing together is how a
  two-part edit hides its second half.**

**Standing limit, stated rather than papered over:** a baseline that merely exits 0 is not
proof the runner can OBSERVE anything. A true positive control needs a canary mutation known
to diverge, run once per batch. It does not exist — do not read `NOTHING REDDENED` as proof of
no coverage without one.

**`lease-diff.sh`'s two checks are COMPLEMENTS** (its comment previously claimed the opposite,
which is the sentence a reader would have trusted). Both halves measured: a degraded PORT
leaves the oracle-derived counts untouched, so the gate passes and the DIFF fires; a degraded
SEQUENCE makes both sides agree, so the diff passes and the GATE fires (`VACUOUS: ok=3
conflicted=8 released=1`).

### ROUNDS 2-4 — every review of a fix found the fix defective
**The base rate is the finding.** Rounds 2, 3 and 4 each found the PREVIOUS round's fix wrong.
A fix ships under the authority of "a review found this", and a control proving the intended
row MOVED is not evidence the new behaviour is RIGHT.

**Round 4's headline, the worst defect of the session: a mutation control was measuring a
SYNTAX ERROR.** The anchor omitted the `for` line above it, so the mutant was an
IndentationError; `gates.py` would not import, the driver exited non-zero on all 9 variants,
and the probe counted CRASH as reddening. It reported "reddens 9" for a fix that had NO
coverage — every corpus row was a string, so the behaviour was unobservable either way.
`tests/mutate-probe.sh` now enforces: unique anchor, edit must change the file, **mutant must
still import**, and only DIVERGE counts. A crash is INCONCLUSIVE, never a catch.

Also round 4: that fix's guard was REMOVED as redundant (`os.fspath` rejects the same values)
and its message was invented; `_node_message_error.__str__` raised `AttributeError` under
`copy.copy`; a subclass named `Node`+base leaked into tracebacks.

**Round 3's** three defects all traced to one habit — reaching for a Python convenience where
the oracle has semantics (`str()` coercion, an integral float printing as `1.0`, a bare
`OSError` destroying `FileNotFoundError` and `.errno`).

**"Reddens N" counts ROWS, not defects.** A corpus row is identical in every tree, so one
divergence in it reports as nine. Say "1 row, all trees".

### ROUND 2 — the review of the fixes found the fixes defective (36e3785)
The lesson worth keeping: **a fix ships under the authority of "a review found this", and a
control proving the intended row MOVED is not evidence the new behaviour is RIGHT.**
- `_js_join` was wrong before it normalized anything. `os.path.join` DISCARDS everything
  before an absolute segment (that is `resolve`'s rule, not `join`'s), so `("/a","/b")` gave
  `/b` against node's `/a/b`. Plus `("a","")` -> `a/` vs `a`, and `()` raised vs `.`.
- A 3000-case RANDOMIZED differential then found a fourth class no hand-picked case reached:
  POSIX preserves a leading `//`, node collapses it. Three rounds of hand-picked cases missed
  what one randomized run caught in seconds — the same lesson as `js_string`'s exponent band.
- That fix reddened ZERO variants, so it is now a permanent `jsJoin` corpus in both drivers.
  A fix nothing can catch regressing is not finished.
- `head -4` on a Python traceback cut off the assertion message, which lives on the LAST line.
- I over-applied `except Exception` beyond what the finding asked; narrowed back where a bare
  catch would turn a typo into "no pipelines configured".

### NEXT ACTION
**Every NAME `gate-check.mjs` imports exists in `gates.py`** (23/23, sound presence test).
That is a name claim, NOT signature compatibility. **The port is FAITHFUL to the oracle's
options-object shape by default** — `stat_current_named_file`, `resolve_target`,
`claim_leases` and `release_leases` all take a positional dict with camelCase keys, exactly as
their oracles do. **Three functions deviate**, flattening the options object into keyword
arguments, and those are the ones a `gate-check.mjs` port must translate differently:
| oracle | port |
|---|---|
| `readStableRegularFile(path, {root, maxBytes, label})` | `read_stable_regular_file(path, max_bytes=…, label=…, root=…)` |
| `writeAtomic(file, text, {root})` | `write_atomic(file, text, root=None)` |
| `withFileLock(root, target, fn, {timeoutMs})` | `with_file_lock(root, target, fn, timeout_ms=…)` |
(An earlier version of this table listed `stat_current_named_file` as the odd one out. It is
the FAITHFUL one; the deviations are the kwargs trio.)
`claimLeases` / `releaseLeases` / `withFileLock` are `await`ed at every JS call site and are
SYNC here (mutual exclusion is the file lock, not the scheduler, so this is not a behavioural
difference — but every call site has to drop the await). One item left:

1. **`gate-check.mjs`** (950 lines). Its own ~30 private functions are the work now, not the
   library: `parseArgs`, the approval store (`recordApproval`, `approvalExists`,
   `readApprovalFile`, `validatedApprovalDir`, `assertPrivateApprovalEntry`), the runner
   (`runCheck`, `runRolling`, `safeRegexMatch` and its Worker), and evidence rewriting
   (`insertOrUpdateEvidence`). It also spawns two siblings needing their own ports —
   `lib/check-supervisor.mjs` and `lib/regex-worker.mjs` — plus `lib/process-tree.mjs`, ported.

   **Use `tests/mutate-probe.sh` for every control, and pass anchors as ordinary QUOTED
   arguments** — never `"$(eval echo $old)"`, which strips all leading whitespace. Read the
   verdict from stdout and do NOT pipe the script through a filter: a pipeline returns its
   last command's status, discarding even `PROBE FAILED`. Hand-rolled probes produced **THREE**
   false results in this port — a syntax error counted as nine catches; a non-unique anchor
   that never mutated yet reported `NOTHING REDDENED`; a dropped runner argument reported the
   same — and carried **two more** defects caught before they could produce one: anchors
   stripped by `eval echo` (every verdict happened to still be correct), and REDDENS for any
   anchor while the tree was already red (argued from the code, then confirmed by a
   deliberately constructed test AFTER the guard existed). The harness now guards all five.
   An earlier version of this line called all four "false results", which asserted more than
   the evidence showed — the exact overstatement this project keeps correcting elsewhere.

   `runRolling(tasks, limit)` is the first place the sync-vs-async choice stops being free:
   it is bounded concurrency over promises, so a synchronous port needs threads or processes
   and the interleaving of check output becomes a NEW divergence surface. Decide that before
   writing it, not during.

**Completeness, re-measured soundly (2026-09-07).** `gate-check.mjs` imports **23** names from
`lib/gates.mjs` and **none** is missing from `gates.py`. The presence test is now
`^(def |class )<name>\b` or `^<name>\s*[:=]` — not the earlier `^(def |_?)<name>\b`, which
reduced to "the name appears at column 0 on any line" and could only produce a false PRESENT.

Two measurement errors happened on THIS claim, both worth remembering because each produced a
confident wrong number:
- the loose presence regex vouched for `MAX_CHECK_OUTPUT_BYTES` and `MAX_AUTOMATIC_EVIDENCE_CHARS`
  on evidence that could not distinguish a definition from a mention (they are genuinely at
  `gates.py:605-606`, verified since);
- extracting the import block with `.*?` under `re.S` SPANNED the earlier `node:fs`,
  `node:path` and `node:os` blocks, reporting "42 imported" with fragments like
  `sep } from "node:path"; import ...` as missing names. `[^{}]*` cannot cross a block.

`scope_files` / `legacy_files` / `read_leases` are ported but have ZERO callers in `scripts/`
or `tests/` — shipped because they are exported, not because anything exercises them.

### REVIEW FINDINGS — all verified first-hand; fix commits listed per round below
NINE adversarial reviews so far (this list drifts every round — check `git log` rather than
trusting the count). Port-side fixes: `e547741`, `36e3785`, `50788cd`, `f3a4c86`. Harness-side:
`272df2b`, `e98441e`, `c952509`, `3d85eaa`. Every finding was re-measured here rather than taken on report,
and each is fixed with a control proving the fix was load-bearing. The fourth review found
the THIRD review's fix defective -- see round 2 below.
Kept as a record of the defect CLASSES, since every one of them shipped green.

1. **F1 — CONFIRMED, a real divergence.** `_markdown_discovery` interpolates `_err_code(error)`
   where the oracle interpolates `error.message`. Measured on a `chmod 000` gates/ directory:
   ```
   JS: cannot inspect gate directory <R>/…/gates: EACCES: permission denied, scandir '<R>/…/gates'
   PY: cannot inspect gate directory <R>/…/gates: EACCES
   ```
   `_err_code` is the right idiom for the oracle's `error.code` — but this site uses `.message`,
   and no variant built an unreadable directory, so the branch was never driven. **Fix:**
   `f"{code}: {os.strerror(n).lower()}, scandir '{path}'"` — measured EXACT against node for
   EACCES / ENOENT / ENOTDIR (NOT the complete set -- see round 2). FIXED with an
   `unreadable-gates-dir` variant; the runner restores the mode in a shell `trap ... EXIT INT
   TERM` so an aborted run cannot leave a 000 directory behind.
   **My own first probe here was NON-DISCRIMINATING**: I read `scopeFiles`, which returns
   `discovery["files"]` and structurally cannot carry an error, and read the resulting match as
   agreement. The errors live in `targets.scopeApi.discoveryErrors`.
2. **F6 — CONFIRMED, delete it.** `mk(os.path.join(state, "."))` in `build-discovery-tree.py`
   creates NOTHING (measured: the directory stays empty), while its comment implies a `.` entry
   is under test. Exactly the row-claiming-an-unreached-mechanism class.
3. **F5 — the attribute branch of `same_file_identity` has zero coverage.** All three driver
   rows pass dicts, so `getattr(value, "st_" + name)` never executes — and that is the branch
   every REAL caller takes. The dict-or-attribute dispatch is also an invention with no
   counterpart in the oracle. Add a row passing two real `os.stat_result`s.
4. **F7 — the differential is blind to EXTRA or MISNAMED keys.** Both `target()` helpers project
   onto the same fixed six-key list, so a typo'd `"ambigous"` or an extra key diffs as nothing.
   A MISSING key IS caught. Assert the Python result's key set is a subset of the six.
5. **F2 — non-UTF-8 filenames decode differently and `js_sort_key` cannot fix it.** Node maps
   undecodable bytes to U+FFFD; Python's `os.scandir` to surrogateescape U+DC80–DCFF. For
   `b"\xff.md"` the two sides hold DIFFERENT STRINGS before any sort, and `json.dump` then
   raises on the lone surrogate — so the port fails LOUDLY rather than silently, the good
   direction. The `js_sort_key` docstring's `surrogatepass` rationale is half wrong and should
   say this.
6. **F3 — `except OSError` is narrower than the oracle's bare `catch`.** `_named_entry(None)`
   raises TypeError where JS answers `true`; an embedded NUL raises ValueError where JS answers
   `true`. Unreachable from `resolve_target` today (the id charset excludes NUL), so latent.
   Note: an `errno`-less `OSError` is NOT a defect — `None != ENOENT` matches JS's
   `undefined !== "ENOENT"`.
7. **F4 — `js_sort_key` uses `str(value)` where JS's sort begins with ToString**, which this
   codebase already models as `js_string`. `js_sort_key(None)` keys on `"None"`, JS on
   `"null"`. Unreachable today, but the docstring claims general equivalence.

Checked and CLEAN, so nobody re-opens them: `zip` vs the `Math.min` loop (equivalent —
`normalize_owns_glob` guarantees a non-empty value); collapsing the oracle's two `return true`
arms; UTF-16-BE byte order vs JS code-unit comparison, including prefixes and the empty string;
and `abspath(join(root, f))` vs `resolve(root, file)` on POSIX for absolute / empty / trailing
separator / `~` / `..`.

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
