---
trdd-id: 2U56GG7S
title: Remove every shell from the plugin with a Python command interpreter and a JS twin
column: dev
status: tasked
created: 2026-09-24T19:32:59+0200
updated: 2026-09-25T12:55:04+0200
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

Round-2 hardening (commit 809f1b4, spec docs_dev/cmdrun-v2-executor-spec.md) changed the contract the JS twin must copy: (1) output_limit cap 0..4_194_304 (4 MiB; worst-case answer 2x4MiBx6B=48MB). (2) CMDRUN_* is a RESERVED namespace: the worker scrubs every CMDRUN_* key coming from the REQUEST env; a prefix assignment (CMDRUN_X=1 cmd) is the SANCTIONED passthrough -- the Python code deliberately re-adds assigns after the scrub (cmdrun.py:1060-1062, commit 809f1b4 comment) -- and what makes hook inheritance inert is the --test-hooks argv gate, not the scrub. The three CMDRUN_TEST_* hooks fire only under --test-hooks argv. (3) Every answer carries tree_kill ('process_group' | 'job' in the Python runtime; the spec reserves 'taskkill' for the Node/Windows parity path -- a validator must accept all three), including bad_request/refused/spawn-failure. (4) The supervisor validates the worker answer against an exact known-key shape before trusting it; keys: status, timed_out, truncated, tree_kill, refused, bad_request, internal_error, leaked, reason, stdout, stderr, pgid -- a new field must be added to BOTH runtimes in the same commit. (5) Windows: TerminateJobObject return checked, then QueryInformationJobObject accounting confirms ActiveProcesses==0; KNOWN RISK of a spurious leaked:true from the settle race -- the Windows-proof step must add a bounded retry window or measure it. (6) SIGCHLD reset to SIG_DFL at the top of main() (POSIX). Verified after fixes: unit suite 193 PASS / 0 fail.
