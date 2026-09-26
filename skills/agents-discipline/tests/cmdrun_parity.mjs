#!/usr/bin/env node
// Node-vs-Python PARITY suite for the shell-free command interpreter (cmdrun).
//
// Spec: docs_dev/cmdrun-v2-executor-spec.md "## Tests" -- the parity suite
// compares SEMANTIC fields only (status / timed_out / truncated, plus the
// reason text on refusal and bad_request rows) by running the SAME command
// through BOTH transports: scripts/lib/cmdrun.py (Python oracle) and
// scripts/lib/cmdrun.mjs (JS twin). A divergence is a bug in one of them;
// the suite does not say which -- it names the row and both answers.
//
// House style: plain asserts-by-comparison, console PASS/FAIL lines, exit 1
// on any FAIL, SKIP lines are printed but not counted. Zero dependencies.
//
// Each interpreter invocation is a FRESH subprocess with the request JSON on
// stdin and exactly one JSON answer line on stdout. Each row runs in its own
// mkdtemp cwd, removed in a finally block -- nothing is left behind on disk,
// and rows that could leave a process alive do not (the timeout row relies on
// cmdrun's own tree_kill, asserted via timed_out).

import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PY_SRC = resolve(root, "scripts/lib/cmdrun.py");
const JS_SRC = resolve(root, "scripts/lib/cmdrun.mjs");
// PYTHON overrides the interpreter name (`python` on Windows, `python3`
// elsewhere) -- the same contract the other suites honor.
const PYBIN = process.env.PYTHON || "python3";
const WIN = process.platform === "win32";

let passed = 0;
let failed = 0;
function report(ok, name, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
  if (ok) passed++; else failed++;
}
function skip(name, why) {
  console.log(`SKIP  ${name} (${why})`);
}
const fmt = (a) => JSON.stringify(a);
const short = (a) => (a.__crash__ ? `crash: ${fmt(a.stderr).slice(0, 200)}` : fmt(a));

// One fresh interpreter subprocess: request JSON on stdin, one JSON line out.
function ask(bin, script, req, timeoutMs) {
  const p = spawnSync(bin, [script], {
    input: JSON.stringify(req),
    encoding: "utf8",
    timeout: timeoutMs,
  });
  const lines = (p.stdout || "").split("\n").map((s) => s.trim()).filter(Boolean);
  if (lines.length !== 1) {
    return {
      __crash__: true,
      stderr: `expected exactly one JSON line, got ${lines.length}: stdout=${fmt(p.stdout).slice(0, 200)} stderr=${fmt(p.stderr).slice(0, 200)}`,
    };
  }
  try {
    return JSON.parse(lines[0]);
  } catch (e) {
    return { __crash__: true, stderr: `${e.message} :: ${lines[0].slice(0, 200)}` };
  }
}

// Build a request from a row (timeout honored by ask's caller).
function buildReq(row, cwd) {
  const req = {
    command: row.cmd,
    cwd,
    mode: "gate",
    timeout_ms: row.timeout_ms ?? 8000,
    output_limit: row.output_limit ?? 1_000_000,
  };
  if (row.env !== undefined) req.env = row.env;
  if (row.omitCmd) delete req.command;
  if (row.override) Object.assign(req, row.override);
  return req;
}

// Build a request from a row and run it through BOTH transports.
function runBoth(row, cwd) {
  const req = buildReq(row, cwd);
  const spawnTimeoutMs = Math.ceil((req.timeout_ms ?? 8000) / 1000 + 20) * 1000;
  return {
    py: ask(PYBIN, PY_SRC, req, spawnTimeoutMs),
    js: ask(process.execPath, JS_SRC, req, spawnTimeoutMs),
  };
}

