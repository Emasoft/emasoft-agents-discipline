---
trdd-id: 2U56GG7S
title: Remove every shell from the plugin with a Python command interpreter and a JS twin
column: dev
status: tasked
created: 2026-09-24T19:32:59+0200
updated: 2026-09-26T07:31:20+0200
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
2026-09-26 PARITY SUITE DELIVERED (worker aed301c4, verified first-hand by coordinator: node --check clean, exit 0, 73 PASS / 0 FAIL / 19 DIVERGE rows / 0 FAIL (grep '^DIVERGE' counts 20 only because the suite's closing PROSE line 'DIVERGE rows are tagged knownDiverge...' starts with the word DIVERGE -- the SUITE's own summary says 19, matching the report; coordinator's earlier 20 was the unfiltered grep), npm test re-verified exit-0): tests/cmdrun_parity.mjs runs the SAME command through both transports and compares semantic fields only; PYTHON env override; per-row mkdtemp; exit 1 on unexplained FAIL. The 19 DIVERGE rows turn PASS automatically when the twin fixes land (tags then removed). Baseline classes: (A) 16 rows real-binary spawn => js 124/timed_out vs py real status -- the onDeathPipeEof suicide; (B) 2 rows tokenizer folds ';' into a word where py refuses 'lone ;'; (C) 1 row 127 stderr text empty in js ('command not found' message missing); plus collateral: js timed-out run leaves the redirection target created-but-0-bytes in the shared cwd. Findings recorded but NOT defects: [!..] bracket negation unreachable BY DESIGN on both runtimes (unquoted ! refused; spec's [!..] promise needs either a bracket exception or a spec edit); empty-command is bad_request not refusal on both. Report: reports/no-bash/20260926_071046+0200-cmdrun-parity-suite.md. NEXT: twin-fix round (death-detection vs pump + ';' tokenizer; 127 message: FIX to match py -- the decision is closed because DIVERGE=0 admits no surviving tags) whose acceptance INCLUDES: reword the suite's closing prose line so it does NOT start with the counted token 'DIVERGE' (cmdrun_parity.mjs closing summary block -- the grep trap), smoke re-run, then parity re-run with 0 DIVERGE and ALL knownDiverge tags removed from the suite (a stale tag would green-light a regression forever -- finding 4), then ADD test:parity to npm test's chain + the CI cmdrun-proof job in the same commit (finding 2: nothing automated runs it today), then commit+push with cancel discipline. PUBLISH FREEZE until DIVERGE=0 (finding 1, round-2 ceiling stated): the suite exits 0 with DIVERGE rows present by design, so a publish before the twin fixes land would ship the defective twin; NO PUBLISH FROM ANY BRANCH CARRYING THE TWIN (main and winproof alike) until the tag count is 0. This directive binds card-reading sessions only -- no publish is pending and main is not auto-publishing, so the card is sufficient NOW; when a publish becomes imminent the hard guard goes into the publish gate's pre-publish test step (assert DIVERGE==0) or a version bump gated on it -- that location is named here so it is not re-derived later. Coverage honesty (finding from review): the parity suite's own win32-gated branches have never executed anywhere, same gap the card records for cmdrun_tests.py. OPEN DECISION (finding 6, not settled): [!..] bracket negation -- spec promises fnmatch [!..] semantics but the grammar refuses unquoted '!' on BOTH runtimes; needs an explicit decision (grammar bracket-exception OR spec edit), recorded here as OPEN, not decided. TWIN DEFECTS (localized by coordinator smoke rows, still unfixed): cmdrun.mjs answers builtins/refusals correctly (4-row smoke: 3 SAME) but ANY REAL-BINARY stage hangs the supervisor to its deadline (124) -- worker side: worker+real binary dies SILENT rc=1 before answering (builtin rows answer fine). Mechanism: the worker's pumping executor + death-pipe detection interact badly -- onDeathPipeEof (cmdrun.mjs:2001-2014) fires spuriously during real spawns (fd0 zero-read misread as EOF per the pumpTicks design comment at :1989), worker SIGKILLs its own group pre-answer; the supervisor then waits out the clock because the killed worker's stdout never 'end's and exitCode!=null&&readableEnded check at :2389 can't complete. This is the SAME AREA the dead worker was restructuring (waitChildSync/waitChildPumping) when it died -- its instinct was right, its uncommitted rewrite is gone. ALSO: js tokenizer bug -- ';true' style input folded ';' into a word ('/dev/null;' open attempt) where py REFUSES: separate tokenizer divergence, one-line class. Fix-direction guidance (supersedes the older NEXT sentence that carried it -- that stale sequencing is EXCISED; the new NEXT above is the only sequencing authority): fix = make death detection not fight the pump (e.g. poll stdin via the pump's zero-read ONLY after EAGAIN, or move death watch to a thread).

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
