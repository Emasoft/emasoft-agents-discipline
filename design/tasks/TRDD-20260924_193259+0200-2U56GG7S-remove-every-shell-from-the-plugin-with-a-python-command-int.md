---
trdd-id: 2U56GG7S
title: Remove every shell from the plugin with a Python command interpreter and a JS twin
column: dev
status: tasked
created: 2026-09-24T19:32:59+0200
updated: 2026-09-24T19:53:04+0200
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
---

# Remove every shell from the plugin with a Python command interpreter and a JS twin

## ⤵ STATE — READ THIS FIRST ON RESUME (authoritative; supersedes the body) — 2026-09-24

Step 1 of the no-bash conversion (docs_dev/no-bash-spec.md "Steps" section) is IN PROGRESS /
landing in this commit: scripts/lib/cmdrun.py (the Python interpreter), tests/cmdrun_tests.py
(hand-written expected-table unit tests), tests/cmdrun_stress.py (randomized fuzz/stress test).
NEXT ACTION: step 2 -- the cmdrun.mjs JS twin plus a Node-vs-Python parity test. Steps 3-8
(ledger-check/gate-check wiring, tests/*.sh ports, git-hooks, docs, Windows push) are not
started. See the spec body below (pasted verbatim, source of truth until this TRDD's own body
supersedes a specific clause).


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

- The interpreter runs as its own process (entry `python cmdrun.py` / `node cmdrun.mjs`), request as JSON on stdin: `{command, cwd, env, mode: "ledger"|"gate", timeout_ms, output_limit}`; answer as ONE JSON line on its own stdout: `{status, refused?, reason?, stdout?, stderr?, timed_out, signal?}`. Stages never inherit the interpreter's stdin/stdout/stderr: ledger mode gives them DEVNULL; gate mode captures stdout/stderr through pipes into the interpreter (bounded by output_limit).
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
