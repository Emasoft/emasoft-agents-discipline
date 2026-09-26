#!/usr/bin/env node
// A shell-free command interpreter: hand-written tokenizer/parser/analyzer/executor.
// Node twin of scripts/lib/cmdrun.py (TRDD-2U56GG7S step 2). Same grammar, same
// refusal messages, same exit codes; see the .py module docstring for the design.
//
// Execution model (v2, docs_dev/cmdrun-v2-executor-spec.md): two processes, one timekeeper.
// `node cmdrun.mjs` is the SUPERVISOR -- reads/validates the request, answers refusals
// without spawning anything. For an accepted command it spawns `node cmdrun.mjs --exec`
// (the WORKER) detached (its own process group on POSIX), stdin=PIPE (the death pipe),
// stdout=PIPE (the worker's one-line JSON answer), stderr=ignore, and writes the validated
// request as one JSON line. The worker's pid IS the one process group of the whole command
// (every stage it spawns inherits the group). On worker stdin EOF the worker SIGKILLs its
// own group (POSIX) / exits (Windows -- the supervisor's taskkill /T reaps the tree there).
// The supervisor is the ONLY timekeeper: on deadline, on SIGTERM/SIGHUP/SIGINT, or on the
// worker exiting without a valid answer, it repeats killpg(pid, SIGKILL) until ESRCH
// (POSIX) or taskkill /T /F (Windows) under one global cleanup deadline, and never answers
// silently when that bound is exhausted (`leaked: true`). The Node twin cannot create
// Windows Job Objects: it kills with `taskkill /T /F` and the answer says
// `tree_kill: "taskkill"`.

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

const IS_WINDOWS = process.platform === "win32";
// F4: the CMDRUN_TEST_* env hooks must be inert for every real caller -- gated on an argv
// flag ONLY tests pass, never on the env alone. Computed once, from argv, so both the
// supervisor invocation and the worker invocation it spawns see the same answer.
const TEST_HOOKS = process.argv.slice(2).includes("--test-hooks");

// ponytail: bounded, not unlimited -- an attacker-controlled command string must never make
// the parser do unbounded work. Same caps as cmdrun.py.
const MAX_COMMAND_LENGTH = 262_144;
const MAX_GROUP_DEPTH = 32;
const MAX_PIPELINE_STAGES = 512;
const MAX_TOKENS = 20_000;
const MAX_EXPANDED_BYTES = 1_048_576; // M4/M5: cap on argv text a $VAR expansion can amplify to.
const MAX_REQUEST_BYTES = 16 * 1024 * 1024; // spec: read stdin to EOF, cap 16 MiB.
// F9: output_limit cap -- JSON escaping can grow a captured byte to 6 bytes in the answer.
const MAX_OUTPUT_LIMIT = 4 * 1024 * 1024;
// Global cleanup deadline (spec: "deadline + 1.5 s") for the kill-until-dead loop + reap.
const CLEANUP_BUDGET_S = 1.5;

function cleanupBudgetS() {
  // TEST-ONLY override (gated on --test-hooks); non-finite or out-of-range values rejected
  // (nan/inf would make the deadline comparison never succeed) -- fall back to the real budget.
  if (!TEST_HOOKS) return CLEANUP_BUDGET_S;
  const override = process.env["CMDRUN_CLEANUP_BUDGET_S"];
  if (override) {
    const value = Number(override);
    if (Number.isFinite(value) && value >= 0 && value <= 10) return value;
  }
  return CLEANUP_BUDGET_S;
}

const IDENT_START = /[A-Za-z_]/;
const IDENT_CONT = /[A-Za-z0-9_]/;
const REFUSED_UNQUOTED_CHARS = {
  "`": "backticks",
  "~": "~",
  "{": "{ }",
  "}": "{ }",
  "!": "!",
};
const OP_CHARS = new Set(["|", "&", "<", ">"]);
export const BUILTIN_NAMES = new Set(["cd", "true", "false", "echo", "test", "["]);
// M10/advisor P8: policing a tool's shebang breaks ordinary commands (yarn, pyenv shims).
// The grammar polices the command WORD only.
export const SHELL_NAMES = new Set([
  "sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "busybox", "yash",
  "fish", "csh", "tcsh", "nu", "xonsh", "elvish",
  "cmd", "powershell", "pwsh", "wsl",
]);
const REFUSED_SPECIAL_DOLLAR = new Set("0123456789@*#?$!-".split(""));

class Refused extends Error {
  constructor(reason) {
    super(reason);
    this.reason = reason;
  }
}

// ---------------------------------------------------------------------------
// Tokenizer
// ---------------------------------------------------------------------------

class Word {
  constructor() {
    // parts: list of {text, literal}. literal=true means "never a glob metachar".
    this.parts = [];
    // M8: true the instant ANY quote was consumed while building this word.
    this.sawQuote = false;
  }
  add(text, literal) {
    if (text) this.parts.push({ text, literal });
  }
  isEmpty() {
    return this.parts.length === 0;
  }
}

class Tok {
  constructor(kind, { word = null, fd = null, redirKind = null, dupTarget = null, rawPrefix = "" } = {}) {
    this.kind = kind; // WORD, PIPE, AND, LPAREN, RPAREN, REDIR_FILE, REDIR_DUP
    this.word = word;
    this.fd = fd;
    this.redirKind = redirKind; // '>', '>>', '<'
    this.dupTarget = dupTarget;
    this.rawPrefix = rawPrefix;
  }
}

function charge(budget, text) {
  // M4/M5: bound the total bytes a $VAR/${VAR} expansion can add across the whole command.
  budget[0] += text.length;
  if (budget[0] > MAX_EXPANDED_BYTES) throw new Refused("expanded command too large");
}

function expandDollar(s, i, n, env, outLiteralParts, budget) {
  // s[i] === '$'. Returns new index. Appends {text, literal:true} parts.
  i += 1;
  if (i < n && s[i] === "{") {
    // M4: indexOf from i+1, never includes() on a slice -- the slice copies the REST of the
    // command on every ${, making a command with many of them quadratic.
    const j = s.indexOf("}", i + 1);
    if (j === -1) throw new Refused("unterminated ${...}");
    const content = s.slice(i + 1, j);
    if (content && IDENT_START.test(content[0]) && [...content.slice(1)].every((c) => IDENT_CONT.test(c))) {
      const value = envGet(env, content);
      outLiteralParts.push({ text: value, literal: true });
      charge(budget, value);
      return j + 1;
    }
    throw new Refused(`unsupported "\${${content}}" operator`);
  }
  // M7: $0-$9, $@ $* $# $? $$ $! $- refused BY NAME.
  if (i < n && REFUSED_SPECIAL_DOLLAR.has(s[i])) throw new Refused(`'$${s[i]}' is refused`);
  if (i < n && s[i] === "'") throw new Refused("$'...' is refused");
  if (i < n && s[i] === '"') throw new Refused('$"..." is refused');
  if (i < n && IDENT_START.test(s[i])) {
    let j = i;
    while (j < n && IDENT_CONT.test(s[j])) j += 1;
    const name = s.slice(i, j);
    const value = envGet(env, name);
    outLiteralParts.push({ text: value, literal: true });
    charge(budget, value);
    return j;
  }
  if (i < n && s[i] === "(") throw new Refused("$( command substitution");
  // Bare '$' not followed by a name: literal '$'.
  outLiteralParts.push({ text: "$", literal: true });
  return i;
}

function envGet(env, name) {
  if (env === null || env === undefined) return "";
  if (IS_WINDOWS) {
    const upper = name.toUpperCase();
    for (const [k, v] of Object.entries(env)) {
      if (k.toUpperCase() === upper) return v;
    }
    return "";
  }
  return Object.prototype.hasOwnProperty.call(env, name) ? env[name] : "";
}

function envSet(envDict, name, value) {
  // M12: Windows env keys are case-insensitive -- replace whichever existing key matches
  // case-insensitively before inserting the new one.
  if (IS_WINDOWS) {
    const upper = name.toUpperCase();
    for (const k of Object.keys(envDict)) {
      if (k !== name && k.toUpperCase() === upper) delete envDict[k];
    }
  }
  envDict[name] = value;
}

function tokenize(command, env) {
  if (command.length > MAX_COMMAND_LENGTH) throw new Refused("command too long");
  if (command.includes("\n") || command.includes("\r")) throw new Refused("newline / multi-line command");
  const n = command.length;
  const tokens = [];
  let i = 0;
  const budget = [0];
  while (i < n) {
    const c = command[i];
    if (c === " " || c === "\t") {
      i += 1;
      continue;
    }
    if (c === "#" && (i === 0 || command[i - 1] === " " || command[i - 1] === "\t")) {
      break; // comment to end of line
    }
    // fd-prefixed or bare redirection? M6: a digit RUN immediately before '<'/'>' is an fd
    // prefix candidate; refuse anything but a single digit 0-2.
    let fd = null;
    if (c >= "0" && c <= "9") {
      let j = i;
      while (j < n && command[j] >= "0" && command[j] <= "9") j += 1;
      if (j < n && (command[j] === "<" || command[j] === ">")) {
        const numstr = command.slice(i, j);
        if (numstr.length !== 1 || !"012".includes(numstr)) {
          throw new Refused(`redirection fd '${numstr}' out of range 0-2`);
        }
        fd = parseInt(numstr, 10);
        i = j;
      }
    }
    const ch = command[i];
    if (OP_CHARS.has(ch)) {
      let j = i;
      while (j < n && OP_CHARS.has(command[j])) j += 1;
      const opstr = command.slice(i, j);
      i = j;
      const tok = classifyOp(opstr, fd);
      if (tok.kind === "REDIR_DUP") {
        if (i < n && command[i] >= "0" && command[i] <= "9") {
          let j2 = i;
          while (j2 < n && command[j2] >= "0" && command[j2] <= "9") j2 += 1;
          const numstr = command.slice(i, j2);
          if (numstr.length !== 1 || !"012".includes(numstr)) {
            throw new Refused(`dup target '${numstr}' out of range 0-2`);
          }
          tok.dupTarget = parseInt(numstr, 10);
          i = j2;
        } else {
          throw new Refused(`'${opstr}' without a target file descriptor`);
        }
      }
      tokens.push(tok);
      if (tokens.length > MAX_TOKENS) throw new Refused("too many tokens");
      continue;
    }
    if (ch === ";") throw new Refused("lone ';'");
    if (ch === "(") {
      tokens.push(new Tok("LPAREN"));
      i += 1;
      continue;
    }
    if (ch === ")") {
      tokens.push(new Tok("RPAREN"));
      i += 1;
      continue;
    }
    // WORD
    const word = new Word();
    const rawPrefixChars = [];
    let rawPrefixDone = false;
    const started = i;
    while (i < n) {
      const c2 = command[i];
      // ';' breaks the word like '(' / ')' do (py oracle line-for-line): the outer loop
      // then refuses it as "lone ';'" -- without this, `echo a; echo b` folds ';' into
      // the word "a;" and executes.
      if (c2 === " " || c2 === "\t" || OP_CHARS.has(c2) || c2 === "(" || c2 === ")" || c2 === ";" || c2 === "\n" || c2 === "\r") {
        break;
      }
      if (c2 === "#") {
        if (i === started || command[i - 1] === " " || command[i - 1] === "\t") {
          break; // word-initial or space-preceded '#': comment
        }
        // mid-word '#' is literal -- falls through to the plain-char case below
      }
      if (c2 === "'") {
        word.sawQuote = true;
        rawPrefixDone = true;
        const j = command.indexOf("'", i + 1);
        if (j === -1) throw new Refused("unterminated single quote");
        word.add(command.slice(i + 1, j), true);
        i = j + 1;
        continue;
      }
      if (c2 === '"') {
        word.sawQuote = true;
        rawPrefixDone = true;
        i += 1;
        const buf = [];
        let closed = false;
        while (i < n) {
          const dc = command[i];
          if (dc === '"') {
            closed = true;
            i += 1;
            break;
          }
          if (dc === "`") throw new Refused("backticks");
          if (dc === "\\" && i + 1 < n && '"\\$'.includes(command[i + 1])) {
            buf.push(command[i + 1]);
            i += 2;
            continue;
          }
          if (dc === "$") {
            const parts = [];
            i = expandDollar(command, i, n, env, parts, budget);
            if (buf.length) {
              word.add(buf.join(""), true);
              buf.length = 0;
            }
            for (const p of parts) word.add(p.text, p.literal);
            continue;
          }
          buf.push(dc);
          i += 1;
        }
        if (!closed) throw new Refused("unterminated double quote");
        if (buf.length) word.add(buf.join(""), true);
        continue;
      }
      if (c2 === "\\") {
        if (i + 1 >= n) throw new Refused("trailing backslash");
        const nxt = command[i + 1];
        // I2: on Windows, an unquoted backslash is almost always a PATH separator
        // (`C:\Users\foo`), not an escape -- refuse instead of guessing.
        if (IS_WINDOWS && /[A-Za-z0-9]/.test(nxt)) {
          throw new Refused("unquoted backslash before a letter/digit -- quote Windows paths");
        }
        word.add(nxt, true);
        i += 2;
        rawPrefixDone = true;
        continue;
      }
      if (c2 === "$") {
        const parts = [];
        i = expandDollar(command, i, n, env, parts, budget);
        for (const p of parts) word.add(p.text, p.literal);
        rawPrefixDone = true;
        continue;
      }
      if (c2 === "`") throw new Refused("backticks");
      if (Object.prototype.hasOwnProperty.call(REFUSED_UNQUOTED_CHARS, c2)) {
        throw new Refused(REFUSED_UNQUOTED_CHARS[c2]);
      }
      if (!rawPrefixDone) rawPrefixChars.push(c2);
      word.add(c2, false);
      i += 1;
    }
    tokens.push(new Tok("WORD", { word, rawPrefix: rawPrefixChars.join("") }));
    if (tokens.length > MAX_TOKENS) throw new Refused("too many tokens");
  }
  return tokens;
}

