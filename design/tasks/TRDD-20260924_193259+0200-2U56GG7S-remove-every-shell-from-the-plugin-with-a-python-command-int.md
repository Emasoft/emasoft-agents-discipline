---
trdd-id: 2U56GG7S
title: Remove every shell from the plugin with a Python command interpreter and a JS twin
column: dev
status: tasked
created: 2026-09-24T19:32:59+0200
updated: 2026-09-26T23:24:20+0200
current-owner: main-agent@agents-discipline
created-by: user
task-type: refactor
min-approval-requirement: none
assignee: user
mandate: true
mandated-by: none
approved: true
approval-judge: user
approval-datetime: 2026-09-24T19:32:59+0200
implementation-commits: 10d15aa 50075fb 6e48a5e daa61ca
---

# Remove every shell from the plugin with a Python command interpreter and a JS twin

## ⤵ STATE — READ THIS FIRST ON RESUME (authoritative; supersedes the body) — 2026-09-25

Step 1 of the no-bash conversion (docs_dev/no-bash-spec.md "Steps" section) was REDONE as the executor v2 design (docs_dev/cmdrun-v2-executor-spec.md, supervisor + anchor worker) after an adversarial audit and advisor review found the v1 executor hung/leaked/lied under 10 High findings -- see the '## Executor v2' section below.
cmdrun.py, tests/cmdrun_tests.py and tests/cmdrun_stress.py are all v2 now: pyright/ruff clean,
130 unit-test rows green (one per audit finding H1-H10/M1-M11/L1-L5 and advisor probe
P1/P2/P6/P7), 3-seed stress run green at --cases 300.
DONE: coordinator ran the full stress run after the hardening round landed -- unit suite
193 PASS / 0 fail, stress 6702 checks all green (3 seeds x 733 cases), commit daa61ca.
NEXT ACTION: Windows proof (needs user OK to push a branch) BEFORE step 3 -- it MUST address the W3 settle-race requirement in '## Contract' item (5) below (bounded retry window for the TerminateJobObject/ActiveProcesses query, or measure the race first). Then step 2 (JS twin) implements '## Contract' below. Steps 2 through 8 are not started.


---