// A seq row replays a command sequence; each transport walks the sequence in ITS OWN
// directory (same per-step commands) and only the FINAL answers are compared. One shared
// directory for both transports would cross-contaminate the file state -- py's step N
// would observe js's step N-1 writes (e.g. ">> appends" saw "more" twice).
function runRow(row, cwd) {
  if (!row.seq) return runBoth(row, cwd);
  const pyDir = join(cwd, "py");
  const jsDir = join(cwd, "js");
  mkdirSync(pyDir);
  mkdirSync(jsDir);
  // The runner already ran setup() in the parent; a seq row's commands run in the
  // per-transport dirs, so seed files (e.g. `cat < in.txt`) must exist there too.
  if (row.setup) {
    row.setup(pyDir);
    row.setup(jsDir);
  }
  let out = { py: undefined, js: undefined };
  for (const cmd of row.seq) {
    const spawnTimeoutMs = Math.ceil((row.timeout_ms ?? 8000) / 1000 + 20) * 1000;
    out = {
      py: ask(PYBIN, PY_SRC, buildReq({ ...row, cmd }, pyDir), spawnTimeoutMs),
      js: ask(process.execPath, JS_SRC, buildReq({ ...row, cmd }, jsDir), spawnTimeoutMs),
    };
    if (out.py.__crash__ || out.js.__crash__) break;
  }
  return out;
}

// ---- rows ---------------------------------------------------------------

const rows = [];
function refusal(name, cmd) {
  rows.push({ name: `refuse: ${name}`, cmd, wantRefused: true });
}

// 1. Refusal table -- every named construct.
refusal("||", "echo a || echo b");
refusal(";", "echo a; echo b");
refusal("; mid-word (redirection target becomes ';'-suffixed)", "true 2>/dev/null; cat x");
refusal("&", "echo a & echo b");
refusal("|&", "echo a |& cat");
refusal("&>", "echo a &> out.txt");
refusal("&>>", "echo a &>> out.txt");
refusal("<<", "cat << EOF");
refusal("<<<", "cat <<< text");
refusal("backticks", "echo `echo hi`");
refusal("$(", "echo $(echo hi)");
refusal("${VAR:-x}", "echo ${UNDEF:-x}");
refusal("~", "echo ~");
refusal("{ }", "echo {a,b}");
refusal("!", "! echo hi");
refusal("newline", "echo a\necho b");
// An EMPTY command string is not a refusal anywhere: both runtimes answer
// bad_request ("missing or invalid 'command'") with identical reason text,
// which checkRow still verifies for parity.
rows.push({ name: "empty-command is a bad_request on both", cmd: "", wantBadRequest: true });
refusal("unterminated-quote", 'echo "hi');
refusal(".sh target", "./deploy.sh");
refusal(".bat target", "run.bat");
refusal(".cmd target", "run.cmd");
refusal(".ps1 target", "run.ps1");
refusal("builtin-as-pipeline-stage", "echo hi | cd /");
refusal("$0", "echo $0");
refusal("$@", "echo $@");
refusal("$*", "echo $*");
refusal("$#", "echo $#");
refusal("$?", "echo $?");
refusal("$$", "echo $$");
refusal("$!", "echo $!");
refusal("$-", "echo $-");
refusal("$'…'", "echo $'x'");
refusal('$"…"', 'echo $"x"');
refusal("command-word-expanding-to-empty", "$UNDEF_VAR");

// 2. Quoting and escapes.
rows.push(
  { name: "quote: single quotes preserve spacing", cmd: "echo 'a b  c'", wantStdout: "a b  c\n" },
  { name: "quote: double quotes with escaped quote", cmd: 'echo "a\\"b"', wantStdout: 'a"b\n' },
  { name: "quote: backslash inside double quotes", cmd: 'echo "a\\\\b"', wantStdout: "a\\b\n" },
  { name: "quote: escaped dollar inside double quotes", cmd: 'echo "\\$HOME"', wantStdout: "$HOME\n" },
  { name: "quote: backslash-escape outside quotes", cmd: "echo a\\ b", wantStdout: "a b\n" },
  { name: "quote: escaped dollar outside quotes", cmd: "echo \\$HOME", wantStdout: "$HOME\n" },
  { name: "quote: $VAR inside double quotes expands", cmd: 'echo "$VAR"', env: { VAR: "hello" }, wantStdout: "hello\n" },
  { name: "quote: ${VAR} inside double quotes expands", cmd: 'echo "${VAR}"', env: { VAR: "hello" }, wantStdout: "hello\n" },
);