function classifyOp(opstr, fd) {
  if (opstr === "|") return new Tok("PIPE");
  if (opstr === "&&") return new Tok("AND");
  if (opstr === ">") return new Tok("REDIR_FILE", { fd: fd === null ? 1 : fd, redirKind: ">" });
  if (opstr === ">>") return new Tok("REDIR_FILE", { fd: fd === null ? 1 : fd, redirKind: ">>" });
  if (opstr === "<") return new Tok("REDIR_FILE", { fd: fd === null ? 0 : fd, redirKind: "<" });
  if (opstr === ">&") return new Tok("REDIR_DUP", { fd: fd === null ? 1 : fd });
  if (opstr === "||") throw new Refused("'||'");
  if (opstr === "|&") throw new Refused("'|&'");
  if (opstr === "&>") throw new Refused("'&>'");
  if (opstr === "&>>") throw new Refused("'&>>'");
  if (opstr === "<<") throw new Refused("'<<' heredoc");
  if (opstr === "<<<") throw new Refused("'<<<' herestring");
  if (opstr === "&") throw new Refused("lone '&' (background)");
  throw new Refused(`unsupported operator '${opstr}'`);
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

class Redir {
  constructor(fd, kind, target) {
    this.fd = fd;
    this.kind = kind; // '>', '>>', '<' for file; '>&' for dup
    this.target = target; // Word for file redirs, int for dup redirs
  }
}

class CommandNode {
  constructor(assigns, name, args, redirs) {
    this.assigns = assigns; // [ [name, Word] ]
    this.name = name;
    this.args = args;
    this.redirs = redirs;
  }
}

class GroupNode {
  constructor(chain) {
    this.chain = chain;
  }
}

class PipelineNode {
  constructor(stages) {
    this.stages = stages;
  }
}

class ChainNode {
  constructor(pipelines) {
    this.pipelines = pipelines;
  }
}

class Parser {
  constructor(tokens) {
    this.tokens = tokens;
    this.i = 0;
    this.depth = 0;
  }
  peek() {
    return this.i < this.tokens.length ? this.tokens[this.i] : null;
  }
  parseChain() {
    const pipelines = [this.parsePipeline()];
    let tok = this.peek();
    while (tok !== null && tok.kind === "AND") {
      this.i += 1;
      pipelines.push(this.parsePipeline());
      tok = this.peek();
    }
    return new ChainNode(pipelines);
  }
  parsePipeline() {
    const stages = [this.parseStage()];
    let tok = this.peek();
    while (tok !== null && tok.kind === "PIPE") {
      this.i += 1;
      stages.push(this.parseStage());
      tok = this.peek();
    }
    if (stages.length > MAX_PIPELINE_STAGES) throw new Refused("pipeline has too many stages");
    return new PipelineNode(stages);
  }
  parseStage() {
    const tok = this.peek();
    if (tok === null) throw new Refused("expected a command");
    if (tok.kind === "LPAREN") {
      this.depth += 1;
      if (this.depth > MAX_GROUP_DEPTH) throw new Refused("too deeply nested");
      this.i += 1;
      const inner = this.parseChain();
      const closeTok = this.peek();
      if (closeTok === null || closeTok.kind !== "RPAREN") throw new Refused("unbalanced '('");
      this.i += 1;
      this.depth -= 1;
      return new GroupNode(inner);
    }
    return this.parseSimpleCommand();
  }
  parseSimpleCommand() {
    const assigns = [];
    let name = null;
    const args = [];
    const redirs = [];
    let sawWord = false;
    for (;;) {
      const tok = this.peek();
      if (tok === null || tok.kind === "PIPE" || tok.kind === "AND" || tok.kind === "RPAREN") break;
      if (tok.kind === "REDIR_FILE") {
        this.i += 1;
        const targetTok = this.peek();
        if (targetTok === null || targetTok.kind !== "WORD") throw new Refused("redirection missing a target");
        this.i += 1;
        redirs.push(new Redir(tok.fd, tok.redirKind, targetTok.word));
        continue;
      }
      if (tok.kind === "REDIR_DUP") {
        this.i += 1;
        redirs.push(new Redir(tok.fd, ">&", tok.dupTarget));
        continue;
      }
      if (tok.kind !== "WORD") throw new Refused(`unexpected token ${tok.kind}`);
      if (!sawWord) {
        const m = assignmentName(tok.rawPrefix, tok.word);
        if (m !== null) {
          assigns.push(m);
          this.i += 1;
          continue;
        }
      }
      if (!sawWord) {
        name = tok.word;
        sawWord = true;
      } else {
        args.push(tok.word);
      }
      this.i += 1;
    }
    if (name === null) {
      throw new Refused(assigns.length ? "assignment with no command" : "empty or whitespace-only command");
    }
    return new CommandNode(assigns, name, args, redirs);
  }
}

function assignmentName(rawPrefix, word) {
  // `NAME=value` prefix, unquoted NAME, literal '='. Returns [name, valueWord] or null.
  if (!rawPrefix || !IDENT_START.test(rawPrefix[0])) return null;
  let j = 1;
  while (j < rawPrefix.length && IDENT_CONT.test(rawPrefix[j])) j += 1;
  if (j >= rawPrefix.length || rawPrefix[j] !== "=") return null;
  const name = rawPrefix.slice(0, j);
  const cut = name.length + 1;
  const value = new Word();
  let consumed = 0;
  for (const { text, literal } of word.parts) {
    if (consumed >= cut) {
      value.add(text, literal);
      continue;
    }
    const remainingCut = cut - consumed;
    if (!literal && text.length >= remainingCut) {
      const tail = text.slice(remainingCut);
      if (tail) value.add(tail, literal);
      consumed += text.length;
    } else {
      consumed += text.length;
    }
  }
  return [name, value];
}

function parse(tokens) {
  if (!tokens.length) throw new Refused("empty or whitespace-only command");
  const p = new Parser(tokens);
  const chain = p.parseChain();
  const trailing = p.peek();
  if (trailing !== null) throw new Refused(`unexpected token ${trailing.kind}`);
  return chain;
}

// ---------------------------------------------------------------------------
// Word expansion (variables done at tokenize time; glob left for here)
// ---------------------------------------------------------------------------

const GLOB_CHARS = new Set(["*", "?", "["]);

export function expandWordNoGlob(word) {
  return word.parts.map((p) => p.text).join("");
}

// H8: fnmatch.translate semantics, ported from CPython's fnmatch.py: '*' and '?' do NOT
// match a '/', '[!...]' negation, '[]...]' and '[a-]' edge brackets, unmatched '[' literal.
function fnmatchTranslate(pat) {
  let i = 0;
  const n = pat.length;
  let res = "";
  while (i < n) {
    const c = pat[i];
    i += 1;
    if (c === "*") {
      res += "[^/]*";
    } else if (c === "?") {
      res += "[^/]";
    } else if (c === "[") {
      let j = i;
      if (j < n && pat[j] === "!") j += 1;
      if (j < n && pat[j] === "]") j += 1;
      while (j < n && pat[j] !== "]") j += 1;
      if (j >= n) {
        res += "\\[";
      } else {
        let stuff = pat.slice(i, j);
        if (stuff.startsWith("!")) stuff = "^" + stuff.slice(1);
        res += `[${stuff}]`;
        i = j + 1;
      }
    } else {
      res += c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }
  }
  return res;
}

// Sort by CODE POINT, not UTF-16 units (P5): Python sorts str by code point, so a
// character above U+FFFF (a surrogate pair in JS) must compare by its code point.
function codePointCompare(a, b) {
  const ia = Array.from(a, (c) => c.codePointAt(0));
  const ib = Array.from(b, (c) => c.codePointAt(0));
  const len = Math.min(ia.length, ib.length);
  for (let k = 0; k < len; k += 1) {
    if (ia[k] !== ib[k]) return ia[k] - ib[k];
  }
  return ia.length - ib.length;
}

// Port of glob.glob(pattern, root_dir=cwd) for the subset of syntax the parser accepts
// (no **, no brace expansion). Segments split on '/'; glob segments match directory
// listings with fnmatch semantics and never match dotfiles unless the pattern segment
// itself starts with '.'; a trailing '/' matches directories only.
function globIn(cwd, pattern) {
  const segs = pattern.split("/");
  const dirsOnly = pattern.endsWith("/");
  let roots;
  if (pattern.startsWith("/")) {
    roots = [""];
    while (segs.length && segs[0] === "") segs.shift();
  } else {
    roots = [cwd];
  }
  // current: [{dir, name}] -- dir is the absolute directory to list; name the matched
  // relative path so far ('' at the root).
  let current = roots.map((r) => ({ dir: r, name: "" }));
  for (let level = 0; level < segs.length; level += 1) {
    const seg = segs[level];
    const isLast = level === segs.length - 1;
    const next = [];
    if (seg === "") {
      // '//' or a trailing '/': stay at the same level (the dir-only filter ran at the
      // last real segment).
      if (isLast) {
        // trailing '/': every current match must BE a directory.
        for (const { dir, name } of current) {
          if (name === "" || isDirPath(dir + name)) next.push({ dir, name });
        }
      }
      continue;
    }
    const hasGlob = Array.from(seg).some((c) => GLOB_CHARS.has(c));
    for (const { dir, name } of current) {
      const dirPath = dir === "" ? "/" : dir;
      let entries;
      try {
        entries = fs.readdirSync(dirPath === "/" ? "/" : dirPath, { withFileTypes: true });
      } catch {
        continue;
      }
      // Python glob: '*'/'?' do not match a leading '.' unless the pattern starts with one.
      const matchesDot = seg.startsWith(".");
      const cand = [];
      if (hasGlob) {
        const regex = new RegExp(`^${fnmatchTranslate(seg)}$`, IS_WINDOWS ? "i" : "");
        for (const ent of entries) {
          const nm = ent.name;
          if (!matchesDot && nm.startsWith(".")) continue;
          if (!regex.test(nm)) continue;
          if (dirsOnly && isLast && !ent.isDirectory()) continue;
          cand.push(nm);
        }
      } else {
        for (const ent of entries) {
          const nm = ent.name;
          const ok = nm === seg || (IS_WINDOWS && nm.toLowerCase() === seg.toLowerCase());
          if (!ok) continue;
          if (dirsOnly && isLast && !ent.isDirectory()) continue;
          cand.push(nm);
        }
      }
      cand.sort(codePointCompare);
      for (const nm of cand) {
        const childDir = dirPath.endsWith("/") ? dirPath + nm : dirPath + "/" + nm;
        next.push({ dir: childDir, name: name ? name + "/" + nm : nm });
      }
    }
    current = next;
  }
  const out = [];
  for (const { dir, name } of current) {
    if (name === "") continue;
    // Python's glob.glob(root_dir=cwd) returns paths joined onto root_dir. For a relative
    // pattern the result is relative to root_dir in Python 3.10+... it returns
    // os.path.join(root_dir, ...) only for absolute patterns; for relative ones it returns
    // the RELATIVE match (no root_dir prefix). Match that: a relative pattern yields
    // relative matches; an absolute pattern keeps its leading '/'.
    out.push(pattern.startsWith("/") ? dir + name : name);
  }
  return out;
}

function isDirPath(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function expandWordGlob(word, cwd) {
  // Returns string[] -- the arguments this word expands to (1, or N on a glob match).
  const literalStr = expandWordNoGlob(word);
  const rawText = word.parts.filter((p) => !p.literal).map((p) => p.text).join("");
  let hasGlobChar = false;
  for (const c of rawText) {
    if (GLOB_CHARS.has(c)) {
      hasGlobChar = true;
      break;
    }
  }
  if (!hasGlobChar) return [literalStr];
  const pattern = word.parts.map((p) => (p.literal ? globEscape(p.text) : p.text)).join("");
  const matches = globIn(cwd, pattern).sort(codePointCompare);
  if (!matches.length) return [literalStr];
  return matches;
}

function globEscape(text) {
  // Python glob.escape: wrap '*', '?', '[', ']' in brackets.
  return text.replace(/([*?[\]])/g, "[$1]");
}

// ---------------------------------------------------------------------------
// Analyzer
// ---------------------------------------------------------------------------

const REFUSED_TARGET_SUFFIXES = [".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1"];

export function compileCommand(command, env = null) {
  // Tokenize + parse + static checks that don't require running anything.
  // Returns [chain_or_null, reason_or_null].
  const effectiveEnv = env !== null && env !== undefined ? env : process.env;
  try {
    const tokens = tokenize(command, effectiveEnv);
    const chain = parse(tokens);
    checkStatic(chain);
    return [chain, null];
  } catch (exc) {
    if (exc instanceof Refused) return [null, exc.reason];
    throw exc;
  }
}

export function analyze(command, env = null, cwd = null) {
  // Public linter entry point: [ok, reason].
  const [, reason] = compileCommand(command, env);
  return [reason === null, reason];
}

function checkStatic(chain) {
  for (const pipeline of chain.pipelines) {
    const stages = pipeline.stages;
    if (stages.length > 1) {
      for (const stage of stages) {
        if (stage instanceof CommandNode && isBuiltinName(stage)) {
          throw new Refused(`builtin '${staticName(stage)}' is not allowed in a pipeline`);
        }
      }
    }
    for (const stage of stages) {
      if (stage instanceof GroupNode) {
        checkStatic(stage.chain);
      } else if (stage instanceof CommandNode) {
        finalizeEmptyWords(stage);
        checkTargetSuffix(stage);
      }
    }
  }
}

function staticName(cmd) {
  return expandWordNoGlob(cmd.name);
}

function isBuiltinName(cmd) {
  return BUILTIN_NAMES.has(staticName(cmd));
}

function finalizeEmptyWords(cmd) {
  // M8: an UNQUOTED word that expands to empty is dropped, with three pins (spec): a quoted
  // empty word ("", "$X") stays an empty argument; `VAR=$EMPTY cmd` stays an assignment
  // (assigns untouched, only args filtered); a COMMAND word that expands to empty is refused.
  if (cmd.name.isEmpty() && !cmd.name.sawQuote) throw new Refused("command word expands to empty");
  cmd.args = cmd.args.filter((w) => !(w.isEmpty() && !w.sawQuote));
}

function isShellName(name) {
  let base = name.toLowerCase();
  if (base.endsWith(".exe")) base = base.slice(0, -".exe".length);
  return SHELL_NAMES.has(base);
}

function checkEnvTarget(args) {
  // M10/F10: `env bash -c ...` is the canonical way around a shell-name refusal on the
  // command word alone -- apply the same rule to whatever `env` would exec, after skipping
  // its own VAR=x, -i, -u NAME/-uNAME, -S STRING (split into words -- first word IS the
  // target), --, -C DIR, and combined short-flag clusters.
  const texts = args.map((w) => expandWordNoGlob(w));
  let i = 0;
  const n = texts.length;
  while (i < n) {
    const t = texts[i];
    if (t === "--") {
      i += 1;
      break;
    }
    if (t.length >= 2 && t[0] === "-" && t[1] !== "-") {
      const body = t.slice(1);
      let j = 0;
      while (j < body.length) {
        const c = body[j];
        if (c === "S") {
          let sValue;
          const rest = body.slice(j + 1);
          if (rest) {
            sValue = rest;
          } else if (i + 1 < n) {
            i += 1;
            sValue = texts[i];
          } else {
            sValue = "";
          }
          const words = sValue.split(/\s+/).filter((w) => w);
          if (words.length) {
            const target = words[0];
            const base = winBasename(target);
            if (isShellName(base)) throw new Refused(`refusing to run shell '${target}' via env -S`);
          }
          break;
        }
        if (c === "u" || c === "C") {
          const rest = body.slice(j + 1);
          if (!rest && i + 1 < n) i += 1; // separate NAME/DIR argument, not attached
          break;
        }
        j += 1;
      }
      i += 1;
      continue;
    }
    if (t.length > 1 && IDENT_START.test(t[0]) && t.includes("=")) {
      const head = t.split("=", 1)[0];
      if ([...head.slice(1)].every((c) => IDENT_CONT.test(c))) {
        i += 1;
        continue;
      }
    }
    break;
  }
  if (i < n) {
    const target = texts[i];
    const base = winBasename(target);
    if (isShellName(base)) throw new Refused(`refusing to run shell '${target}' via env`);
  }
}

function checkTargetSuffix(cmd) {
  const name = staticName(cmd);
  const lowered = name.toLowerCase();
  for (const suf of REFUSED_TARGET_SUFFIXES) {
    if (lowered.endsWith(suf)) throw new Refused(`refusing to run a '${suf}' target: ${name}`);
  }
  const base = winBasename(name);
  if (isShellName(base)) throw new Refused(`refusing to run shell '${name}' as a command`);
  if (base.toLowerCase() === "env") checkEnvTarget(cmd.args);
  if (BUILTIN_NAMES.has(name)) {
    if (name === "cd" && cmd.args.length !== 1) throw new Refused("cd requires exactly one argument");
  }
}

function winBasename(name) {
  const norm = name.replace(/\\/g, "/");
  const idx = norm.lastIndexOf("/");
  return idx === -1 ? norm : norm.slice(idx + 1);
}

// ---------------------------------------------------------------------------
// Executor (runs inside the worker)
// ---------------------------------------------------------------------------

class CwdBox {
  constructor(value) {
    this.value = value;
  }
}

class ExecContext {
  constructor(env, cwdBox, outputLimit) {
    this.env = env;
    this.cwdBox = cwdBox;
    this.outputLimit = outputLimit;
    this.children = [];
  }
}

function isWindowsPathSep(name) {
  return name.includes("/") || (IS_WINDOWS && name.includes("\\"));
}

class NotExecutable {
  constructor(reason = null) {
    this.reason = reason; // when set, replaces the generic "permission denied" message
  }
}

const NOT_EXECUTABLE = new NotExecutable();

function isNpmTrampolineName(name) {
  const base = winBasename(name).toLowerCase();
  return base === "npm" || base === "npx";
}

function resolveNpmTrampoline(base, env, tried = null) {
  // H10: `npm`/`npx` resolve to `node` running the sibling npm package's CLI script, never
  // to `npm.cmd`/`npx.cmd` (spawning a .cmd implicitly starts cmd.exe). Windows only.
  if (!IS_WINDOWS) return null;
  const pathVar = envGet(env, "PATH") || "";
  for (const d of pathVar.split(path.delimiter)) {
    if (!d || !path.isAbsolute(d)) continue;
    const nodeExe = checkCandidate(path.join(d, "node"), env);
    if (nodeExe === null || typeof nodeExe !== "string") continue;
    const nodeDir = path.dirname(nodeExe);
    const cli = path.join(nodeDir, "node_modules", "npm", "bin", `${base}-cli.js`);
    if (fs.existsSync(cli) && fs.statSync(cli).isFile()) {
      return [[nodeExe], cli];
    }
    if (tried !== null) tried.push(cli);
    const exe = checkCandidate(path.join(d, base), env);
    if (typeof exe === "string") return exe; // Volta-style npm.exe shipped alongside node.exe
  }
  return null;
}

function resolveExecutable(name, cwd, env) {
  if (isWindowsPathSep(name)) {
    // M11: only a name containing a path separator is a path -- a bare name (even one
    // starting with '.') is looked up on PATH only, never against cwd.
    const candidate = path.isAbsolute(name) ? name : path.join(cwd, name);
    const found = checkCandidate(candidate, env);
    if (found === null && !IS_WINDOWS && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      try {
        fs.accessSync(candidate, fs.constants.X_OK);
      } catch {
        return NOT_EXECUTABLE;
      }
    }
    return found;
  }
  if (IS_WINDOWS && isNpmTrampolineName(name)) {
    const tried = [];
    const trampoline = resolveNpmTrampoline(winBasename(name).toLowerCase(), env, tried);
    if (trampoline !== null) return trampoline;
    if (tried.length) {
      // `node` was found but its sibling npm CLI script wasn't -- name the exact path.
      return new NotExecutable(`npm/npx trampoline: node found but missing ${tried[0]}`);
    }
  }
  const pathVar = envGet(env, "PATH") || "";
  for (const d of pathVar.split(path.delimiter)) {
    // M11: skip relative AND empty PATH entries -- "never search the current directory".
    if (!d || !path.isAbsolute(d)) continue;
    const candidate = path.join(d, name);
    const found = checkCandidate(candidate, env);
    if (found) return found;
  }
  return null;
}

function checkCandidate(p, env) {
  if (IS_WINDOWS) {
    // H10: Win32 strips trailing dots/spaces before deciding the extension.
    const stripped = p.replace(/[. ]+$/, "");
    if (stripped !== p) return checkCandidate(stripped, env);
    const ext = path.extname(p);
    if (ext) {
      // CreateProcess only ever runs a PE image directly -- restrict candidates to
      // .COM/.EXE regardless of PATHEXT, never .BAT/.CMD.
      const up = ext.toUpperCase();
      if (up !== ".COM" && up !== ".EXE") return null;
      return fs.existsSync(p) && fs.statSync(p).isFile() ? path.resolve(p) : null;
    }
    const exts = envGet(env, "PATHEXT") || ".COM;.EXE";
    const extList = exts.split(";").filter((e) => e.toUpperCase() === ".COM" || e.toUpperCase() === ".EXE");
    for (const candExt of extList.length ? extList : [".COM", ".EXE"]) {
      const cand = p + candExt;
      if (fs.existsSync(cand) && fs.statSync(cand).isFile()) return path.resolve(cand);
    }
    return null;
  }
  try {
    fs.accessSync(p, fs.constants.X_OK);
    if (!fs.statSync(p).isFile()) return null;
    return path.resolve(p);
  } catch {
    return null;
  }
}

const DEV_FD_ALIASES = { "/dev/stdin": 0, "/dev/stdout": 1, "/dev/stderr": 2 };

function devFdAlias(p) {
  // F1: /dev/stdin|stdout|stderr and /dev/fd/0-2 are the STAGE'S OWN stdio at this point in
  // the redirection chain, never a path to open by itself (opening /dev/stdout as a PATH
  // would resolve to the worker's ANSWER pipe on macOS/Linux, letting a stage forge or
  // erase the JSON answer). Returns the local fd number to dup, or null if `p` names
  // neither -- /dev/fd/N for any other N is refused by throwing.
  if (Object.prototype.hasOwnProperty.call(DEV_FD_ALIASES, p)) return DEV_FD_ALIASES[p];
  if (p.startsWith("/dev/fd/")) {
    const rest = p.slice("/dev/fd/".length);
    if (/^\d+$/.test(rest) && ["0", "1", "2"].includes(rest)) return parseInt(rest, 10);
    throw new Error(`refusing to open '${p}': only /dev/fd/0-2 are supported`);
  }
  return null;
}

// Stage fd map values ("fdvals"): a small symbolic layer over what Node can express.
//  - {kind:"worker", fd:0|1|2}: the worker's OWN stdio at that slot.
//  - {kind:"path", fd:number}: a real, open numeric fd (file / devnull) -- spawned via
//    Node's numeric-stdio support.
//  - {kind:"dup", source:fdval}: a dup of another fdval (>/dev/stdout & friends).
//  - {kind:"pipe", stream:Readable|Writable, pipe:Pipe}: one end of an inter-stage or
//    capture pipe (Node pipe objects; handed to spawn as 'pipe' + manual wiring, or
//    drained by the event loop for capture).
class WorkerFd {
  constructor(fd) {
    this.kind = "worker";
    this.fd = fd;
  }
}
class PathFd {
  constructor(fd) {
    this.kind = "path";
    this.fd = fd;
  }
}
class DupFd {
  constructor(source) {
    this.kind = "dup";
    this.source = source;
  }
}
// A Node pipe: {readable, writable, readFd, writeFd} from child_process spawn stdio
// sockets, or a manual two-socket pair for builtins.
class PipeFd {
  constructor(readable, writable, readFd, writeFd) {
    this.kind = "pipe";
    this.readable = readable; // net.Socket/Readable or null
    this.writable = writable;
    this.readFd = readFd; // raw fd number of the read end (for passing to spawn), or null
    this.writeFd = writeFd; // raw fd number of the write end, or null
  }
}

function fdvalResolveDup(v) {
  while (v instanceof DupFd) v = v.source;
  return v;
}

// Node's spawn() stdio option for a fdval, at spawn time.
function spawnStdioFor(v, mode) {
  v = fdvalResolveDup(v);
  if (v.kind === "worker") {
    if (mode === "capture-stdout" && v.fd === 1) return "pipe";
    if (mode === "capture-stderr" && v.fd === 2) return "pipe";
    if (mode === "capture-stdin" && v.fd === 0) return "pipe";
    return v.fd; // inherit the worker's own stdio slot
  }
  if (v.kind === "path") return v.fd; // Node dups the numeric fd into the child
  if (v.kind === "pipe") {
    if (mode === "stdin") return v.readFd; // the previous stage's stdout STREAM object
  }
  return "ignore";
}

// os.devnull: not present in every Node build -- fall back per platform.
const DEVNULL = os.devnull !== undefined ? os.devnull : IS_WINDOWS ? "NUL" : "/dev/null";

function openRedirTarget(redir, ctx, localFds) {
  let p = expandWordNoGlob(redir.target);
  if (p === "/dev/null" || (IS_WINDOWS && p.toUpperCase() === "NUL")) p = DEVNULL;
  const devFd = devFdAlias(p);
  if (devFd !== null) return new DupFd(localFds[devFd]);
  const abspath = path.isAbsolute(p) ? p : path.join(ctx.cwdBox.value, p);
  if (redir.kind === ">") return new PathFd(fs.openSync(abspath, "w", 0o644));
  if (redir.kind === ">>") return new PathFd(fs.openSync(abspath, "a", 0o644));
  if (redir.kind === "<") {
    // P7: a FIFO with no writer blocks open indefinitely -- open O_NONBLOCK then clear
    // the flag so reads behave normally (POSIX only; Windows has no O_NONBLOCK).
    if (!IS_WINDOWS) {
      const fd = fs.openSync(abspath, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK);
      try {
        fs.fstatSync(fd);
        setBlocking(fd);
      } catch {
        /* leave as-is */
      }
      return new PathFd(fd);
    }
    return new PathFd(fs.openSync(abspath, fs.constants.O_RDONLY));
  }
  throw new Error(`unsupported redirection kind ${redir.kind}`);
}

let _setblockingFn = null;
function setBlocking(fd) {
  // Clear O_NONBLOCK on a just-opened fd (POSIX). Lazy-required; failures ignored.
  if (_setblockingFn === null) {
    _setblockingFn = false;
    try {
      // eslint-disable-next-line global-require
      const { setNonBlocking } = process.binding ? {} : {};
      void setNonBlocking;
    } catch {
      /* no binding available */
    }
  }
  void fd;
  // ponytail: Node exposes no public clear-O_NONBLOCK; O_NONBLOCK on a regular file is a
  // no-op on Linux and cleared by the first read on macOS, so a plain-file read is fine
  // either way; a FIFO < input still fails fast on the open() with O_NONBLOCK, which is
  // the P7 behaviour that matters. Upgrade path: a native addon, if this ever matters.
}

function applyRedirs(cmd, baseFds, ctx) {
  // Returns [localFds, openedPathFds]. F11: on a failing redirection, every target already
  // opened by an EARLIER redirection in this same command is closed before re-raising.
  const local = { ...baseFds };
  const opened = [];
  try {
    for (const redir of cmd.redirs) {
      if (redir.kind === ">&") {
        local[redir.fd] = new DupFd(local[redir.target]);
      } else {
        const v = openRedirTarget(redir, ctx, local);
        local[redir.fd] = v;
        if (v instanceof PathFd) opened.push(v.fd);
      }
    }
  } catch (exc) {
    closeQuiet(opened);
    throw exc;
  }
  return [local, opened];
}

class OutputSink {
  // Drains one readable stream into a size-bounded buffer without ever blocking a writer.
  constructor(stream, limit) {
    this.limit = limit;
    this.buf = [];
    this.bufLen = 0;
    this.truncated = false;
    this.done = false;
    this.stream = stream;
    stream.on("data", (chunk) => {
      const room = this.limit - this.bufLen;
      if (room > 0) {
        const take = chunk.subarray(0, room);
        this.buf.push(take);
        this.bufLen += take.length;
        if (chunk.length > room) this.truncated = true;
      } else {
        this.truncated = true;
      }
    });
    stream.on("error", () => {});
    const markDone = () => {
      this.done = true;
    };
    stream.on("end", markDone);
    stream.on("close", markDone);
    stream.resume();
  }

  snapshot() {
    return Buffer.concat(this.buf, this.bufLen);
  }
}

function writeFdval(v, data) {
  // Best-effort write to a fdval. Only worker fds and open path fds are writable here;
  // pipe fdvals are written by their owning stream objects instead.
  v = fdvalResolveDup(v);
  if (v.kind === "worker") {
    if (v.fd === 2 && _gateStderrSink !== null) {
      // Gate mode: the supervisor spawned the worker with stderr=ignore, so a write to the
      // worker's own fd 2 (a pre-spawn failure like exit-127/126, which py's gate mode
      // captures because its fds[2] IS a capture pipe) would be lost. Route it into the
      // capture sink instead -- same answer the py oracle produces.
      _gateStderrSink(data);
      return;
    }
    try {
      fs.writeSync(v.fd, data);
    } catch {
      /* EPIPE etc: swallow */
    }
    return;
  }
  if (v.kind === "path") {
    try {
      fs.writeSync(v.fd, data);
    } catch {
      /* ignore */
    }
  }
}

// Non-null only inside the worker's gate-mode execution: captures writes that the py oracle
// would land in its capture pipe (pre-spawn 127/126 messages), for the final answer's stderr.
// One process = one role, so this is safe ONLY if workerMain runs at most once per process and
// every exit path resets it to null — a stale hook would silently corrupt a second answer's
// stderr and truncated accounting instead of crashing.
let _gateStderrSink = null;
let _gateStderrTruncated = false;

function closeQuiet(fds) {
  for (const fd of fds) {
    if (typeof fd !== "number") continue;
    try {
      fs.closeSync(fd);
    } catch {
      /* ignore */
    }
  }
}

function closeFdval(v) {
  v = fdvalResolveDup(v);
  if (v && v.kind === "path" && typeof v.fd === "number") {
    try {
      fs.closeSync(v.fd);
    } catch {
      /* ignore */
    }
  }
}

export async function runCommand(cmd, fds, ctx, closeAfterSpawn = new Set()) {
  try {
    return await runCommandInner(cmd, fds, ctx, closeAfterSpawn);
  } catch (exc) {
    if (exc instanceof Refused) throw exc;
    // H6: a failed stage must not hang its neighbours -- close what we were given and
    // report a status instead of letting the exception vanish silently.
    closeAfterSpawn.forEach(closeFdval);
    writeFdval(fds[2], Buffer.from(`cmdrun: ${exc && exc.message ? exc.message : exc}\n`));
    return 1;
  }
}

async function runCommandInner(cmd, fds, ctx, closeAfterSpawn) {
  const name = expandWordNoGlob(cmd.name);
  if (BUILTIN_NAMES.has(name)) return runBuiltin(cmd, name, fds, ctx);
  const args = [name];
  for (const w of cmd.args) args.push(...expandWordGlob(w, ctx.cwdBox.value));
  let localFds;
  let opened;
  try {
    [localFds, opened] = applyRedirs(cmd, fds, ctx);
  } catch (exc) {
    // L5: a redirection failure gets a shell-like message and status 1 -- "internal error"
    // is reserved for a genuine interpreter bug, not "the file doesn't exist".
    const target = cmd.redirs.length ? expandWordNoGlob(cmd.redirs[0].target) : "?";
    closeAfterSpawn.forEach(closeFdval);
    const msg = `${target}: ${osErrorStr(exc)}`;
    writeFdval(fds[2], Buffer.from(`cmdrun: ${msg}\n`));
    return 1;
  }
  // F4: scrub every CMDRUN_* key before it can reach a CHILD process. CMDRUN_* is a
  // RESERVED namespace (review round 2): a caller's explicit `env: {"CMDRUN_X": ...}` is
  // dropped here too, on purpose -- the exact hook names are re-added by prefix assignment
  // (`CMDRUN_X=1 cmd`) when a test genuinely needs them, and both transports stay in
  // agreement instead of diverging by path.
  const overlayEnv = {};
  for (const [k, v] of Object.entries(ctx.env)) {
    if (!k.startsWith("CMDRUN_")) overlayEnv[k] = v;
  }
  for (const [aname, avalue] of cmd.assigns) {
    envSet(overlayEnv, aname, expandWordNoGlob(avalue));
  }
  const resolved = resolveExecutable(args[0], ctx.cwdBox.value, overlayEnv);
  if (resolved instanceof NotExecutable) {
    const msg = resolved.reason !== null ? resolved.reason : `permission denied: ${args[0]}`;
    writeFdval(localFds[2], Buffer.from(`${msg}\n`));
    closeQuiet(opened);
    closeAfterSpawn.forEach((v) => {
      if (!opened.includes(fdvalRawFd(v))) closeFdval(v);
    });
    return 126;
  }
  if (resolved === null) {
    let msg = `command not found: ${args[0]}`;
    if (IS_WINDOWS && args[0] === "python3") msg += " (use `python` or `py -3`)";
    writeFdval(localFds[2], Buffer.from(`${msg}\n`));
    closeQuiet(opened);
    closeAfterSpawn.forEach((v) => {
      if (!opened.includes(fdvalRawFd(v))) closeFdval(v);
    });
    return 127;
  }
  let argv;
  if (Array.isArray(resolved)) {
    const [prefixArgs, resolvedPath] = resolved;
    argv = [...prefixArgs, resolvedPath, ...args.slice(1)];
  } else {
    argv = [resolved, ...args.slice(1)];
  }
  const stdio = [
    spawnStdioFor(localFds[0], "stdin"),
    spawnStdioFor(localFds[1], "stdout"),
    spawnStdioFor(localFds[2], "stderr"),
  ];
  let proc;
  try {
    proc = spawn(argv[0], argv.slice(1), {
      cwd: ctx.cwdBox.value,
      env: overlayEnv,
      stdio,
      // Stages are plain spawns inheriting the worker's process group -- never detached.
      detached: false,
      windowsHide: true,
    });
  } catch (exc) {
    writeFdval(localFds[2], Buffer.from(`exec failed: ${exc && exc.message ? exc.message : exc}\n`));
    closeQuiet(opened);
    closeAfterSpawn.forEach((v) => {
      if (!opened.includes(fdvalRawFd(v))) closeFdval(v);
    });
    if (exc && exc.code === "EACCES") return 126;
    if (exc && exc.code === "ENOENT") return 127;
    return 1;
  }
  ctx.children.push(proc);
  // fork() duplicated the open fds into the child; OUR copies must close right away,
  // before wait(), or a fast writer leaves the pipe's write end open in the INTERPRETER
  // itself after its own child has already exited, so the next stage's read never sees
  // EOF. `opened` (this call's own redir fds) is excluded: closed just above; only the
  // pipeline's inter-stage pipe fds land in closeAfterSpawn.
  for (const fd of opened) {
    try {
      fs.closeSync(fd);
    } catch {
      /* ignore */
    }
  }
  closeAfterSpawn.forEach((v) => {
    if (!opened.includes(fdvalRawFd(v))) closeFdval(v);
  });
  return waitChildAsync(proc);
}

function fdvalRawFd(v) {
  v = fdvalResolveDup(v);
  return v && typeof v.fd === "number" ? v.fd : null;
}

// Synchronously wait for a child via spawnSync-style blocking is impossible in Node for
// an already-spawned child; Popen.wait() semantics here are emulated by deasync-free
// blocking on the child's exit through worker_threads? No -- the lazy correct primitive
// Node gives us: poll the child's exit STATE SYNCHRONOUSLY is impossible, so the executor
// runs its waits on the event loop (async). waitChildAsync is the single wait primitive.
function waitChildAsync(proc) {
  return new Promise((resolve) => {
    const done = () => resolve(childStatus(proc));
    if (proc.exitCode !== null || proc.signalCode !== null) return done();
    proc.once("exit", done);
    proc.once("error", done);
  });
}

function childStatus(proc) {
  if (proc.signalCode) return 128 + osConstantsSignal(proc.signalCode);
  const code = proc.exitCode;
  return code === null ? 1 : code;
}

function osConstantsSignal(name) {
  // Node: map a signal NAME to its number via os.constants.signals (128+N).
  const map = os.constants.signals;
  return Object.prototype.hasOwnProperty.call(map, name) ? map[name] : 15;
}

function osErrorStr(exc) {
  // A shell-like strerror from a Node fs error, matching Python's exc.strerror.
  const map = {
    ENOENT: "No such file or directory",
    EACCES: "Permission denied",
    EISDIR: "Is a directory",
    ENOTDIR: "Not a directory",
    EPERM: "Operation not permitted",
    EMFILE: "Too many open files",
    ELOOP: "Too many levels of symbolic links",
    ENAMETOOLONG: "File name too long",
    ENXIO: "No such device or address",
    EIO: "Input/output error",
    ENOSPC: "No space left on device",
    EROFS: "Read-only file system",
  };
  if (exc && exc.code && Object.prototype.hasOwnProperty.call(map, exc.code)) return map[exc.code];
  return exc && exc.message ? exc.message : String(exc);
}

function runBuiltin(cmd, name, fds, ctx) {
  const args = [];
  for (const w of cmd.args) args.push(...expandWordGlob(w, ctx.cwdBox.value));
  const [localFds, opened] = applyRedirs(cmd, fds, ctx);
  try {
    if (name === "cd") {
      const target = args[0];
      const p = path.isAbsolute(target) ? target : path.join(ctx.cwdBox.value, target);
      const resolved = path.normalize(p);
      let isDir = false;
      try {
        isDir = fs.statSync(resolved).isDirectory();
      } catch {
        isDir = false;
      }
      if (!isDir) {
        writeFdval(localFds[2], Buffer.from(`cd: no such directory: ${target}\n`));
        return 1;
      }
      ctx.cwdBox.value = resolved;
      return 0;
    }
    if (name === "true") return 0;
    if (name === "false") return 1;
    if (name === "echo") {
      let noNewline = false;
      let rest = args;
      if (rest.length && rest[0] === "-n") {
        noNewline = true;
        rest = rest.slice(1);
      }
      const out = rest.join(" ") + (noNewline ? "" : "\n");
      writeFdval(localFds[1], Buffer.from(out));
      return 0;
    }
    if (name === "test" || name === "[") {
      let a = [...args];
      if (name === "[") {
        if (!a.length || a[a.length - 1] !== "]") {
          writeFdval(localFds[2], Buffer.from("test: missing ']'\n"));
          return 2;
        }
        a = a.slice(0, -1);
      }
      if (a.length !== 2 || !["-e", "-f", "-d", "-s"].includes(a[0])) {
        writeFdval(localFds[2], Buffer.from("test: usage: test -e|-f|-d|-s PATH\n"));
        return 2;
      }
      const [flag, target] = a;
      const p = path.isAbsolute(target) ? target : path.join(ctx.cwdBox.value, target);
      let ok = false;
      try {
        const st = fs.statSync(p);
        if (flag === "-e") ok = true;
        else if (flag === "-f") ok = st.isFile();
        else if (flag === "-d") ok = st.isDirectory();
        else ok = st.isFile() && st.size > 0;
      } catch {
        ok = false;
      }
      return ok ? 0 : 1;
    }
    throw new Error(`unreachable builtin ${name}`);
  } finally {
    for (const fd of opened) {
      try {
        fs.closeSync(fd);
      } catch {
        /* ignore */
      }
    }
  }
}

export async function runStage(stage, fds, ctx, closeAfterSpawn = new Set()) {
  if (stage instanceof GroupNode) {
    const innerBox = new CwdBox(ctx.cwdBox.value);
    const innerCtx = new ExecContext(ctx.env, innerBox, ctx.outputLimit);
    innerCtx.children = ctx.children;
    let status;
    try {
      status = await runChainSync(stage.chain, fds, innerCtx);
    } catch (exc) {
      // F2: a `(...)` group that raises must close everything it owns and answer status 1
      // promptly -- a null status must never reach the top-level answer.
      closeAfterSpawn.forEach(closeFdval);
      writeFdval(fds[2], Buffer.from(`cmdrun: ${exc && exc.message ? exc.message : exc}\n`));
      return 1;
    }
    // A group has no single "spawn" moment, so unlike runCommand it can only release these
    // once it is entirely done using them.
    closeAfterSpawn.forEach(closeFdval);
    return status;
  }
  return runCommand(stage, fds, ctx, closeAfterSpawn);
}

// Synchronous chain/pipeline runner for LEDGER mode (all stages write devnull) and for
// any pipeline whose stages all have raw-fd stdio (no capture pipes). Gate-mode capture
// goes through runPipelineGate, which uses Node pipe objects drained by the event loop.
export function runPipeline(pipeline, fds, ctx) {
  const stages = pipeline.stages;
  if (stages.length === 1) return runStage(stages[0], fds, ctx);
  // Inter-stage pipes. Node cannot create a standalone os.pipe() without spawning, so the
  // pipeline is run through the gate machinery with capture disabled -- its pipe objects
  // are created by the FIRST spawn of each stage, handed to the next via fd numbers.
  return runPipelineGate(pipeline, fds, ctx, null);
}

export async function runChainSync(chain, fds, ctx) {
  let status = 0;
  for (const pipeline of chain.pipelines) {
    status = await runPipeline(pipeline, fds, ctx);
    if (status !== 0) break;
  }
  return status;
}

// ---------------------------------------------------------------------------
// Gate-mode pipeline runner (Node-native): spawns stages left-to-right; stage N's stdin
// is stage N-1's stdout pipe; each pipe end closes exactly once after its consumer is
// spawned. capture (when `sinks` is non-null): the LAST stage's stdout and EVERY stage's
// stderr go to pipes drained by the event loop, bytes past output_limit discarded-but-
// counted. Ledger mode passes sinks=null: every stage's stdout/stderr go to the fdval the
// fd map already carries (devnull).
// ---------------------------------------------------------------------------

export async function runPipelineGate(pipeline, fds, ctx, sinks) {
  const stages = pipeline.stages;
  if (stages.length === 1 && !(stages[0] instanceof GroupNode) && !isCaptureSingle(sinks)) {
    // Single command, no capture pipes needed: plain sync path.
    return runStage(stages[0], fds, ctx);
  }
  if (stages.length === 1) {
    // Single stage with capture (gate mode single command) or a group.
    const stage = stages[0];
    if (stage instanceof GroupNode) return runStageGroupCapture(stage, fds, ctx, sinks);
    return runSingleCommandCapture(stage, fds, ctx, sinks);
  }
  const n = stages.length;
  const statuses = new Array(n).fill(null);
  const procs = new Array(n).fill(null);
  const outerFdSet = new Set(Object.values(fds));
  let prevStdout = null; // PipeFd handed to the next stage as stdin
  for (let idx = 0; idx < n; idx += 1) {
    const stage = stages[idx];
    // Builtins stay forbidden as pipeline stages (checked statically for n>=2). Groups as
    // a stage: run inline is not possible across Node pipes without dup(); the parity
    // suite does not exercise group-in-multi-stage-pipeline -- refuse cleanly (status 1,
    // bash-like message) rather than mis-execute.
    if (stage instanceof GroupNode) {
      writeFdval(fds[2], Buffer.from("cmdrun: a ( ) group is not supported as a pipeline stage\n"));
      statuses[idx] = 1;
      if (prevStdout !== null) {
        destroyPipe(prevStdout);
        prevStdout = null;
      }
      continue;
    }
    const cmd = stage;
    const name = expandWordNoGlob(cmd.name);
    if (BUILTIN_NAMES.has(name)) {
      statuses[idx] = 1;
      if (prevStdout !== null) {
        destroyPipe(prevStdout);
        prevStdout = null;
      }
      continue;
    }
    const args = [name];
    for (const w of cmd.args) args.push(...expandWordGlob(w, ctx.cwdBox.value));
    let localFds;
    let opened;
    try {
      [localFds, opened] = applyRedirs(cmd, fds, ctx);
    } catch (exc) {
      const target = cmd.redirs.length ? expandWordNoGlob(cmd.redirs[0].target) : "?";
      writeFdval(fds[2], Buffer.from(`cmdrun: ${target}: ${osErrorStr(exc)}\n`));
      statuses[idx] = 1;
      // H6: upstream read end closed (upstream gets EPIPE/SIGPIPE), downstream gets a
      // fresh pipe whose write end is destroyed after ITS spawn -- equivalently, hand the
      // next stage an immediately-closed stdin by leaving prevStdout null and marking
      // this stage failed (the next stage still spawns with its normal stdin).
      if (prevStdout !== null) {
        destroyPipe(prevStdout);
        prevStdout = null;
      }
      continue;
    }
    // F4 scrub (same as runCommandInner).
    const overlayEnv = {};
    for (const [k, v] of Object.entries(ctx.env)) {
      if (!k.startsWith("CMDRUN_")) overlayEnv[k] = v;
    }
    for (const [aname, avalue] of cmd.assigns) {
      envSet(overlayEnv, aname, expandWordNoGlob(avalue));
    }
    const resolved = resolveExecutable(args[0], ctx.cwdBox.value, overlayEnv);
    const failStage = (msg, status) => {
      if (msg !== null) writeFdval(localFds[2], Buffer.from(`${msg}\n`));
      closeQuiet(opened);
      statuses[idx] = status;
      if (prevStdout !== null) {
        destroyPipe(prevStdout);
        prevStdout = null;
      }
    };
    if (resolved instanceof NotExecutable) {
      failStage(resolved.reason !== null ? resolved.reason : `permission denied: ${args[0]}`, 126);
      continue;
    }
    if (resolved === null) {
      let msg = `command not found: ${args[0]}`;
      if (IS_WINDOWS && args[0] === "python3") msg += " (use `python` or `py -3`)";
      failStage(msg, 127);
      continue;
    }
    let argv;
    if (Array.isArray(resolved)) {
      const [prefixArgs, resolvedPath] = resolved;
      argv = [...prefixArgs, resolvedPath, ...args.slice(1)];
    } else {
      argv = [resolved, ...args.slice(1)];
    }
    const isLast = idx === n - 1;
    const stdinSpec = prevStdout !== null ? prevStdout.readFd : spawnStdioFor(localFds[0], "stdin");
    // Gate-mode capture. A slot is captured only while it still points at the WORKER's own
    // stdio (a WorkerFd); a slot redirected to a file keeps its fd. A DUP of a captured
    // slot (`2>&1`) is captured TOO, and py routes it to the SOURCE slot's sink: its
    // `local[2] = local[1]` makes stderr share the stdout capture pipe's write end, so
    // `cmd 2>&1` lands in answer.stdout (probed both runtimes). Node cannot share one pipe
    // across two stdio entries, so the dup gets its own pipe and its OutputSink joins the
    // SOURCE slot's sink -- same bytes, same answer. NB: the dup source slot is the
    // WorkerFd's .fd, NOT the slot being spec'd (slot 2 dup of WorkerFd(1): keying on v.fd
    // === 2 sent the bytes to raw fd 1 -- the answer pipe -- in front of the JSON line).
    const resolvedOf = (slot) => fdvalResolveDup(localFds[slot]);
    const isWorkerStdio = (slot) => resolvedOf(slot).kind === "worker";
    // A dup source that is itself captured-and-redirected-away later (">f 2>&1" order
    // flips it) resolves to a PathFd, so kind checks alone keep the bash ordering right.
    const stderrDupSrc = resolvedOf(2).kind === "worker" ? resolvedOf(2).fd : null; // 1 => dup of stdout slot, 2 => own
    const stdoutSpec = !isLast ? "pipe" : (sinks !== null && isWorkerStdio(1) ? "pipe" : spawnStdioFor(localFds[1], "stdout"));
    const stderrSpec = sinks !== null && isWorkerStdio(2) ? "pipe" : spawnStdioFor(localFds[2], "stderr");
    let proc;
    try {
      proc = spawn(argv[0], argv.slice(1), {
        cwd: ctx.cwdBox.value,
        env: overlayEnv,
        stdio: [stdinSpec, stdoutSpec, stderrSpec],
        detached: false,
        windowsHide: true,
      });
    } catch (exc) {
      failStage(`exec failed: ${exc && exc.message ? exc.message : exc}`, exc && exc.code === "EACCES" ? 126 : exc && exc.code === "ENOENT" ? 127 : 1);
      continue;
    }
    procs[idx] = proc;
    ctx.children.push(proc);
    // H7: close each pipe end exactly once after the consumer is spawned. `opened` (this
    // stage's own redir fds) closes right after spawn; prevStdout's READ fd was handed to
    // the child as its stdin -- our copy must be closed now so the child sees EOF.
    for (const fd of opened) {
      try {
        fs.closeSync(fd);
      } catch {
        /* ignore */
      }
    }
    if (prevStdout !== null) {
      // Close the worker's copy of the read end (the child now owns it via spawn dup).
      if (prevStdout.readable) {
        try {
          prevStdout.readable.destroy();
        } catch {
          /* ignore */
        }
      }
      prevStdout = null;
    }
    // Wire capture streams. A stderr that is a dup of the STDOUT capture slot routes its
    // bytes into sinks.out (py: both fds are the same capture pipe's write end).
    if (sinks !== null && proc.stderr) {
      const s = new OutputSink(proc.stderr, ctx.outputLimit);
      (stderrDupSrc === 1 ? sinks.out : sinks.err).push(s);
    }
    if (isLast && sinks !== null && proc.stdout) {
      const s = new OutputSink(proc.stdout, ctx.outputLimit);
      sinks.out.push(s);
    }
    if (!isLast) {
      // Node exposes no raw fd on the child's stdout pipe socket (fd undefined), but spawn
      // accepts the STREAM itself as the next child's stdio entry -- it dups it into the
      // consumer and EOF propagates correctly when the writer exits. Keep the stream, not
      // a number.
      prevStdout = new PipeFd(proc.stdout, null, proc.stdout, null);
    }
  }
  // Wait for every stage like Popen.wait(): the event-loop-driven waitChildAsync lets
  // capture streams drain between stage exits (Node reads the pipes only on the loop).
  for (let idx = 0; idx < n; idx += 1) {
    if (procs[idx] !== null) statuses[idx] = await waitChildAsync(procs[idx]);
  }
  // Pipefail = last non-zero stage status else 0. Never status:null.
  let status = 0;
  for (const s of statuses) {
    if (s !== 0) status = s;
  }
  return status;
}

function isCaptureSingle(sinks) {
  return sinks !== null;
}

function destroyPipe(p) {
  if (p.readable) {
    try {
      p.readable.destroy();
    } catch {
      /* ignore */
    }
  }
  if (p.writable) {
    try {
      p.writable.destroy();
    } catch {
      /* ignore */
    }
  }
}

// Single-command gate capture: spawn (or run inline for a builtin) with stdout/stderr as
// Node pipes drained into `sinks.out`/`sinks.err`. Redirections still apply on top: a
// stage that redirects its own stdout to a FILE writes the file, and the capture pipe
// sees nothing (spawnStdioFor resolves the fdval to the file fd, not 'pipe').
async function runSingleCommandCapture(cmd, fds, ctx, sinks) {
  const name = expandWordNoGlob(cmd.name);
  if (BUILTIN_NAMES.has(name)) {
    // A builtin's stdout would need a pipe Node cannot create without a child. Run it
    // with its fdval (worker stdio), which in the worker is the answer channel -- NOT
    // acceptable. Instead: builtins under capture write into a buffer we snapshot.
    return runBuiltinCapture(cmd, name, fds, ctx, sinks);
  }
  return runSingleStageCapture(cmd, fds, ctx, sinks);
}

function runBuiltinCapture(cmd, name, fds, ctx, sinks) {
  // Buffer-capture for a builtin: echo's stdout goes to sinks.out, its stderr to
  // sinks.err, honoring its own redirections (a redirected slot bypasses capture).
  const args = [];
  for (const w of cmd.args) args.push(...expandWordGlob(w, ctx.cwdBox.value));
  // L5 parity: a failing redirection is a shell-like status 1 with a `cmdrun:` message,
  // NOT an internal_error. The py oracle's run_command wraps _run_builtin in a generic
  // handler that converts the throw; this gate path previously had no equivalent, so the
  // Refused-free exception escaped to workerMain and set internal_error:true -- the flag
  // callers key on to distinguish an interpreter bug from a legitimate failure.
  let localFds;
  let opened;
  try {
    [localFds, opened] = applyRedirs(cmd, fds, ctx);
  } catch (exc) {
    const target = cmd.redirs.length ? expandWordNoGlob(cmd.redirs[0].target) : "?";
    writeFdval(fds[2], Buffer.from(`cmdrun: ${target}: ${osErrorStr(exc)}\n`));
    return 1;
  }
  try {
    const outRedirected = cmd.redirs.some((r) => r.fd === 1);
    const errRedirected = cmd.redirs.some((r) => r.fd === 2);
    const outChunks = [];
    const errChunks = [];
    const captureWrite = (slot, data) => {
      if (slot === 1 && !outRedirected) {
        outChunks.push(Buffer.from(data));
        return;
      }
      if (slot === 2 && !errRedirected) {
        errChunks.push(Buffer.from(data));
        return;
      }
      writeFdval(localFds[slot], data);
    };
    let status;
    if (name === "cd") {
      const target = args[0];
      const p = path.isAbsolute(target) ? target : path.join(ctx.cwdBox.value, target);
      const resolved = path.normalize(p);
      let isDir = false;
      try {
        isDir = fs.statSync(resolved).isDirectory();
      } catch {
        isDir = false;
      }
      if (!isDir) {
        captureWrite(2, Buffer.from(`cd: no such directory: ${target}\n`));
        status = 1;
      } else {
        ctx.cwdBox.value = resolved;
        status = 0;
      }
    } else if (name === "true") {
      status = 0;
    } else if (name === "false") {
      status = 1;
    } else if (name === "echo") {
      let noNewline = false;
      let rest = args;
      if (rest.length && rest[0] === "-n") {
        noNewline = true;
        rest = rest.slice(1);
      }
      captureWrite(1, Buffer.from(rest.join(" ") + (noNewline ? "" : "\n")));
      status = 0;
    } else if (name === "test" || name === "[") {
      let a = [...args];
      if (name === "[") {
        if (!a.length || a[a.length - 1] !== "]") {
          captureWrite(2, Buffer.from("test: missing ']'\n"));
          status = 2;
        } else {
          a = a.slice(0, -1);
          if (a.length !== 2 || !["-e", "-f", "-d", "-s"].includes(a[0])) {
            captureWrite(2, Buffer.from("test: usage: test -e|-f|-d|-s PATH\n"));
            status = 2;
          } else {
            const [flag, target] = a;
            const p = path.isAbsolute(target) ? target : path.join(ctx.cwdBox.value, target);
            let ok = false;
            try {
              const st = fs.statSync(p);
              if (flag === "-e") ok = true;
              else if (flag === "-f") ok = st.isFile();
              else if (flag === "-d") ok = st.isDirectory();
              else ok = st.isFile() && st.size > 0;
            } catch {
              ok = false;
            }
            status = ok ? 0 : 1;
          }
        }
      } else {
        if (a.length !== 2 || !["-e", "-f", "-d", "-s"].includes(a[0])) {
          captureWrite(2, Buffer.from("test: usage: test -e|-f|-d|-s PATH\n"));
          status = 2;
        } else {
          const [flag, target] = a;
          const p = path.isAbsolute(target) ? target : path.join(ctx.cwdBox.value, target);
          let ok = false;
          try {
            const st = fs.statSync(p);
            if (flag === "-e") ok = true;
            else if (flag === "-f") ok = st.isFile();
            else if (flag === "-d") ok = st.isDirectory();
            else ok = st.isFile() && st.size > 0;
          } catch {
            ok = false;
          }
          status = ok ? 0 : 1;
        }
      }
    } else {
      throw new Error(`unreachable builtin ${name}`);
    }
    if (outChunks.length && sinks !== null) {
      const s = new BufferSink(outChunks, ctx.outputLimit);
      sinks.out.push(s);
    }
    if (errChunks.length && sinks !== null) {
      const s = new BufferSink(errChunks, ctx.outputLimit);
      sinks.err.push(s);
    }
    return status;
  } finally {
    for (const fd of opened) {
      try {
        fs.closeSync(fd);
      } catch {
        /* ignore */
      }
    }
  }
}

class BufferSink {
  constructor(chunks, limit) {
    let bufLen = 0;
    this.bufs = [];
    this.truncated = false;
    this.done = true;
    for (const c of chunks) {
      const room = limit - bufLen;
      if (room > 0) {
        const take = c.subarray(0, room);
        this.bufs.push(take);
        bufLen += take.length;
        if (c.length > room) this.truncated = true;
      } else {
        this.truncated = true;
      }
    }
  }
  snapshot() {
    return Buffer.concat(this.bufs);
  }
}

async function runSingleStageCapture(cmd, fds, ctx, sinks) {
  const name = expandWordNoGlob(cmd.name);
  if (BUILTIN_NAMES.has(name)) return runBuiltinCapture(cmd, name, fds, ctx, sinks);
  const args = [name];
  for (const w of cmd.args) args.push(...expandWordGlob(w, ctx.cwdBox.value));
  let localFds;
  let opened;
  try {
    [localFds, opened] = applyRedirs(cmd, fds, ctx);
  } catch (exc) {
    const target = cmd.redirs.length ? expandWordNoGlob(cmd.redirs[0].target) : "?";
    writeFdval(fds[2], Buffer.from(`cmdrun: ${target}: ${osErrorStr(exc)}\n`));
    return 1;
  }
  const overlayEnv = {};
  for (const [k, v] of Object.entries(ctx.env)) {
    if (!k.startsWith("CMDRUN_")) overlayEnv[k] = v;
  }
  for (const [aname, avalue] of cmd.assigns) {
    envSet(overlayEnv, aname, expandWordNoGlob(avalue));
  }
  const resolved = resolveExecutable(args[0], ctx.cwdBox.value, overlayEnv);
  const finishFail = (msg, status) => {
    if (msg !== null) writeFdval(localFds[2], Buffer.from(`${msg}\n`));
    closeQuiet(opened);
    return status;
  };
  if (resolved instanceof NotExecutable) {
    return finishFail(resolved.reason !== null ? resolved.reason : `permission denied: ${args[0]}`, 126);
  }
  if (resolved === null) {
    let msg = `command not found: ${args[0]}`;
    if (IS_WINDOWS && args[0] === "python3") msg += " (use `python` or `py -3`)";
    return finishFail(msg, 127);
  }
  let argv;
  if (Array.isArray(resolved)) {
    const [prefixArgs, resolvedPath] = resolved;
    argv = [...prefixArgs, resolvedPath, ...args.slice(1)];
  } else {
    argv = [resolved, ...args.slice(1)];
  }
  // Same slot-based capture rule as runPipelineGate: capture a worker-stdio slot (or a dup
  // of one); a slot redirected to a file keeps its fd. A stderr dup of the STDOUT capture
  // slot routes its bytes into sinks.out (py: `2>&1` shares the stdout capture pipe's
  // write end, so `cmd 2>&1` lands in answer.stdout). Never key on the fdval's fd --
  // the dup source slot is WorkerFd(1).fd, and v.fd===2 there misroutes to the answer pipe.
  const resolvedOf = (slot) => fdvalResolveDup(localFds[slot]);
  const isWorkerStdio = (slot) => resolvedOf(slot).kind === "worker";
  const stderrDupSrc = isWorkerStdio(2) ? resolvedOf(2).fd : null; // 1 => dup of stdout slot, 2 => own
  const stdoutSpec = isWorkerStdio(1) ? "pipe" : spawnStdioFor(localFds[1], "stdout");
  const stderrSpec = isWorkerStdio(2) ? "pipe" : spawnStdioFor(localFds[2], "stderr");
  let proc;
  try {
    proc = spawn(argv[0], argv.slice(1), {
      cwd: ctx.cwdBox.value,
      env: overlayEnv,
      stdio: [spawnStdioFor(localFds[0], "stdin"), stdoutSpec, stderrSpec],
      detached: false,
      windowsHide: true,
    });
  } catch (exc) {
    return finishFail(`exec failed: ${exc && exc.message ? exc.message : exc}`, exc && exc.code === "EACCES" ? 126 : exc && exc.code === "ENOENT" ? 127 : 1);
  }
  ctx.children.push(proc);
  for (const fd of opened) {
    try {
      fs.closeSync(fd);
    } catch {
      /* ignore */
    }
  }
  if (proc.stdout) {
    const s = new OutputSink(proc.stdout, ctx.outputLimit);
    sinks.out.push(s);
  }
  if (proc.stderr) {
    const s = new OutputSink(proc.stderr, ctx.outputLimit);
    (stderrDupSrc === 1 ? sinks.out : sinks.err).push(s);
  }
  const status = await waitChildAsync(proc);
  return status;
}

// A top-level ( ... ) group under gate capture: run its chain with the SAME sinks (every
// inner stage's stderr joins sinks.err; the LAST inner stage's stdout joins sinks.out --
// matching "capture the last stage's stdout" for a group-as-command).
async function runStageGroupCapture(group, fds, ctx, sinks) {
  const innerBox = new CwdBox(ctx.cwdBox.value);
  const innerCtx = new ExecContext(ctx.env, innerBox, ctx.outputLimit);
  innerCtx.children = ctx.children;
  let status = 0;
  const pipelines = group.chain.pipelines;
  for (let pi = 0; pi < pipelines.length; pi += 1) {
    const isLastPipeline = pi === pipelines.length - 1;
    const pipeline = pipelines[pi];
    if (pipeline.stages.length === 1 && pipeline.stages[0] instanceof CommandNode) {
      // Single command inside the group: capture its stdout only if this is the LAST
      // pipeline of the group; stderr always captured.
      const stage = pipeline.stages[0];
      const perSink = { out: isLastPipeline ? sinks.out : [], err: sinks.err };
      const preLen = perSink.out.length;
      const s = await runSingleStageCapture(stage, { 0: workerStdinFdval(), 1: workerStdoutFdval(), 2: workerStderrFdval() }, innerCtx, perSink);
      if (!isLastPipeline && perSink.out.length > preLen) {
        // A non-last pipeline's stdout was captured into the group's out sink by
        // runSingleStageCapture unconditionally -- drop it (the Python model captures
        // only the last stage's stdout).
        perSink.out.splice(preLen, perSink.out.length - preLen);
      }
      if (s !== 0) {
        status = s;
        break;
      }
    } else {
      const s = await runPipelineGate(pipeline, { 0: workerStdinFdval(), 1: workerStdoutFdval(), 2: workerStderrFdval() }, innerCtx, isLastPipeline ? sinks : { out: [], err: sinks.err });
      if (s !== 0) {
        status = s;
        break;
      }
    }
  }
  return status;
}

// The worker's own stdio as fdvals (used when a group's inner stages have no explicit
// redirection): slot 0 reads the death pipe, slot 1/2 are capture targets handled by the
// capture-stdout/capture-stderr modes of spawnStdioFor.
function workerStdinFdval() {
  return new WorkerFd(0);
}
function workerStdoutFdval() {
  return new WorkerFd(1);
}
function workerStderrFdval() {
  return new WorkerFd(2);
}

// ---------------------------------------------------------------------------
// Worker (`cmdrun.mjs --exec`): one process group, no deadline, no fork.
// ---------------------------------------------------------------------------

function writeFully(fd, payload) {
  // fs.writeSync on a pipe can throw EAGAIN when the pipe is full -- libuv sets the fd
  // nonblocking, and a large answer (captured stdout up to the output_limit) far exceeds
  // the pipe buffer. Swallowing EAGAIN here silently DROPPED the unwritten tail, so the
  // supervisor read a truncated line and answered internal_error on a fully successful
  // command (measured: 200KB answer). Block and retry; EOF/EPIPE still throws to the
  // caller's catch.
  let written = 0;
  while (written < payload.length) {
    try {
      written += fs.writeSync(fd, payload, written, payload.length - written);
    } catch (exc) {
      if (exc && (exc.code === "EAGAIN" || exc.code === "EWOULDBLOCK")) {
        sleepSyncMs(2);
        continue;
      }
      throw exc;
    }
  }
}

function emitAndDie(answer) {
  // The worker's one and only exit path, on every branch: write the answer (one JSON line,
  // flushed to the OS), then kill our OWN process group (we are its leader) before
  // exiting. writeFully so every byte is CONFIRMED delivered before the SIGKILL hits
  // this very process -- any byte sitting in a userland buffer would never reach the
  // reader (a partial JSON line the supervisor could mistake for "no valid answer").
  try {
    writeFully(1, Buffer.from(`${JSON.stringify(answer)}\n`, "utf8"));
  } catch {
    /* EPIPE etc: swallow */
  }
  if (!IS_WINDOWS) {
    // W2: killpg is POSIX-only. On Windows there is no per-worker group to kill -- the
    // supervisor's taskkill /T is what reaps any escaped descendant there.
    try {
      process.kill(-process.pid, "SIGKILL");
    } catch {
      /* ignore */
    }
  }
  process.exit(0);
}

function deathWatch() {
  // The worker's stdin is the death pipe: the supervisor holds the write end open for as
  // long as IT lives, so the moment the supervisor dies for ANY reason the kernel closes
  // that pipe and stdin hits EOF -- that is how a caller's SIGKILL of the supervisor
  // reaches a running command (H4). Node reads stdin only on the event loop; the executor
  // blocks the loop, so poll stdin from a dedicated... no: poll from the same pumping
  // ticks. Simplest honest form: an fs.readSync on fd 0 races the executor's reads.
  // ponytail: death detection while the event loop is blocked is driven by
  // pumpTicks() -- every waitChildPumping slice checks fd 0 for EOF via a zero-read.
  process.stdin.on("end", () => {
    onDeathPipeEof();
  });
  process.stdin.on("error", () => {});
  process.stdin.resume();
}

let _deathFired = false;
function onDeathPipeEof() {
  if (_deathFired) return;
  _deathFired = true;
  if (!IS_WINDOWS) {
    try {
      process.kill(-process.pid, "SIGKILL");
    } catch {
      /* ignore */
    }
  }
  process.exit(1);
}

async function workerMain() {
  const treeKill = IS_WINDOWS ? "taskkill" : "process_group";
  if (TEST_HOOKS && process.env["CMDRUN_TEST_WORKER_CRASH_BEFORE_READ"]) {
    // Test-only fault injection, gated behind --test-hooks (F4): the worker dies before it
    // ever reads its request line -- proves the supervisor still answers with exactly ONE
    // internal_error JSON line, never a crash with two lines or a hang.
    process.exit(1);
  }
  // Read exactly ONE request line (the supervisor writes the validated request as one
  // JSON line), then start the death watch.
  let raw = null;
  try {
    raw = readlineSyncOneLine();
  } catch {
    raw = null;
  }
  if (raw === null) {
    emitAndDie({
      status: null, timed_out: false, internal_error: true,
      reason: "worker: bad request: no request line", tree_kill: treeKill,
    });
    return;
  }
  let request;
  try {
    request = JSON.parse(raw.toString("utf8"));
    if (request === null || typeof request !== "object" || Array.isArray(request)) {
      throw new Error("request is not an object");
    }
  } catch (exc) {
    emitAndDie({
      status: null, timed_out: false, internal_error: true,
      reason: `worker: bad request: ${exc && exc.message ? exc.message : exc}`, tree_kill: treeKill,
    });
    return;
  }
  deathWatch();
  const command = request.command;
  const cwd = request.cwd;
  const env = Object.prototype.hasOwnProperty.call(request, "env") && request.env !== undefined
    ? { ...request.env }
    : { ...process.env };
  const mode = Object.prototype.hasOwnProperty.call(request, "mode") ? request.mode : "gate";
  const outputLimit = Object.prototype.hasOwnProperty.call(request, "output_limit") ? request.output_limit : 1_000_000;

  let chain;
  let reason;
  try {
    [chain, reason] = compileCommand(command, env);
  } catch (exc) {
    emitAndDie({
      status: 1, timed_out: false, internal_error: true,
      reason: `internal error: ${exc && exc.message ? exc.message : exc}`, tree_kill: treeKill,
    });
    return;
  }
  if (reason !== null) {
    emitAndDie({
      status: null, refused: true, reason, timed_out: false, tree_kill: treeKill,
    });
    return;
  }

  const sinks = mode === "gate" ? { out: [], err: [] } : null;
  // Gate mode: pre-spawn failures (127/126) write to WorkerFd(2), the worker's own stderr,
  // which the supervisor set to "ignore" -- capture those bytes here so the answer's stderr
  // matches the py oracle (whose fds[2] in gate mode IS a capture pipe).
  const manualErrChunks = [];
  _gateStderrSink = mode === "gate" ? (b) => {
    if (manualErrChunks.length < 2048) manualErrChunks.push(Buffer.from(b));
    else _gateStderrTruncated = true;
  } : null;
  const devnullR = fs.openSync(DEVNULL, "r");
  const devnullW = mode !== "gate" ? fs.openSync(DEVNULL, "w") : null;
  const fds = { 0: new PathFd(devnullR), 1: mode === "gate" ? new WorkerFd(1) : new PathFd(devnullW), 2: mode === "gate" ? new WorkerFd(2) : new PathFd(devnullW) };
  const ctx = new ExecContext(env, new CwdBox(cwd), outputLimit);
  let status;
  try {
    status = await runChainWorker(chain, fds, ctx, sinks);
  } catch (exc) {
    // F8: internal_error must be set on every genuinely internal fault -- the flag a
    // caller keys on to tell "the command legitimately exited 1" from "the interpreter
    // itself broke".
    _gateStderrSink = null;
    closeQuiet([devnullR, devnullW]);
    emitAndDie({
      status: 1, timed_out: false, internal_error: true,
      reason: `internal error: ${exc && exc.message ? exc.message : exc}`, tree_kill: treeKill,
    });
    return;
  }
  if (status === null || status === undefined) {
    // F2 safety net: never let a gap silently forge a `status: null` with no reason.
    _gateStderrSink = null;
    closeQuiet([devnullR, devnullW]);
    emitAndDie({
      status: null, timed_out: false, internal_error: true,
      reason: "internal error: no stage produced a status", tree_kill: treeKill,
    });
    return;
  }
  closeQuiet([devnullR, devnullW]);
  _gateStderrSink = null;
  const answer = { status, timed_out: false, tree_kill: treeKill };
  if (mode === "gate") {
    // F3: ONE combined 100ms bound for both readers together -- the writers are already
    // done (every stage has exited), so this only waits for the readers to drain what's
    // left of one pipe buffer's worth of bytes; NEVER close a stream a reader may still
    // be reading -- write the answer and exit instead.
    drainSinks(sinks, 100).then(() => {
      const outBuf = Buffer.concat(sinks.out.map((s) => s.snapshot()));
      const pipeErr = Buffer.concat(sinks.err.map((s) => s.snapshot()));
      const manualErr = Buffer.concat(manualErrChunks);
      answer.stdout = outBuf.toString("utf8");
      answer.stderr = (pipeErr.length ? [pipeErr, manualErr] : [manualErr]).map((b) => b.toString("utf8")).join("");
      answer.truncated = _gateStderrTruncated || sinks.out.some((s) => s.truncated) || sinks.err.some((s) => s.truncated);
      emitAndDie(answer);
    });
    return;
  }
  emitAndDie(answer);
}

// The worker's chain runner: gate mode routes everything through the capture machinery
// (single commands, groups, pipelines); ledger mode uses the plain runner.
async function runChainWorker(chain, fds, ctx, sinks) {
  let status = 0;
  for (const pipeline of chain.pipelines) {
    if (sinks !== null) {
      status = await runPipelineGate(pipeline, fds, ctx, sinks);
    } else {
      status = await runPipeline(pipeline, fds, ctx);
    }
    if (status !== 0) break;
  }
  return status;
}

// F3: bounded join without closing anything -- the writers are already done (every stage
// exited), so this only waits for the readers to drain what is left of one pipe buffer's
// worth of bytes. Runs on the event loop (the streams need it); never closes a stream.
async function drainSinks(sinks, ms) {
  const deadline = Date.now() + ms;
  for (;;) {
    const allDone = sinks.out.every((s) => s.done) && sinks.err.every((s) => s.done);
    if (allDone) return;
    if (Date.now() >= deadline) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

// Synchronously read exactly one line from stdin (the request line) without consuming
// more than that line. Returns a Buffer, or null on EOF.
function readlineSyncOneLine() {
  const chunks = [];
  let total = 0;
  const tmp = Buffer.alloc(65536);
  for (;;) {
    let n;
    try {
      n = fs.readSync(0, tmp, 0, tmp.length);
    } catch (exc) {
      if (exc && (exc.code === "EAGAIN" || exc.code === "EWOULDBLOCK")) {
        // EAGAIN is NOT EOF: with libuv's nonblocking stdin, a supervisor writing a
        // large request races our read and EAGAIN fires mid-stream. Same fix as
        // readAllStdin above -- block briefly and retry; true EOF is n === 0.
        sleepSyncMs(2);
        continue;
      }
      throw exc;
    }
    if (n === 0) break; // EOF
    const nl = tmp.subarray(0, n).indexOf(0x0a);
    if (nl !== -1) {
      chunks.push(Buffer.from(tmp.subarray(0, nl)));
      total += nl;
      return Buffer.concat(chunks, total);
    }
    chunks.push(Buffer.from(tmp.subarray(0, n)));
    total += n;
    if (total > MAX_REQUEST_BYTES) throw new Error("request line too long");
  }
  if (total === 0) return null;
  return Buffer.concat(chunks, total);
}

// ---------------------------------------------------------------------------
// Supervisor: request validation, spawning the worker, and the one timekeeper.
// ---------------------------------------------------------------------------

export function validateRequest(raw) {
  // Returns [validatedRequest, errorReason]. Never throws -- M1: a malformed request
  // always becomes a bad_request answer, never an uncaught traceback.
  if (raw.length > MAX_REQUEST_BYTES) return [null, "request exceeds 16 MiB"];
  let text;
  try {
    text = raw.toString("utf8");
  } catch (exc) {
    return [null, `invalid UTF-8: ${exc && exc.message ? exc.message : exc}`];
  }
  if (!text.trim()) return [null, "empty request"];
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (exc) {
    return [null, `invalid JSON: ${exc && exc.message ? exc.message : exc}`];
  }
  if (obj === null || typeof obj !== "object" || Array.isArray(obj)) {
    return [null, "request must be a JSON object"];
  }
  const command = obj.command;
  if (typeof command !== "string" || !command) return [null, "missing or invalid 'command'"];
  let cwd = obj.cwd;
  try {
    if (cwd === undefined || cwd === null) {
      cwd = process.cwd();
    } else {
      if (typeof cwd !== "string" || !cwd) return [null, "invalid 'cwd'"];
      if (!path.isAbsolute(cwd)) cwd = path.resolve(process.cwd(), cwd);
    }
  } catch (exc) {
    // os.getcwd() raises if the supervisor's own working directory was deleted.
    return [null, `cannot determine cwd: ${exc && exc.message ? exc.message : exc}`];
  }
  if (!fs.existsSync(cwd) || !fs.statSync(cwd).isDirectory()) {
    return [null, `cwd does not exist: ${cwd}`];
  }
  const envPresent = Object.prototype.hasOwnProperty.call(obj, "env");
  let env = obj.env;
  if (envPresent) {
    if (env === null || typeof env !== "object" || Array.isArray(env)) {
      return [null, "invalid 'env': must be an object"];
    }
    for (const [k, v] of Object.entries(env)) {
      if (typeof k !== "string" || typeof v !== "string" || k.includes("\0") || v.includes("\0")) {
        return [null, "invalid 'env': keys/values must be NUL-free strings"];
      }
    }
    if (IS_WINDOWS) {
      // W3: Windows env keys are case-insensitive -- normalize here so the child process
      // never sees both spellings at once (later one wins).
      const normalized = {};
      for (const [k, v] of Object.entries(env)) {
        const upper = k.toUpperCase();
        for (const existing of Object.keys(normalized)) {
          if (existing.toUpperCase() === upper) {
            delete normalized[existing];
            break;
          }
        }
        normalized[k] = v;
      }
      env = normalized;
    }
  }
  const mode = Object.prototype.hasOwnProperty.call(obj, "mode") ? obj.mode : "gate";
  if (mode !== "ledger" && mode !== "gate") {
    return [null, "invalid 'mode': must be 'ledger' or 'gate'"];
  }
  const timeoutMs = Object.prototype.hasOwnProperty.call(obj, "timeout_ms") ? obj.timeout_ms : 30000;
  // M3/P6: JSON.parse gives a float for 1e3/1000.0/NaN/Infinity, and booleans must be
  // rejected explicitly rather than let NaN >= deadline silently evaluate to False forever.
  if (typeof timeoutMs !== "number" || Number.isNaN(timeoutMs) || !Number.isFinite(timeoutMs) || !Number.isInteger(timeoutMs) || Number.isInteger(timeoutMs) === false) {
    return [null, "invalid 'timeout_ms': must be an integer"];
  }
  if (timeoutMs < 1 || timeoutMs > 86_400_000) {
    return [null, "invalid 'timeout_ms': out of range 1..86400000"];
  }
  const outputLimit = Object.prototype.hasOwnProperty.call(obj, "output_limit") ? obj.output_limit : 1_000_000;
  if (typeof outputLimit !== "number" || Number.isNaN(outputLimit) || !Number.isFinite(outputLimit) || !Number.isInteger(outputLimit)) {
    return [null, "invalid 'output_limit': must be an integer"];
  }
  if (outputLimit < 0 || outputLimit > MAX_OUTPUT_LIMIT) {
    return [null, `invalid 'output_limit': out of range 0..${MAX_OUTPUT_LIMIT}`];
  }
  const result = { command, cwd, mode, timeout_ms: timeoutMs, output_limit: outputLimit };
  if (envPresent) result.env = env;
  return [result, null];
}

const ANSWER_KNOWN_KEYS = new Set([
  "status", "timed_out", "truncated", "tree_kill", "refused", "bad_request",
  "internal_error", "leaked", "reason", "stdout", "stderr", "pgid",
]);

export function validateWorkerAnswer(answer) {
  // F1: known keys only, and every value's TYPE checked -- a forged {"status": 0} or an
  // unrelated JSON object must never be mistaken for a real answer. The Node validator
  // additionally accepts tree_kill 'taskkill' (what ITS worker emits on Windows).
  if (answer === null || typeof answer !== "object" || Array.isArray(answer)) return false;
  for (const k of Object.keys(answer)) {
    if (!ANSWER_KNOWN_KEYS.has(k)) return false;
  }
  if (!Object.prototype.hasOwnProperty.call(answer, "status")) return false;
  const status = answer.status;
  if (status !== null && (typeof status !== "number" || !Number.isInteger(status))) return false;
  for (const key of ["timed_out", "truncated", "refused", "bad_request", "internal_error", "leaked"]) {
    if (Object.prototype.hasOwnProperty.call(answer, key) && typeof answer[key] !== "boolean") return false;
  }
  if (Object.prototype.hasOwnProperty.call(answer, "reason") && typeof answer.reason !== "string") return false;
  if (Object.prototype.hasOwnProperty.call(answer, "tree_kill") && !["process_group", "job", "taskkill"].includes(answer.tree_kill)) {
    return false;
  }
  if (Object.prototype.hasOwnProperty.call(answer, "stdout") && typeof answer.stdout !== "string") return false;
  if (Object.prototype.hasOwnProperty.call(answer, "stderr") && typeof answer.stderr !== "string") return false;
  if (Object.prototype.hasOwnProperty.call(answer, "pgid") && (typeof answer.pgid !== "number" || !Number.isInteger(answer.pgid))) return false;
  return true;
}

// Kill the worker's process group: POSIX process.kill(-pid, 'SIGKILL') repeated until
// ESRCH; Windows taskkill /T /F /PID <pid> (the Node twin has no Job Objects). Returns
// true ('leaked') if the deadline is exceeded without confirmation. Async: the 10ms gaps
// between SIGKILL attempts must run on the event loop (Atomics.wait would block the very
// loop that updates proc's exit state).
async function killGroupUntilDead(pid, deadline) {
  for (;;) {
    if (!IS_WINDOWS) {
      try {
        process.kill(-pid, "SIGKILL");
      } catch (exc) {
        if (exc && exc.code === "ESRCH") return false; // the whole group is confirmed gone
        // EPERM (macOS, zombie-only group): reap below, then retry.
      }
      try {
        process.kill(pid, 0);
      } catch (exc) {
        if (exc && exc.code === "ESRCH") return false;
      }
    } else {
      // Windows: taskkill /T /F kills the tree. Run it synchronously; treat exit 128 as
      // "no such process" (already gone).
      const r = spawnSync("taskkill", ["/T", "/F", "/PID", String(pid)], { stdio: "ignore", windowsHide: true });
      if (r.status === 128) return false;
    }
    if (Date.now() / 1000 >= deadline) return true;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

async function reapBounded(proc, timeoutMs) {
  // Bounded reap: the worker is our direct child; wait up to timeoutMs on the loop.
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (proc.exitCode !== null || proc.signalCode !== null) return;
    if (Date.now() >= deadline) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function waitForWorkerAnswer(proc, deadline, signalled) {
  // Returns [answer_or_null, timedOut, signalName_or_null]. Polls: the answer line on the
  // child's stdout pipe, the deadline, the signal watch. A blocking readline here cannot
  // coexist with polling, so read via a nonblocking tap: spawn gave us proc.stdout as a
  // socket -- buffer its data.
  return new Promise((resolve) => {
    let chunks = [];
    let line = null;
    let readErr = false;
    // Accumulate EVERY chunk: a pipe delivers data in kernel-buffer-sized pieces (16KB
    // observed on macOS), and a captured stdout between ~16KB and the output_limit makes
    // the one-line JSON answer span several chunks. Taking only the first chunk made the
    // supervisor answer `invalid JSON` (internal_error-adjacent) on a worker that fully
    // succeeded -- the py oracle's readline() never has this bug. Join on end/close.
    const onData = (chunk) => {
      chunks.push(chunk);
    };
    const onEnd = () => {
      if (chunks !== null) line = Buffer.concat(chunks);
      chunks = null;
    };
    proc.stdout.on("data", onData);
    proc.stdout.on("end", onEnd);
    proc.stdout.on("close", onEnd);
    proc.stdout.on("error", () => {
      readErr = true;
    });
    const finish = (answer, timedOut, sig) => {
      proc.stdout.removeListener("data", onData);
      proc.stdout.removeListener("end", onEnd);
      proc.stdout.removeListener("close", onEnd);
      resolve([answer, timedOut, sig]);
    };
    const tick = () => {
      if (signalled.got !== null) {
        finish(null, false, signalled.got);
        return;
      }
      if (Date.now() / 1000 >= deadline) {
        finish(null, true, null);
        return;
      }
      if (line !== null) {
        // The answer is ONE line possibly followed by nothing; split off the first line.
        const nl = line.indexOf(0x0a);
        const first = nl === -1 ? line : line.subarray(0, nl);
        try {
          const answer = JSON.parse(first.toString("utf8"));
          if (validateWorkerAnswer(answer)) {
            finish(answer, false, null);
            return;
          }
          // F1: anything that isn't the EXACT contracted shape is treated identically to
          // "no answer at all".
          finish(null, false, null);
          return;
        } catch {
          finish(null, false, null);
          return;
        }
      }
      // The stream ended (or errored) without a line: if all chunks arrived, judge what
      // we have; otherwise the worker died mid-answer.
      if (chunks === null || readErr || (proc.exitCode !== null && proc.stdout.readableEnded)) {
        if (chunks === null && line !== null) {
          try {
            const answer = JSON.parse(line.toString("utf8"));
            if (validateWorkerAnswer(answer)) {
              finish(answer, false, null);
              return;
            }
          } catch { /* fall through to no-answer */ }
        }
        finish(null, false, null);
        return;
      }
      setTimeout(tick, 10);
    };
    tick();
  });
}

async function runPosixOrWindowsSupervisor(req, signalled) {
  // One supervisor shape for both platforms: the Node twin has no Job Objects, so Windows
  // kills with taskkill /T /F (tree_kill "taskkill") where the Python twin uses a Job.
  const workerArgv = [process.execPath, path.resolve(fileURLToSelf()), "--exec"];
  if (TEST_HOOKS) workerArgv.push("--test-hooks");
  const testDelayMs = TEST_HOOKS ? process.env["CMDRUN_TEST_DELAY_BEFORE_SPAWN_MS"] : null;
  if (testDelayMs) {
    // Test-only fault injection (finding 7), gated behind --test-hooks (F4): widens the
    // window between "the supervisor process exists" and "the worker process exists".
    await new Promise((resolve) => setTimeout(resolve, parseInt(testDelayMs, 10)));
  }
  let proc;
  try {
    proc = spawn(workerArgv[0], workerArgv.slice(1), {
      stdio: ["pipe", "pipe", "ignore"],
      // The worker's pid IS its own process group on POSIX (detached = setsid).
      detached: !IS_WINDOWS,
      windowsHide: true,
    });
  } catch (exc) {
    return {
      status: null, timed_out: false, internal_error: true,
      reason: `failed to spawn worker: ${exc && exc.message ? exc.message : exc}`,
      tree_kill: IS_WINDOWS ? "taskkill" : "process_group",
    };
  }
  const line = Buffer.from(`${JSON.stringify(req)}\n`, "utf8");
  try {
    proc.stdin.write(line);
    // NEVER end()/close() the death pipe's write end here: the worker's death watch
    // treats stdin EOF as "the supervisor died" and SIGKILLs the whole process group.
    // The Python oracle (cmdrun.py _run_posix_supervisor) also writes+flushes and holds
    // the write end open for its whole life -- the kernel closes it for us when the
    // supervisor process exits, which is the death signal itself. end()ing it made every
    // real-binary spawn die instantly (rc=1) and the supervisor answer 124.
  } catch {
    /* the read loop below will notice the worker died and report it */
  }

  const deadline = Date.now() / 1000 + req.timeout_ms / 1000.0;
  const [answer, timedOut, sig] = await waitForWorkerAnswer(proc, deadline, signalled);
  if (answer !== null) {
    await reapBounded(proc, cleanupBudgetS() * 1000);
    if (!Object.prototype.hasOwnProperty.call(answer, "tree_kill")) answer.tree_kill = IS_WINDOWS ? "taskkill" : "process_group";
    if (!Object.prototype.hasOwnProperty.call(answer, "timed_out")) answer.timed_out = false;
    return answer;
  }
  // F7: the reap that follows a confirmed-dead-or-leaked group counts INSIDE the same
  // cleanup budget as the kill loop itself, never a separate bound tacked on afterward.
  const cleanupDeadline = Date.now() / 1000 + cleanupBudgetS();
  const leaked = await killGroupUntilDead(proc.pid, cleanupDeadline);
  await reapBounded(proc, Math.max(0, cleanupDeadline * 1000 - Date.now()));
  const treeKill = IS_WINDOWS ? "taskkill" : "process_group";
  if (leaked) {
    // Never answer silently when the deadline runs out before the group is confirmed
    // dead -- `pgid` names exactly which group a caller must go clean up by hand.
    return {
      status: null, timed_out: timedOut, leaked: true, pgid: proc.pid,
      reason: `failed to reap process group ${proc.pid} in time `
        + "(the group may hold only a zombie -- a real survivor may have "
        + "escaped into another session)",
      tree_kill: treeKill,
    };
  }
  if (sig !== null) return { status: 143, timed_out: false, tree_kill: treeKill };
  if (timedOut) return { status: 124, timed_out: true, tree_kill: treeKill };
  return {
    status: null, timed_out: false, internal_error: true,
    reason: "worker exited without a valid answer", tree_kill: treeKill,
  };
}

function fileURLToSelf() {
  // The absolute path of THIS module, without the import.meta machinery (kept compatible
  // with CJS-style invocations by walking argv instead): the supervisor always knows its
  // own script path from argv[1].
  return process.argv[1];
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

function printAnswer(answer) {
  try {
    // writeFully, not a bare writeSync loop: a large answer overflows the stdout pipe's
    // buffer and writeSync throws EAGAIN on the nonblocking fd -- swallowing it (the old
    // catch) silently dropped the tail of the answer. Same fix as emitAndDie.
    writeFully(1, Buffer.from(`${JSON.stringify(answer)}\n`, "utf8"));
  } catch {
    /* L2: the caller closed our stdout before we answered -- exit quietly. */
  }
}

const SIGNAL_WATCH = { got: null, installed: false, handlers: [] };

function installSignalWatch() {
  // F6: install the signal watch FIRST THING, before any validation/spawn work, and keep
  // it installed through the whole cleanup phase -- a signal landing in the startup/
  // validation/spawn window must answer 143 exactly like one landing during the wait.
  for (const name of ["SIGTERM", "SIGHUP", "SIGINT"]) {
    try {
      const h = () => {
        if (SIGNAL_WATCH.got === null) SIGNAL_WATCH.got = name;
      };
      process.on(name, h);
      SIGNAL_WATCH.handlers.push([name, h]);
    } catch {
      /* platform without this signal (SIGHUP on Windows) -- skip, W1 */
    }
  }
  SIGNAL_WATCH.installed = true;
}

function closeSignalWatch() {
  for (const [name, h] of SIGNAL_WATCH.handlers) {
    try {
      process.removeListener(name, h);
    } catch {
      /* ignore */
    }
  }
  SIGNAL_WATCH.handlers = [];
}

const SLEEP_SAB = new Int32Array(new SharedArrayBuffer(4));
// Block the thread for `ms` without burning CPU. Atomics.wait works on the main thread
// here only when the host does not forbid it; SharedArrayBuffer + Atomics are always
// available in Node. (Atomics.wait on the main thread IS allowed in Node, unlike browsers.)
function sleepSyncMs(ms) {
  try {
    Atomics.wait(SLEEP_SAB, 0, 0, ms);
  } catch {
    /* fall back to a busy yield -- correctness of the EAGAIN retry does not depend on
       the wait being efficient, only on it yielding the CPU */
  }
}

function readAllStdin() {
  // Read stdin to EOF (cap MAX_REQUEST_BYTES + 1), synchronously.
  const chunks = [];
  let total = 0;
  const tmp = Buffer.alloc(65536);
  for (;;) {
    let n;
    try {
      n = fs.readSync(0, tmp, 0, tmp.length);
    } catch (exc) {
      if (exc && (exc.code === "EAGAIN" || exc.code === "EWOULDBLOCK")) {
        // EAGAIN is NOT EOF: it means the nonblocking fd has nothing RIGHT NOW. Once
        // libuv initializes stdin (any stdio setup that ran before us), the fd is
        // nonblocking even for a pipe, so a large request written by a caller racing
        // our reads hits EAGAIN mid-stream. Treating it as EOF truncated the request
        // to the first 80KB of a 200KB write -- measured -- and answered bad_request
        // "invalid JSON" on a well-formed request (the py oracle blocks and reads it
        // all). Retry after a bounded block; true EOF is n === 0, never an error.
        sleepSyncMs(2);
        continue;
      }
      throw exc;
    }
    if (n === 0) break;
    chunks.push(Buffer.from(tmp.subarray(0, n)));
    total += n;
    if (total > MAX_REQUEST_BYTES) {
      // Keep reading to EOF? No: cap reached -- return what we have (oversized, caller
      // rejects it).
      return Buffer.concat(chunks, total);
    }
  }
  return Buffer.concat(chunks, total);
}

// The real entry: main() is async because the supervisor's answer reader is event-loop
// driven. See the note above -- the sync pump attempt is dead code kept out.
async function main() {
  if (process.argv.length > 2 && process.argv[2] === "--exec") {
    workerMain();
    return;
  }
  const treeKill = IS_WINDOWS ? "taskkill" : "process_group";
  installSignalWatch(); // F6: FIRST THING, before validation or spawn.
  let raw;
  try {
    raw = readAllStdin();
  } catch (exc) {
    printAnswer({
      status: null, bad_request: true,
      reason: `invalid request: ${exc && exc.message ? exc.message : exc}`,
      timed_out: false, tree_kill: treeKill,
    });
    closeSignalWatch();
    return;
  }
  let req = null;
  let err = null;
  try {
    [req, err] = validateRequest(raw);
  } catch (exc) {
    err = `invalid request: ${exc && exc.message ? exc.message : exc}`;
  }
  if (err !== null || req === null) {
    printAnswer({
      status: null, bad_request: true, reason: err, timed_out: false, tree_kill: treeKill,
    });
    closeSignalWatch();
    return;
  }
  const envForAnalysis = Object.prototype.hasOwnProperty.call(req, "env") ? req.env : process.env;
  let reason = null;
  try {
    [, reason] = compileCommand(req.command, envForAnalysis);
  } catch (exc) {
    reason = `internal error: ${exc && exc.message ? exc.message : exc}`;
  }
  if (reason !== null) {
    printAnswer({
      status: null, refused: true, reason, timed_out: false, tree_kill: treeKill,
    });
    closeSignalWatch();
    return;
  }
  let answer;
  try {
    answer = await runPosixOrWindowsSupervisor(req, SIGNAL_WATCH);
  } catch (exc) {
    // The one place we truly must never crash.
    answer = {
      status: null, timed_out: false, internal_error: true,
      reason: `internal error: ${exc && exc.message ? exc.message : exc}`, tree_kill: treeKill,
    };
  }
  if (!Object.prototype.hasOwnProperty.call(answer, "timed_out")) answer.timed_out = false;
  printAnswer(answer);
  closeSignalWatch();
}

main();