# No-bash conversion — specification (source for the TRDD; the TRDD is authoritative once minted)
2026-09-26 CI round: branch trdd-2U56GG7S-cmdrun-winproof pushed 5x (27fd163..0a8bf28); runs 1-3 fixed macOS (BSD-sed _scrub), ubuntu (fixture probe-spelling), F6 startup race; run 5 (36195202384) is the FIRST cmdrun-on-Windows execution. HELD on main unpushed: 34bdf4f (comment-only; push with the next real change). RULE for the next CI push on this branch: gh run cancel any in-flight run the new SHA supersedes BEFORE trusting the new run as the verdict (run 4 36194907073 burned because superseded, not cancelled). Branch cleanup PR-vs-delete still undecided -- delete the branch only after the Windows verdicts are recorded in this card.
2026-09-26 review corrections to the CI-round lines: (1) CANCEL RULE made actionable -- AFTER pushing to the branch, immediately run: gh run list --branch trdd-2U56GG7S-cmdrun-winproof and gh run cancel every run except the newest (zero judgment; the old wording required knowing which SHA supersedes which). (2) run 5 is the first ATTEMPT at a cmdrun-on-Windows execution -- in flight at the time of writing; a queue/infra failure leaves the first still unclaimed. (3) The branch carries NO unique commits (every push was main:branch mirroring), so deleting it after the Windows verdicts are recorded loses nothing; the verdicts that gate deletion = the cmdrun-proof windows cell's verdict AND the ledger-tests Windows outcome, whatever color each is, recorded in this card. (4) F6 caveat: the poll fix NARROWED the startup race (250ms vs 50ms) and run 3 verified it green on ubuntu/macos; the deterministic fix (test-hooks 'watch installed' stderr line) is still future work.
2026-09-26 RUN 5 VERDICT (36195202384): cmdrun-proof ubuntu+macos SUCCESS; windows cell ran the suite (first real execution) -- 17 PASS rows covering the core grammar (words/quotes/escapes/$VAR/prefix-assign/&&/( ) groups, tree_kill:'job' correct) then CRASHED at the first file-redirection row: line 342 open(out1) FileNotFoundError -- the  run never created out1.txt (redirection target absent on win32; cause unmeasured -- likely the redirection-open path or the stage spawn; this IS the Windows measurement the round was for). Also: port worker found cmdrun.mjs executor defect mid-write (Atomics.wait blocks the event loop so proc.exitCode never updates -- executor must be async); worker died on low credit, fix pending.
2026-09-26 round-8 review corrections to the run-5 lines: (1) the Atomics.wait executor diagnosis is an UNVERIFIED worker hypothesis -- reproduce it first with a plain 'node -e' child on the POSIX host (the mechanism, if real, is platform-independent) before any async rewrite; 'every smoke test that worked used builtins' does not discriminate it from a fixture/spawn cause. (2) The Windows crash search space is THREE causes, not two: redirection-open, stage spawn, AND the fixture itself -- the row embeds single quotes ('import sys; ...') which on Windows are LITERAL characters, not quoting; that fixture spelling is the first of its kind in the file, consistent with everything before passing. Falsifier: same command with double-quote escaping fails on POSIX too => fixture bug, not win32. (3) The crash point is row ~18 of ~130: ~112 rows (redirections, pipes, timeouts, output_limit, the M/W/H Windows legs) never executed ANYWHERE -- the Windows verdict is mostly UNKNOWN, not mostly-known. The measurement round is one sample.
2026-09-26 REPRODUCED (coordinator, POSIX host, minimal case): a spawn child polled via Atomics.wait-spin on proc.exitCode never observes exit -- exitCode stayed null past 3s (220 spins) while a plain 'sleep 0.2' exited immediately. The worker's Atomics.wait diagnosis is VERIFIED as a mechanism: Node updates exitCode on the event loop, which Atomics.wait blocks. The cmdrun.mjs executor must await the event loop (proc 'exit' event / async wait), not spin. Fixture-vs-win32 question for the Windows crash row remains open (falsifier unchanged: re-spell the fixture's single quotes and re-run).
2026-09-26 round-9 review repairs + two verifications: (1) CONTROL run discriminates -- same child, NON-blocking 10ms setTimeout poll observed exit code 0 at 210ms on the first check; combined with the blocking-spin hang, the verified mechanism is the GENERIC one (any event-loop block starves exitCode), and Atomics.wait is UNISOLATED -- one member of the class, not the proven culprit. (2) cmdrun.mjs ON DISK contains ZERO Atomics.wait in any wait path (grep: only a comment at line 2308 warning AGAINST it) and ZERO waitChildSync -- the on-disk executor's wait is waitChildAsync (proc.once('exit'), event-loop-based, cmdrun.mjs:1320-1327), already the correct shape. The dead worker's waitChildSync/waitChildPumping names describe ITS OWN uncommitted edit-in-progress, not the committed file. Conclusion: the twin rewrite must preserve waitChildAsync as the only wait primitive; the worker's 'executor must be async' fix direction matches what the committed code already does -- VERIFY the committed executor is fully async end-to-end (no sync wait anywhere) rather than rewriting it. (3) 'a plain sleep 0.2 exited immediately' in the earlier append was an overclaim: nothing in that repro observed the child exit; the control run now supplies that observation.
2026-09-26 fixture falsifier EXECUTED (coordinator, POSIX host, json.dumps-built request exactly as the harness does): the single-quoted fixture WORKS on POSIX -- status 0, stdout 'E' captured, out1.txt created. The fixture spelling is NOT inherently broken; the Windows crash is win32-specific. Search space narrows to: (a) win32 redirection-open path (os.open of an absolute Windows path with backslashes), (b) win32 stage spawn/argv quoting of the -c argument, (c) any win32-only cmdrun code divergence. The on-disk cmdrun.mjs verified: syntax-clean (node --check), 2614 lines, exactly ONE spawnSync (taskkill on Windows tree-kill, correct), waitChildAsync the only wait primitive -- coherent, not a torn rewrite. Parity test (tests/cmdrun_parity.mjs) still MISSING: the worker died before writing it. [SUPERSEDED -- delivered and verified the next day; see the later line.]
2026-09-26 TWIN FIXED AND PARITY GREEN (wholesale contraction per the card editing rule; supersedes the pre-fix localization of the former line 44, which lives in git history at cde827b..0847349; the disproven pump theory -- onDeathPipeEof/pumpTicks/supervisor-exit-check refs -- is recorded only as 'disproven' here; probe details: reports/no-bash/20260926_093639+0200-twin-fix-round.md). STATE: the JS twin's three defect classes are FIXED, commits 6d20038 + 9c41644, verified first-hand by the coordinator -- parity suite 92 PASS / 0 FAIL / 0 SKIP, ZERO DIVERGE rows, all knownDiverge tags + KNOWN_* constants + DIVERGE print branch REMOVED (no output line begins with the token DIVERGE), node --check clean, npm test exit 0 (full chain, 0 FAIL), row-name diff before/after the tag purge is EMPTY (59 names both sides -- no row removed; the 73->92 growth is previously-diverging rows completing their checks). ACTUAL ROOT CAUSE (the card's earlier pump/onDeathPipeEof zero-read hypothesis was DISPROVEN by probes; the fix is the OPPOSITE shape -- the pump was untouched): the JS supervisor called proc.stdin.end() right after writing the request, closing the death pipe's write end while the supervisor was ALIVE, so the worker's process.stdin 'end' fired at startup and it SIGKILLed its own group; builtins escaped because they complete on microtasks before the first I/O poll; the py oracle never closes its stdin -- the kernel closes the pipe when the supervisor dies, which IS the death signal. Fix: the supervisor holds the write end for its whole life (comment at the site). The fix exposed two latent capture bugs the 124 answers had masked -- gate-mode last-stage stdout spec'd onto the worker's raw fd 1 (the ANSWER pipe) and 2>&1 dups keyed on the wrong fd -- both fixed in runPipelineGate and runSingleStageCapture (a dup of the stdout capture slot gets its own pipe whose OutputSink joins sinks.out). Also: ';' added to the word-break set (py's break set is "();#\n\r"); _gateStderrSink hook routes gate-mode WorkerFd(2) writes into the answer's stderr (lifecycle invariant recorded at the definition; comment-only remedy accepted by review -- no guard, one-process-one-role architecture). Parity harness fixes: seq rows replay per-transport in their own subdirs (they cross-contaminated in one shared dir); cd-row strengthened to wantStdoutEnds 'sub\n' (real output, not mere transport agreement). WINDOWS UNVERIFIED (macOS host; taskkill/Job Object legs and the capture-spec's win32 fd branches are what the winproof push measures). NEXT (one list, one gate): (1) ADD test:parity to npm test's chain + the CI cmdrun-proof job (nothing automated runs it today); (2) push the winproof branch WITH cancel discipline (gh run cancel every superseded run before trusting the new one) -- its cmdrun-proof windows cell is the Windows verdict for both this card and TRDD-REJRD8V5's port; (3) record the Windows verdicts in this card, then decide branch PR-vs-delete. PUBLISH FREEZE (lifted): DIVERGE=0 is true and the tags are gone -- the freeze's premise no longer holds; the hard-guard location (pre-publish DIVERGE==0 assert in the publish gate's pre-publish test step) stays recorded as the guard to install when the publish gate is next touched. CARD EDITING RULE (survives the rewrite, load-bearing HERE because its only guaranteed reader is this line's next editor -- do not relocate): this line is edited WHOLESALE only -- no --expect span patches; every edit replaces the entire line and is followed by one full-line read before commit; span patches are prohibited, a review may sanction a specific patch, each sanction is recorded at the next touch (sanction ledger: adbd4c8, 0847349 -- both review-sanctioned; the one-behind lag this rule's wording now closes). The one-NEXT invariant is scoped to the STATE block (a body 'NEXT:' is prose, not a directive). OPEN DECISION (unchanged, not settled): [!..] bracket negation -- spec promises fnmatch [!..] semantics but the grammar refuses unquoted '!' on BOTH runtimes; needs an explicit decision (grammar bracket-exception OR spec edit). Coverage honesty (unchanged): the parity suite's own win32-gated branches have never executed anywhere, same gap the card records for cmdrun_tests.py; the capture-spec's resolve-through-dups heuristic has NO nested-dup-chain coverage (the four flat 2>&1 shapes only) -- a banked task, not a defect.
2026-09-26 POST-REVIEW ROUNDS: commit 293c175 landed (3 review rounds on e3aa1ef's follow-ups): catch scoped to OS-shaped errors, EAGAIN stall capped at 10s no-progress, rescue parse now parses accumulated chunks with first-line split, /dev/fd/N refusal throws unmapped-coded EDEVFDREFUSED (byte-identical message to py), suite 99->100 rows (new cmp:[stderr] row pins the refusal path). Landed-commit review: no revert; follow-ups queued as session task #2 (ERR_* misroute tightening, known-divergences ledger, dead arm cleanup); supervisor EPIPE-after-stall verified already-wrapped (cmdrun.mjs:2586-2595). NEXT: task #8 - win32 crash is the TEST's construction (cmdrun_tests.py:341 embeds backslash path unquoted; oracle's Windows-path refusal cmdrun.py:375 correctly refuses; out1.txt never created). Fix = quote the path or use forward-slash relative name in the test command.
2026-09-26 21:47 — Run 9 fixes LANDED as b1e36fa (cd-leak MSYS premise, M8 CRLF strip, leak/F7 platform split + F8 rename; review verdict 'land all four, no blockers' — its two comment recordings applied in-commit: confirmed-empty>leaked clause, _win_kill_job docstring divergence recorded pending cmdrun.py docstring fix at next touch). Local py-suite 304 PASS exit 0. Mirrored to winproof (1b065d8..b1e36fa); run 10 = 36267107130 queued, monitor armed — its windows cmdrun cell is the verdict on the 4 failing rows. ubuntu cell green since run 9.
2026-09-26 21:58 — RUN 10 verdict (36267107130): cmdrun_tests.py WINDOWS CELL GREEN — all 4 run-9 fixes PASS (cd-leak, M8, leak-124 shape confirmed {status:124,timed_out,tree_kill:job}, F8), 201 PASS, clean exit; ubuntu/macos green. The failure moved DOWN the chain: python-lib-checks.py crashed win32 — systemic encoding defect (21x subprocess text=True decodes node's UTF-8 child stdout as cp1252; unicode-ids fixture Cyrillic 0x81 undefined → reader-thread crash → json.loads(None) TypeError :771 → truncation; jsapi_drive.py emoji stdout can't encode to cp1252) + one real row divergence (gates_helpers mode 666 vs 777 — mode&0o777 meaningless on win32, needs split). Review verdicts on b1e36fa+dc57e6b: no revert; owed: win32 leak row assert leaked-shape ABSENCE (fields now confirmed), b1e36fa message item-4 prose is wrong (no W3/I2 changes in that diff). Windows ledger-tests cells red, out of scope. NEXT: fix python-lib-checks encoding + mode rows, re-mirror, run 11.
2026-09-26 22:30 — RUN 11 cancelled superseded (review round 4 predicted its windows red deterministically: 7923db6's win32 mode row re-pinned the cross-runtime comparison as an assertion); amended as ddf950e (within-runtime agreement, renamed). RUN 12 (36269469828, ddf950e) in progress — consume normally. OWED (durable, survives /clear): (1) round-5 guard for the mode row — unconditional _mode_py[0][1] indexing IndexErrors when the py driver fails (_mode_py==[]), converting a clean FAIL into a run truncation; fix = len(_mode_js)==2 and len(_mode_py)==2 before indexing; comment must state win32 st_mode is synthetic (leaf-only-mode defect class unobservable; row is a within-runtime tripwire, POSIX branch carries real coverage) — being applied this session, commit will follow. (2) _win_kill_job docstring divergence (promises leaked:true at zero budget; code answers 124-confirmed-empty) — fix at cmdrun.py next touch. (3) shape-absence control (cmdrun_tests.py win32 leak row) is vacuous-on-{} and derives force from its paired 124 row — NIT comment owed at that file's next touch. (4) b1e36fa commit-message item 4 W3/I2 prose error — recorded here, no code correction possible. (5) PYTHONIOENCODING stage-spawn propagation (cmdrun default-env stages now emit utf-8) is an unpinned latent fix, left as-is.
2026-09-26 22:35 — RUN 16 (36272470124, d9dfbdf): windows cmdrun-proof cell DOWN TO 2 FAILS. gates_helpers symlink row GREEN — the named-entry type assert in gates.py read_stable_regular_file (assert-order parity, product fix d9dfbdf) closed it cross-runtime byte-for-byte. Probe record (runs 14-15): CRLF fixture bytes IDENTICAL both runtimes (in-process cause); py lstat on win32 reports S_ISLNK=True (primitive parity falsified H1). macos node-20 green again this run (the 250ms budget flake did not recur). REMAINING windows fails: (1) dispatch logWarning row — task #4 re-scoped: ORACLE js readState refuses REGULAR dispatch.json on win32 (py exercised the designed logWarning path correctly; NOT a port defect); (2) parse_gates crlf-duplicates eol divergence — in-process, js=\r\n vs py=\n on win32 with identical input bytes; mechanism still open, next probe must instrument the two drivers' read paths on win32. Probe-row lifecycle obligation: convert probes to assertions or remove once each mechanism closes. Review ledger: d9dfbdf verdict no-revert (refusal-set argument); 'every platform' wording residue recorded; _probe_dir try/finally + probe comment clause owed at next touch.

## User directives (verbatim)

- 2026-09-23: "can't you reproduce the same mechanisms using python? i don't want a single bash script in the whole plugin. find a way to preserve the mechanism using python, no matter what."
- 2026-09-23: "consider that this very plugin must run on windows too. zero bash support. no unix like shell at all."
- 2026-09-23: "instead of handiling commands to the shell, reproduce the command functionality in python."
- 2026-09-24: "i told you to convert everything bash to python. why there is still bash code in the repo? ... decide yourself about the rest after making tests and confirming facts, finding edge cases and put the algorithm on stress-test. it must never fail or hung or go looping."

## Decisions (made by the coordinator on the user's delegation, 2026-09-24)

- D1 = (b): each runtime gets its own shell-free interpreter — `scripts/lib/cmdrun.py` and a JS
  twin `scripts/lib/cmdrun.mjs`. Reason: SKILL.md/README promise a zero-dependency Node tool;
  (a) Node-calls-Python adds Python to it and makes the oracle depend on the port; a Node-vs-Python
  parity test proves the twins agree. Evidence: reports/no-bash/20260923_233446+0200-fable-advisor-verdict.md.
- D2 = refuse `.bat/.cmd/.ps1/.sh` targets; on Windows resolve `npm`/`npx` to
  `node <node-dir>/node_modules/npm/bin/{npm,npx}-cli.js`. Never run cmd.exe or any shell.
- D3 = grammar below; everything else refused BY NAME.
- D4 = the test oracle for the interpreter is hand-written expected tables (no bash to generate them).

## Grammar (both twins, identical semantics)

Supported:
- Words; `'single'` (literal); `"double"` (escapes `\" \\ \$`, `$VAR`/`${VAR}` expanded); backslash escape outside quotes.
- `$VAR`, `${VAR}`: no word-splitting, no globbing of the result, undefined → empty. Env names case-insensitive on Windows.
- `VAR=value cmd` prefix assignments (apply to that command only).
- Globs `* ? [..]` on UNQUOTED words, relative to the current dir, sorted; no match → literal word.
- Pipelines `a | b | c`; `&&` chains; `( ... )` grouping. Precedence: `|` binds tighter than `&&`.
- Redirections per stage, applied left to right with dup semantics: `[n]>f`, `[n]>>f`, `<f`, `[n]>&m` (n,m in 0-2). `/dev/null` maps to `os.devnull`.
- Pipeline status = pipefail (last non-zero stage status, else 0). Both checkers use it.
- `#` at the start of a word begins a comment to end of line.
- Builtins (NOT allowed as pipeline stages — refuse with message): `cd DIR` (persists across top-level `&&`, not out of `( )`; no-arg refused), `true`, `false`, `echo [-n] args` (writes to the command's stdout, honouring its redirections), `test`/`[ ... ]` with only `-e -f -d -s PATH`.
- Executable resolution: hand-rolled — split PATH, apply PATHEXT on Windows, stat, pass the ABSOLUTE path to the spawn call. Never search the current directory; `./x` and `dir/x` run as written relative to the current dir. Command not found → exit 127 with message; on Windows add "use `python` or `py -3`" when the name is `python3`.

Refused, each with a message naming the construct: `||`, `;`, lone `&`, `|&`, `&>`, `&>>`, `<<`, `<<<`, backticks, `$(`, `${VAR:-x}` and every other `${...}` operator, `~`, `{ }`, `!`, newline / multi-line, empty or whitespace-only command, unterminated quote, targets ending `.sh .bat .cmd .ps1`, a builtin used as a pipeline stage.

## Execution model

- The interpreter runs as its own process (entry `python cmdrun.py` / `node cmdrun.mjs`), request as JSON on stdin: `{command, cwd, env, mode: "ledger"|"gate", timeout_ms, output_limit}`; answer as ONE JSON line on its own stdout -- the shape below in '## Contract (hardened, step-2 twin MUST implement this)' SUPERSEDES this earlier sketch (which named a 'signal?' key the answer never had and predates tree_kill/internal_error/leaked/bad_request and the 4 MiB cap). Stages never inherit the interpreter's stdin/stdout/stderr: ledger mode gives them DEVNULL; gate mode captures stdout/stderr through pipes into the interpreter (bounded by output_limit).
- Tree kill: POSIX — the interpreter calls `setsid()` first thing (or is spawned with a new session) and on timeout kills its whole process group; Windows — a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE via ctypes, each stage spawned CREATE_SUSPENDED, assigned, then resumed (post-spawn assign fails on GitHub runners already inside a job). If job creation fails, say so in the answer's reason; do not silently degrade.
- The CALLER also enforces the timeout (kill the interpreter's group/job) so a hung interpreter can never hang the checker.
- Exit codes: POSIX signal death → 128+N; Windows → raw unsigned code.
- A stage that spawns a background grandchild holding an inherited handle must NOT delay the answer (regression test: `node -e "require('child_process').spawn(process.execPath,['-e','setTimeout(()=>{},60000)'],{stdio:'inherit',detached:true}).unref()"`).
- Oracle/approval: gate-check's `oracle()` drops `shell` and gains `interpreter: "cmdrun/1"`, in the same commit.

## Must never fail, hang or loop

Every construct above gets unit tests with a hand-written expected table. Plus a stress test:
randomized command strings (grammar fuzz incl. adversarial quoting, deep `( )` nesting to a
bounded depth, huge argument lists, 10k-stage pipelines refused or bounded, binary output,
output flood past the limit, timeouts of 1 ms, processes that ignore SIGTERM, fork bombs bounded
by timeout) — the interpreter must always return one JSON answer within timeout + 2 s, never
raise an uncaught exception, never leave a descendant alive (process-table snapshot before and
after), and parsing must be linear-time (no regex backtracking on attacker text).

## Steps (one verified commit each)

1. cmdrun.py + unit tests + stress test (POSIX); Windows parts written and unit-tested where possible.
2. cmdrun.mjs twin + a Node-vs-Python parity test (Python/Node test file, no bash).
3. ledger-check (.mjs/.py) run acceptances through cmdrun instead of `/bin/bash -o pipefail -c`; the `||`/`;`/`&` regex refusals and the no-op detector move into or on top of cmdrun's analyzer.
4. gate-check supervisors (check-supervisor.mjs/.py) run CHECKs through cmdrun instead of `<shell> -c`; `--shell`/AGENTS_DISCIPLINE_SHELL retired; oracle gains `interpreter`.
5. Port every tests/*.sh (18 files) to Python, one commit per file or small group; a .sh is deleted only in the commit whose .py replacement passes; package.json scripts lose `bash`/`for` loops (use a Node or Python runner).
6. git-hooks/pre-push → Python with `#!/usr/bin/env python` (not python3).
7. Docs (SKILL.md, SECURITY.md, README, references) updated; CI stops excluding Windows from the diff suites.
8. Windows proof: needs the user's permission to push a branch (ask when step 1 lands).

Exit condition: `git ls-files | xargs file | grep -i "shell script"` is empty, no `bash`/`/bin/sh`/`shell: true`/`shell=True`/`cmd.exe` execution remains in scripts/ or tests/ or package.json, all suites green on macOS and Linux (and Windows once pushed).

## Approval log

- 2026-09-24T19:32:59+0200 — MANDATE issued by user (min-approval-requirement: none). Pre-approved: issuer authority >= required approver. No approval request was sent.

## Executor v2

Supervisor + anchor worker design; full spec: docs_dev/cmdrun-v2-executor-spec.md.
Two processes, one timekeeper: cmdrun.py is the threadless supervisor; it validates the
request, spawns cmdrun.py --exec (the worker) with start_new_session=True, stdin=PIPE
(the death pipe), stdout=PIPE (the answer), and writes the request as one JSON line.
The worker's pid IS the one process group of the whole command -- every stage it spawns
is a plain group-inheriting Popen, no per-stage groups. A daemon thread in the worker reads
stdin to EOF and SIGKILLs its own group the instant that happens, so a caller's SIGKILL of
the supervisor reaches the work automatically (closes H4).
The supervisor is the only timekeeper: on its own deadline, on SIGTERM/SIGHUP/SIGINT, or
worker exit without a valid answer, it repeats killpg(worker_pid, SIGKILL) until ESRCH under
one global cleanup deadline (never silent: leaked:true + pgid on exhaustion).
Windows: Job Object assigned to the worker BEFORE the request line is written; .COM/.EXE
candidates only; npm/npx trampoline to node + npm-cli.js. Every answer carries tree_kill.
Closes H1 H2 H3 H4(partly, residual documented) H5 H6 H7 H8 H9 H10 and all advisor probes
tested on POSIX (P1 P2 P6 P7); full findings-by-finding table in the step-1 report.
created-by: user is wrong -- a worker minted this card, not the human; the field is write-once.

## Contract (hardened, step-2 twin MUST implement this)

Round-2 hardening (commit 809f1b4, spec docs_dev/cmdrun-v2-executor-spec.md) changed the contract the JS twin must copy: (1) output_limit cap 0..4_194_304 (4 MiB; worst-case answer 2x4MiBx6B=48MB). (2) CMDRUN_* is a RESERVED namespace: the worker scrubs every CMDRUN_* key from the environment it hands to the child -- the request env, or the inherited environ when the request has none (the original F4 case); a prefix assignment (CMDRUN_X=1 cmd) is the SANCTIONED passthrough -- the Python code deliberately re-adds assigns after the scrub (cmdrun.py:1060-1062, commit 809f1b4 comment) -- and what makes hook inheritance inert is the --test-hooks argv gate, not the scrub. The three CMDRUN_TEST_* hooks fire only under --test-hooks argv. (3) Every answer carries tree_kill ('process_group' | 'job' in the Python runtime; the spec reserves 'taskkill' for the Node/Windows parity path -- each runtime's validator accepts exactly the values its own worker can emit: Python's accepts 'process_group'|'job' (never sees 'taskkill'); the Node runtime's must additionally accept 'taskkill'), including bad_request/refused/spawn-failure. (4) The supervisor validates the worker answer against an exact known-key shape before trusting it; keys: status, timed_out, truncated, tree_kill, refused, bad_request, internal_error, leaked, reason, stdout, stderr, pgid -- a new field must be added to BOTH runtimes in the same commit. (5) Windows: TerminateJobObject return checked, then QueryInformationJobObject accounting confirms ActiveProcesses==0; KNOWN RISK of a spurious leaked:true from the settle race -- the Windows-proof step must add a bounded retry window or measure it. (6) SIGCHLD reset to SIG_DFL at the top of main() (POSIX). Verified after fixes: unit suite 193 PASS / 0 fail.