// 3. Var expansion: undefined->empty, no word-splitting/globbing of results,
// VAR=value prefix assignments (the PATH-bearing env rows exec ${PYBIN}).
const PYC = (code) => `${PYBIN} -c "${code}"`;
const withPath = (extra) => ({ PATH: process.env.PATH || "", ...extra });
const ENV_PRINTER = PYC("import os;print(os.environ.get('CMDRUN_X','<missing>'))");
rows.push(
  { name: "var: undefined var expands to empty", cmd: "echo [$UNDEF_PX]", wantStdout: "[]\n" },
  { name: "var: no word-splitting or globbing of the result", cmd: "echo $SPACED", env: { SPACED: "x y *" }, wantStdout: "x y *\n" },
  { name: "var: no word-splitting or globbing of the result (${})", cmd: "echo ${SPACED}", env: { SPACED: "z w" }, wantStdout: "z w\n" },
  { name: "var: prefix assignment reaches the child env", cmd: `VAR=hi ${PYBIN} -c "import os;print(os.environ.get('VAR',''))"`, env: withPath(), wantStdout: "hi\n", timeout_ms: 2000 },
  { name: "var: prefix assignment does not leak to the next command", cmd: `VAR=first ${PYBIN} -c "import os;print(os.environ.get('VAR',''))" && ${PYBIN} -c "import os;print(os.environ.get('VAR','second'))"`, env: withPath(), wantStdout: "first\nsecond\n", timeout_ms: 3000 },
);

// 4. Pipelines + pipefail. Builtins are refused as pipeline stages, so the
// pipefail rows use absolute-path binaries (POSIX); WIN skips them.
const F = "/usr/bin/false";
const T = "/usr/bin/true";
if (WIN) {
  skip("pipe: pipefail with real binaries", "needs /usr/bin/{true,false} (win32)");
} else {
  rows.push(
    { name: "pipe: 3-stage pipeline", cmd: "printf 'x\\ny\\n' | grep y | wc -l", wantStatus: 0, cmp: ["stdout"], timeout_ms: 2000 },
    { name: "pipe: pipefail - failing early stage fails the pipeline", cmd: `${F} | ${T}`, wantStatus: 1 },
    { name: "pipe: pipefail - last non-zero stage wins", cmd: `${T} | ${F} | ${T}`, wantStatus: 1 },
    { name: "pipe: pipefail - all-zero pipeline is zero", cmd: `${T} | ${T} | ${T}`, wantStatus: 0 },
  );
}

// 5. && chains (stop at first failure) and ( ) grouping.
rows.push(
  { name: "and: chain runs to the end on success", cmd: "true && true && echo ok", wantStdout: "ok\n" },
  { name: "and: stops at the first failure", cmd: "false && echo unreached", wantStatus: 1, wantStdout: "" },
  { name: "group: ( ) grouping runs its inner chain", cmd: "(true && echo grouped)", wantStdout: "grouped\n" },
);

// 6. Redirections: > then read, >> append, dup order (2>&1 >f vs >f 2>&1),
// < input, /dev/null. The stderr writer makes the dup direction unambiguous.
const STDERR_E = PYC("import sys;sys.stderr.write('E')");
rows.push(
  { name: "redir: > writes the file", seq: ["echo hi > out.txt", "cat out.txt"], wantStdout: "hi\n" },
  { name: "redir: >> appends", seq: ["echo hi > out.txt", "echo more >> out.txt", "cat out.txt"], wantStdout: "hi\nmore\n" },
  { name: "redir: < input redirection", setup: (d) => writeFileSync(join(d, "in.txt"), "seed\n"), seq: ["cat < in.txt"], wantStdout: "seed\n" },
  { name: "redir: 2>&1 >f - duped write lands in captured stdout", cmd: `${STDERR_E} 2>&1 > o1.txt`, wantStatus: 0, wantStdout: "E", timeout_ms: 2000 },
  { name: "redir: >f 2>&1 - the file receives stderr", seq: [`${STDERR_E} > o2.txt 2>&1`, "test -s o2.txt"], wantStatus: 0, timeout_ms: 2000 },
  { name: "redir: /dev/null swallows output", cmd: "echo hi > /dev/null", wantStatus: 0 },
);

// 7. Builtins: echo -n, test/[ on -e -f -d -s (true and false cases), cd.
const subFiles = (d) => {
  mkdirSync(join(d, "sub"));
  writeFileSync(join(d, "file.txt"), "x");
  writeFileSync(join(d, "empty.txt"), "");
};
rows.push(
  { name: "builtin: echo -n suppresses the newline", cmd: "echo -n abc", wantStdout: "abc" },
  { name: "builtin: test -e true", setup: subFiles, cmd: "test -e file.txt", wantStatus: 0 },
  { name: "builtin: test -e false on a missing path", cmd: "test -e missing-zz-px", wantStatus: 1 },
  { name: "builtin: [ -f ] on a regular file", setup: subFiles, cmd: "[ -f file.txt ]", wantStatus: 0 },
  { name: "builtin: [ -d ] on a directory", setup: subFiles, cmd: "[ -d sub ]", wantStatus: 0 },
  { name: "builtin: test -s true on a non-empty file", setup: subFiles, cmd: "test -s file.txt", wantStatus: 0 },
  { name: "builtin: test -s false on an empty file", setup: subFiles, cmd: "test -s empty.txt", wantStatus: 1 },
  { name: "builtin: cd persists across &&", setup: subFiles, cmd: "cd sub && pwd", wantStatus: 0, wantStdoutEnds: "sub\n" },
  { name: "builtin: cd into a missing directory fails with status 1", cmd: "cd no-such-dir-zz-px", wantStatus: 1, cmp: ["stderr"] },
  // F-capture: the test/[ usage rows once lost their stderr entirely in the JS twin --
  // runBuiltinCapture's early `return 2` skipped the BufferSink push that flushes
  // errChunks into the answer. These rows pin the messages.
  { name: "builtin: test usage error reaches stderr", cmd: "test x", wantStatus: 2, wantStderr: "test: usage: test -e|-f|-d|-s PATH\n" },
  { name: "builtin: [ missing bracket reaches stderr", cmd: "[ x", wantStatus: 2, wantStderr: "test: missing ']'\n" },
  // F-redirfail: a builtin redirection failure is status 1 + `cmdrun:` message on BOTH
  // twins (the JS gate path once answered internal_error, the py builtin path printed
  // the raw [Errno N] shape).
  { name: "builtin: redirection failure is status 1 with cmdrun: message", cmd: "echo hi > /dev/full", wantStatus: 1, wantStderrStarts: "cmdrun: " },
  // F-digit: a Unicode decimal (Arabic-Indic 2) before '>' is NOT an fd prefix on either
  // twin -- it folds into the word and the command is not found (127 on both). py once
  // refused it as "fd out of range" because str.isdigit() matches Unicode decimals.
  { name: "token: Unicode digit before > folds into the word", cmd: "٢1>o-fd.txt echo hi", wantStatus: 127, cmp: ["stderr"] },
);

// 8. Exit codes: 127 missing command, 126 non-executable file, 128+N signal.
// POSIX-shaped rows; WIN skips them (mirrors cmdrun_tests.py's own gating).
if (WIN) {
  skip("exit: 127 missing command", "needs a POSIX PATH shape (win32)");
  skip("exit: 126 non-executable file", "POSIX exec-bit semantics (win32)");
  skip("exit: 128+N signal death", "POSIX signal semantics (win32)");
} else {
  rows.push(
    { name: "exit: 127 unknown command", cmd: "bogus-command-zz-px-123", wantStatus: 127, cmp: ["stderr"] },
    { name: "exit: 126 non-executable existing file", setup: (d) => { writeFileSync(join(d, "noexec.txt"), "x"); chmodSync(join(d, "noexec.txt"), 0o000); }, cmd: "./noexec.txt", wantStatus: 126 },
    { name: "exit: 137 = 128+9 on SIGKILL", cmd: PYC("import os,signal;os.kill(os.getpid(),signal.SIGKILL)"), wantStatus: 137, timeout_ms: 2000 },
  );
}

// 9. output_limit truncation: big stdout, small cap -> truncated, status 0.
rows.push(
  { name: "limit: oversized stdout is truncated with status 0", cmd: PYC("print('x'*200000)"), output_limit: 1000, wantStatus: 0, wantTruncated: true, timeout_ms: 2000 },
  // F-large: a captured stdout LARGER than one pipe chunk (16KB observed) once broke
  // the JS supervisor twice -- its answer reader took only the first 'data' chunk, and
  // emitAndDie's write loop swallowed EAGAIN and dropped the answer tail. Both fixed;
  // these rows hold the fixes (the old limit row's 1000-byte cap never left one chunk).
  { name: "limit: 20KB answer crosses one pipe chunk", cmd: PYC("print('x'*20000)"), output_limit: 4_000_000, wantStatus: 0, wantStdoutEnds: "x\n", timeout_ms: 8000 },
  { name: "limit: 300KB answer spans many chunks", cmd: PYC("import sys; sys.stdout.write('x'*300000 + chr(10))"), output_limit: 4_000_000, wantStatus: 0, wantStdoutEnds: "x\n", timeout_ms: 8000 },
  // F-eagain: a request larger than one pipe chunk (env value bloats it past 16KB)
  // used to be truncated by the JS transports' EAGAIN-as-EOF stdin reads.
  { name: "limit: 200KB request (bloated env) survives", cmd: "echo $OK", env: withPath({ OK: "x".repeat(200_000), BIGPAD: "y".repeat(100_000) }), wantStdoutEnds: "x\n", timeout_ms: 8000 },
);

// 10. bad_request: type/range validation, both transports must refuse with
// the SAME reason text. Nothing executes, so no known-divergence expected.
rows.push(
  { name: "badreq: timeout_ms=true (a bool)", cmd: "echo hi", override: { timeout_ms: true }, wantBadRequest: true },
  { name: "badreq: timeout_ms=2.5 (non-integral)", cmd: "echo hi", override: { timeout_ms: 2.5 }, wantBadRequest: true },
  { name: "badreq: timeout_ms out of range", cmd: "echo hi", override: { timeout_ms: 86_400_001 }, wantBadRequest: true },
  { name: "badreq: output_limit negative", cmd: "echo hi", override: { output_limit: -1 }, wantBadRequest: true },
  { name: "badreq: output_limit above the 4 MiB cap", cmd: "echo hi", override: { output_limit: 67_108_864 }, wantBadRequest: true },
  { name: "badreq: mode invalid", cmd: "echo hi", override: { mode: "bogus" }, wantBadRequest: true },
  { name: "badreq: missing command", cmd: "echo hi", omitCmd: true, wantBadRequest: true },
  { name: "badreq: env not an object", cmd: "echo hi", env: "not-an-object", wantBadRequest: true },
);

// 11. CMDRUN_* scrub: the reserved namespace never reaches a child via the
// request env; the sanctioned path is a prefix assignment.
rows.push(
  { name: "scrub: non-reserved request env is visible to expansion", cmd: "echo $OK", env: { CMDRUN_X: "1", OK: "1" }, wantStdout: "1\n" },
  { name: "scrub: CMDRUN_* is scrubbed from the child env", cmd: ENV_PRINTER, env: withPath({ CMDRUN_X: "1" }), wantStdout: "<missing>\n", timeout_ms: 2000 },
  { name: "scrub: prefix assignment passes CMDRUN_* to the child", cmd: `CMDRUN_X=2 ${ENV_PRINTER}`, env: withPath(), wantStdout: "2\n", timeout_ms: 2000 },
);

// 12. Glob: sorted match, bracket classes (incl. [a-] and [!...]), literal
// fallback, quoted-no-expand, and the POSIX no-dotfile rule. echo is a
// builtin, so these rows spawn nothing.
const globSetup = (d) => {
  for (const n of ["a.txt", "b.txt", "c.log", ".hidden"]) writeFileSync(join(d, n), "");
};
rows.push(
  { name: "glob: * sorted match", setup: globSetup, cmd: "echo *.txt", wantStdout: "a.txt b.txt\n" },
  { name: "glob: no-match falls back to the literal word", cmd: "echo no_such*.zzz", wantStdout: "no_such*.zzz\n" },
  { name: "glob: a quoted glob is never expanded", cmd: "echo '*.txt'", wantStdout: "*.txt\n" },
  { name: "glob: bracket class [ab]", setup: globSetup, cmd: "echo [ab].txt", wantStdout: "a.txt b.txt\n" },
  // NB: an unquoted `!` is refused by BOTH runtimes (identical reason "!"),
  // even inside a bracket class -- `[!a]` is therefore NOT expandable in this
  // grammar; the parity row asserts the two runtimes agree on the refusal.
  { name: "glob: unquoted ! inside brackets is refused identically", setup: globSetup, cmd: "echo [!a].txt", wantRefused: true },
  { name: "glob: trailing-dash bracket [a-]", setup: globSetup, cmd: "echo [a-].txt", wantStdout: "a.txt\n" },
  { name: "glob: * does not match dotfiles", setup: globSetup, cmd: "echo *", wantStdout: "a.txt b.txt c.log\n", winSkip: true },
);

// 13. Short timeout: timed_out on both, and the two runtimes must AGREE on
// status (equal, and one of the spec-allowed values).
rows.push(
  { name: "timeout: short deadline kills the sleep", cmd: PYC("import time;time.sleep(2)"), timeout_ms: 300, statusIn: [null, 124], wantTimedOut: true },
);

// ---- runner -------------------------------------------------------------

let skipped = 0;
function fail(row, detail) {
  report(false, row.name, detail);
}

function checkRow(row, py, js) {
  if (py.__crash__ || js.__crash__) {
    fail(row, `transport crash: py=${short(py)} js=${short(js)}`);
    return;
  }
  if (row.wantRefused || row.wantBadRequest) {
    const key = row.wantRefused ? "refused" : "bad_request";
    if (py[key] !== true || js[key] !== true) {
      fail(row, `expected ${key} on BOTH: py=${short(py)} js=${short(js)}`);
      return;
    }
    if (py.reason !== js.reason) {
      fail(row, `reason diverges: py=${fmt(py.reason)} js=${fmt(js.reason)}`);
      return;
    }
    report(true, row.name);
    return;
  }
  for (const f of ["status", "timed_out", "truncated"]) {
    if (row.statusIn && f === "status") continue;
    if (py[f] !== js[f]) {
      fail(row, `${f} diverges: py=${fmt(py[f])} js=${fmt(js[f])}`);
      return;
    }
  }
  if (row.statusIn) {
    if (py.status !== js.status) {
      fail(row, `status diverges: py=${fmt(py.status)} js=${fmt(js.status)}`);
      return;
    }
    if (!row.statusIn.includes(py.status)) {
      fail(row, `agreed status ${fmt(py.status)} not in [${row.statusIn}]`);
      return;
    }
  }
  if (row.wantStatus !== undefined && py.status !== row.wantStatus) {
    fail(row, `status=${fmt(py.status)} expected ${fmt(row.wantStatus)}`);
    return;
  }
  if (row.wantTimedOut !== undefined && py.timed_out !== row.wantTimedOut) {
    fail(row, `timed_out=${fmt(py.timed_out)} expected ${fmt(row.wantTimedOut)}`);
    return;
  }
  if (row.wantTruncated !== undefined && py.truncated !== row.wantTruncated) {
    fail(row, `truncated=${fmt(py.truncated)} expected ${fmt(row.wantTruncated)}`);
    return;
  }
  for (const f of row.cmp ?? []) {
    if (py[f] !== js[f]) {
      fail(row, `${f} diverges: py=${fmt(py[f])} js=${fmt(js[f])}`);
      return;
    }
  }
  if (row.wantStdout !== undefined && py.stdout !== row.wantStdout) {
    fail(row, `stdout=${fmt(py.stdout)} expected ${fmt(row.wantStdout)}`);
    return;
  }
  if (row.wantStdoutEnds !== undefined && !(py.stdout || "").endsWith(row.wantStdoutEnds)) {
    fail(row, `stdout=${fmt(py.stdout)} expected to end with ${fmt(row.wantStdoutEnds)}`);
    return;
  }
  if (row.wantStderr !== undefined && py.stderr !== row.wantStderr) {
    fail(row, `stderr=${fmt(py.stderr)} expected ${fmt(row.wantStderr)}`);
    return;
  }
  if (row.wantStderrStarts !== undefined && !(py.stderr || "").startsWith(row.wantStderrStarts)) {
    fail(row, `stderr=${fmt(py.stderr)} expected to start with ${fmt(row.wantStderrStarts)}`);
    return;
  }
  report(true, row.name);
}

for (const row of rows) {
  if (WIN && row.winSkip) {
    skip(row.name, "win32-only row");
    skipped++;
    continue;
  }
  const d = mkdtempSync(join(tmpdir(), "cmdrun-parity-"));
  try {
    if (row.setup) row.setup(d);
    const { py, js } = runRow(row, d);
    checkRow(row, py, js);
  } finally {
    rmSync(d, { recursive: true, force: true });
  }
}

console.log(`\n${passed} PASS / ${failed} FAIL / ${skipped} SKIP`);
console.log(`python oracle: ${PYBIN} ${PY_SRC}`);
console.log(`js twin:       node ${JS_SRC}`);
process.exitCode = failed > 0 ? 1 : 0;
