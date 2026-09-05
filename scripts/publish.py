#!/usr/bin/env python3
"""Unified publish pipeline: bypass-guard -> lint -> validate (remote CPV) -> test -> bump -> badge -> changelog -> commit -> push -> release.

Modes:
  --gate                  Quality gate, no bump/push. Runs, in order: ruff (G2),
                          jscpd (G2b), actionlint (G2c), mypy (G2d), the compiled
                          build gates (G2e), shellcheck (G2f), the remote CPV
                          validate (G3), the trufflehog secret scan (G3s), the
                          test suite (G4) and the Linux fork-parity probe (G4b).
                          Runs STANDALONE for a local pre-release check. When
                          invoked by the pre-push hook it ALSO enforces that the
                          push was started by this script (G0) and that the
                          version was bumped (G1) — those two protect a push, so
                          they only apply while one is in flight (issue cpv#192).
  --print-gates           Print the numbered pipeline stage list and exit 0.
                          Pure information: no side effects, no repo lookup.
  --install-hook          Install git-hooks/pre-push into .git/hooks/ and set core.hooksPath.
  --install-branch-rules  Apply the cpv-branch-rules GitHub ruleset to the origin
                          (server-side CI enforcement — run once after first push).
  (no flag)               Full release pipeline (15 stages, fail-fast). The bump type
                          is AUTO-DETECTED via `git-cliff --bumped-version` from the
                          conventional commits on HEAD.
  --patch/--minor/--major Force a specific bump type (overrides auto-detection).

Pipeline stages (all fail-fast — any non-zero exit aborts). The list below,
`_PIPELINE_STAGES`, the `[N/M]` progress labels and `--print-gates` are ONE
source of truth — the count is derived, never retyped (audit row 31):
   1. Bypass guard — reject CPV_SKIP_*, SKIP_*, NO_VERIFY env vars
   2. Check working tree is clean
   3. Lint + type-check (ruff + mypy)
   4. Run tests (pytest)
   5. Validate plugin (uvx cpv-remote-validate plugin . --strict — fetches
      the canonical CPV validator from GitHub so this plugin never vendors
      a local copy and never drifts from upstream rules)
   6. Secret scan (trufflehog). A pipeline stage as well as a gate stage: a
      plugin whose hooks were never installed would otherwise publish having
      scanned nothing.
   7. Linux fork-parity probe. Same reason — with hooks uninstalled the gate
      copy never runs.
   8. CI-parity preflight (uvx cpv-remote-validate ci-preflight . — the
      jscpd / actionlint / mypy / uv-sync-dev / Mega-Linter / static-CIP gates
      that CI's Lint job runs but `validate_plugin --strict` does NOT). Runs
      BEFORE the bump/commit/tag/push, so a pipeline defect can never leave a
      half-published state. A MISSING local tool degrades to a WARNING and never
      blocks the publish.
   9. Marketplace-registration check (Layout A: notify workflow + PAT secret +
      remote marketplace.json registration + remote receiver workflow;
      Layout B: must run from marketplace root + nested plugin must be listed)
  10. Check version consistency across all sources
  11. Bump version in plugin.json, pyproject.toml, and __version__ vars
  12. Update README version badge
  13. Generate changelog (git-cliff)
  14. Commit, tag, push
  15. Create GitHub release (gh CLI)

Post-release stages (deliberately UNNUMBERED — they run after the release is
public and can never abort it): verify CI is green, prove the release installs.

Gate stages (--gate mode, called by pre-push hook):
   G0. Orchestrator check — direct `git push` is blocked; only publish.py
       may initiate a push (verified via process ancestry, NOT env vars).
   G1. Version bump check (local vs remote, auto-detects origin/HEAD)
   G2. Lint (ruff)
   G2b. Copy-paste check (jscpd, parity with ci.yml Mega-Linter COPYPASTE_JSCPD;
        WARNs+skips if jscpd/npx unavailable so a push is never false-blocked)
   G2c. Workflow lint (actionlint, parity with ci.yml Lint job; WARNs+skips if
        actionlint unavailable so a push is never false-blocked)
   G2d. Type-check (mypy scripts/ --ignore-missing-imports, parity with ci.yml
        Lint job; WARNs+skips if mypy unavailable so a push is never false-blocked)
   G2e. Compiled-component build gates (cargo clippy+test / go vet+build+test /
        dotnet build / swift build / zig build), each self-detecting; WARNs+skips
        if the toolchain is unavailable so a push is never false-blocked. C/C++ is
        detected + noted (built in CI; no false-block-safe local command) (issue #175)
   G2f. Shell lint (shellcheck, parity with ci.yml Mega-Linter BASH_SHELLCHECK;
        WARNs+skips if shellcheck unavailable so a push is never false-blocked)
   G3. Validate (uvx cpv-remote-validate plugin . --strict)
   G3s. Secret scan (trufflehog; installed on demand — a FAILED install blocks,
        because "we never looked" is not "we looked and found nothing")
   G4. Tests (pytest)
   G4b. Linux fork-parity probe — re-runs the suite with multiprocessing forced
        to fork, the way Linux does it, so a fork-from-multithreaded-process
        deadlock cannot hide behind macOS's spawn default. Self-detecting (only
        when the plugin creates process pools); skips on Linux; WARNs where fork
        is unavailable

Usage:
    uv run python scripts/publish.py                      # auto-bump from git-cliff
    uv run python scripts/publish.py --gate
    uv run python scripts/publish.py --install-hook
    uv run python scripts/publish.py --install-branch-rules
    uv run python scripts/publish.py --patch              # force patch
    uv run python scripts/publish.py --minor              # force minor
    uv run python scripts/publish.py --major              # force major
    uv run python scripts/publish.py --dry-run            # preview (auto-bump)

Cornerstone rule: a plugin CANNOT be pushed unless validation passes with
0 issues (WARNING allowed). There are no exceptions and no bypass flags.
Every push is blocked unless scripts/publish.py orchestrates it end-to-end
AND stage_validate / stage_tests / stage_lint all succeed.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Load gh / git retry wrappers from the sibling module so every push +
# `gh release create` survives transient github.com hiccups (the retry
# pattern from ~/.claude/rules/github-timeouts.md). Shipped verbatim
# from the canonical CPV install via gen_cpv_network_resilience_py().
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    # `pyright: ignore[reportAssignmentType]` (on the import line itself):
    # the typed real import and the ImportError fallback shims below are
    # conditional variants of the same names; Pyright flags the typed import as
    # not assignable to the fallback's loose declared type (its mypy counterpart
    # is the [no-redef, misc] on the shims). Suppress exactly that — the
    # standard import-fallback idiom. issue #151.
    from cpv_network_resilience import gh_with_retry, git_with_retry  # pyright: ignore[reportAssignmentType]
except ImportError:
    # Fallback: scripts/cpv_network_resilience.py was not shipped with this
    # plugin (older scaffold). Define no-op shims so publish.py still works,
    # but warn so the user knows to refresh via `cpv standardize --force-templates`.
    print(
        "[publish.py] WARNING: scripts/cpv_network_resilience.py missing — "
        "network calls will not auto-retry on transient errors. "
        "Run `cpv standardize --force-templates` to refresh.",
        file=sys.stderr,
    )
    # `misc` is needed alongside `no-redef`: under `mypy --strict` the typed
    # real import (cpv_network_resilience) and these minimal fallback shims are
    # conditional variants of the same name with NON-IDENTICAL signatures, which
    # `--strict` reports as [misc] ("All conditional function variants must have
    # identical signatures"); the combined code suppresses exactly that, the
    # standard import-fallback idiom (cf. the tomli fallback at
    # cpv_lint_engine.py with [no-redef,import-not-found]).
    def gh_with_retry(cmd, **kwargs):  # type: ignore[no-redef, misc]
        kwargs.pop("max_attempts", None)
        kwargs.pop("backoff", None)
        kwargs.setdefault("check", True)
        kwargs.setdefault("capture_output", False)
        return subprocess.run(cmd, **kwargs)
    def git_with_retry(cmd, **kwargs):  # type: ignore[no-redef, misc]
        kwargs.pop("max_attempts", None)
        kwargs.pop("backoff", None)
        kwargs.setdefault("check", True)
        kwargs.setdefault("capture_output", False)
        return subprocess.run(cmd, **kwargs)

# -- ANSI colors ---------------------------------------------------------------


def _colors_ok() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_C = _colors_ok()
RED    = "\033[0;31m" if _C else ""
GREEN  = "\033[0;32m" if _C else ""
YELLOW = "\033[1;33m" if _C else ""
BLUE   = "\033[0;34m" if _C else ""
BOLD   = "\033[1m" if _C else ""
DIM    = "\033[2m" if _C else ""
NC     = "\033[0m" if _C else ""


# -- Helpers -------------------------------------------------------------------


def cprint(msg: str) -> None:
    print(msg, flush=True)

# Wall-clock bound for the TEST SUITE specifically (issue #179). `run()`'s 300s
# default is sized for a lint/scan invocation; a real suite is minutes, so
# inheriting that default made gate G4 UNSATISFIABLE for any plugin whose tests
# run longer — a 13,618-test suite reached 47% at the cap and the gate killed its
# own run. A cap the suite cannot finish inside does not make the gate stricter,
# it makes it unprovable, and a timeout is indistinguishable from a hang. The
# bound is overridable so the NEXT larger suite does not have to patch the
# template — a fixed bound is exactly the defect being fixed here, and replacing
# 300 with a bigger constant would only move the cliff.
_TEST_SUITE_TIMEOUT_ENV = "PLUGIN_TEST_SUITE_TIMEOUT"
_DEFAULT_TEST_SUITE_TIMEOUT = 1800.0

# Wall-clock bound for the RELEASE PUSH (issue #224). The push is not a bare
# network transfer: the branch-aware pre-push hook runs the whole gate — remote
# CPV validate plus the full test suite — INSIDE the push's own clock. Inheriting
# cpv_network_resilience's 600s network default killed every attempt mid-gate and
# retried it 60 times (a 4-second bare push's budget), orphaning a gate subtree
# each round. Size it to the gate's own work plus 30 min of slack, and drop the
# attempt budget to 3: enough to absorb a real transfer hiccup AFTER a green gate.
#
# _CPV_TIMEOUT_SEC is ALSO the single budget for every `uvx cpv-remote-validate`
# invocation — gate G3, stage_validate and stage_ci_preflight all pass it
# (audit row 4). Three different budgets for one command is a defect on its own:
# the tightest one silently sits on the publish path. A cold `uvx` build + a full
# remote validate was measured at ~76s, so 600s is ample headroom, not a stall.
_CPV_TIMEOUT_SEC = 600.0
# The suite is budgeted TWICE: the branch-aware pre-push hook runs the full test
# suite at G4 and again at G4b (the fork-parity probe re-runs it under a forced
# `fork` start method), both inside the push's own clock. Budgeting one suite run
# killed the push mid-G4b on a slow tree.
_PUSH_TIMEOUT_SEC = _CPV_TIMEOUT_SEC + 2 * _DEFAULT_TEST_SUITE_TIMEOUT + 1800.0
_PUSH_MAX_ATTEMPTS = 3

# The numbered pipeline stages, in the order main() runs them. SINGLE SOURCE OF
# TRUTH for the `[N/M]` progress labels, the module docstring and --print-gates:
# a hand-typed total is what let the docstring claim 11 while main() ran more
# (audit row 31). Post-release stages are listed separately because they run
# after the release is public and can never abort it, so numbering them would
# misrepresent them as gates the publish is conditional on.
_PIPELINE_STAGES = [
    "Bypass guard",
    "Check working tree is clean",
    "Lint + type-check (ruff + mypy)",
    "Run tests (pytest)",
    "Validate plugin (remote CPV)",
    "Secret scan (trufflehog)",
    "Linux fork-parity probe",
    "CI-parity preflight (remote CPV)",
    "Marketplace-registration check",
    "Check version consistency",
    "Bump version",
    "Update README version badge",
    "Generate changelog (git-cliff)",
    "Commit, tag, push",
    "Create GitHub release",
]
_POST_RELEASE_STAGES = [
    "Verify CI is green on the released commit",
    "Prove the release installs",
]
_TOTAL_PIPELINE_STAGES = len(_PIPELINE_STAGES)


def print_gates() -> int:
    """Print the numbered stage list and return 0. No side effects whatsoever.

    Deliberately runs BEFORE the repo lookup and every gate: it answers "what
    does this pipeline do?", a question that must be answerable from a dirty
    tree, outside a git repo, and offline.
    """
    print(f"Publish pipeline stages ({_TOTAL_PIPELINE_STAGES}):")
    for _i, _name in enumerate(_PIPELINE_STAGES, start=1):
        print(f"  {_i:>2}/{_TOTAL_PIPELINE_STAGES}. {_name}")
    print()
    print("Post-release stages (unnumbered — never abort the publish):")
    for _name in _POST_RELEASE_STAGES:
        print(f"      - {_name}")
    return 0


def _test_suite_timeout() -> float:
    """Seconds allowed for the pytest gate; the env override wins when positive.

    An empty, zero, negative, or unparseable value falls back to the default.
    That asymmetry is deliberate: a typo must never SHORTEN the bound, because a
    near-zero ceiling would re-create the unsatisfiable gate this constant exists
    to remove.
    """
    raw = os.environ.get(_TEST_SUITE_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_TEST_SUITE_TIMEOUT
    try:
        override = float(raw)
    except ValueError:
        return _DEFAULT_TEST_SUITE_TIMEOUT
    return override if override > 0 else _DEFAULT_TEST_SUITE_TIMEOUT


def _fork_parity_timeout() -> float:
    """Budget for the fork-parity probe — the SAME one the test gate uses.

    The probe re-runs the SAME suite, so it gets the SAME bound. A named alias
    rather than a second env lookup: two parsers of one variable drift, and the
    drifted one is the bound nobody notices. It also keeps every resolution of
    this budget in one region of the file, beside the constants it derives from.
    """
    return _test_suite_timeout()


def run(
    cmd: list[str], cwd: Path | None = None, *, check: bool = True, capture: bool = False,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    """Run a command, stream output, fail-fast on error.

    `timeout` stays at 300s by default — the right bound for the lint/scan steps
    that make up almost every call site, and the reason a hung one fails fast.
    Callers whose work is legitimately longer pass their own; see
    `_test_suite_timeout` for the test gate.
    """
    cprint(f"  {BLUE}$ {' '.join(cmd)}{NC}")
    # A subprocess exceeding `timeout` raises TimeoutExpired; without this it
    # would die with a raw traceback instead of the styled fail-fast message
    # every other failure path uses. Catch it and exit 1.
    try:
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True,
                                capture_output=capture, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Report the ACTUAL bound: a hardcoded "300s" starts lying the moment any
        # caller overrides it, and a wrong number here sends triage the wrong way.
        cprint(f"  {RED}Command timed out after {timeout:g}s: {' '.join(cmd)}{NC}")
        sys.exit(1)
    if check and result.returncode != 0:
        cprint(f"  {RED}Command failed (exit {result.returncode}){NC}")
        sys.exit(result.returncode)
    return result

def get_repo_root() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, check=True)
    return Path(r.stdout.strip())


# -- gh-auth precheck (TRDD-bbff5bc5) ---------------------------------------


def _parse_owner_repo_from_remote(remote_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from `git@host:owner/repo.git` or
    `https://host/owner/repo[.git]`. Returns None on unparseable input.
    """
    if not remote_url:
        return None
    url = remote_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    match = re.search(r"[:/]([^:/\s]+)/([^/\s]+)$", url)
    if not match:
        return None
    return match.group(1), match.group(2)


def _resolve_owner_repo(plugin_root: Path) -> tuple[str, str]:
    """Read remote.origin.url, parse (owner, repo). Exit 1 on failure."""
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=str(plugin_root), capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        cprint(f"  {RED}Could not read remote.origin.url. Run: git remote add origin <url>{NC}")
        sys.exit(1)
    parsed = _parse_owner_repo_from_remote(result.stdout.strip())
    if parsed is None:
        cprint(f"  {RED}Could not parse owner/repo from remote URL: {result.stdout.strip()!r}{NC}")
        sys.exit(1)
    return parsed


def _ensure_gh_auth(owner: str, repo: str) -> None:
    """Verify gh CLI installed + authenticated + push perm on owner/repo.

    Called BEFORE every push gate. Exits 1 on any of: gh missing, not
    authed, no push permission. Per TRDD-bbff5bc5 §4.1: never invokes
    `gh auth token`; uses only `gh auth status` and `gh api` so PAT-shaped
    strings cannot leak to stdout/stderr.
    """
    if os.environ.get("CPV_SKIP_GH_AUTH_CHECK") == "1":
        return
    gh_bin = shutil.which("gh")
    if gh_bin is None:
        cprint(f"  {RED}gh CLI not installed. Install: brew install gh{NC}")
        sys.exit(1)
    # 60s timeout (was 15s) — slow-link tolerance; downstream push gates
    # still enforce real auth on failure.
    try:
        status = subprocess.run(
            [gh_bin, "auth", "status"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        cprint(f"  {RED}gh auth status timed out after 60 s — flaky network. Retry, or set CPV_SKIP_GH_AUTH_CHECK=1.{NC}")
        sys.exit(1)
    if status.returncode != 0:
        cprint(f"  {RED}gh CLI not authenticated.{NC}")
        cprint(f"  {YELLOW}Run: gh auth login --hostname github.com --git-protocol https{NC}")
        sys.exit(1)
    try:
        perms = subprocess.run(
            [gh_bin, "api", f"repos/{owner}/{repo}", "--jq", ".permissions.push"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        cprint(f"  {RED}gh permission check timed out after 60 s — set CPV_SKIP_GH_AUTH_CHECK=1 to bypass this gate.{NC}")
        sys.exit(1)
    if perms.returncode != 0 or perms.stdout.strip() != "true":
        active_login = ""
        for line in (status.stdout + status.stderr).splitlines():
            line = line.strip()
            if "account " in line and ("Logged in" in line or "Active" in line):
                m = re.search(r"account\s+(\S+)", line)
                if m:
                    active_login = m.group(1)
                    break
        login_str = f" '{active_login}'" if active_login else ""
        cprint(f"  {RED}gh user{login_str} has no push permission on {owner}/{repo}.{NC}")
        cprint(f"  {YELLOW}Diagnose:{NC}")
        cprint(f"  {YELLOW}  1. Ask the repo owner to add you as a collaborator with write access.{NC}")
        cprint(f"  {YELLOW}  2. If you have multiple gh accounts: gh auth status; gh auth switch{NC}")
        cprint(f"  {YELLOW}  3. If using a fine-grained token: ensure 'Contents: write' on this repo.{NC}")
        sys.exit(1)


# -- Semver --------------------------------------------------------------------

def parse_semver(version: str) -> tuple[int, int, int] | None:
    """Parse 'X.Y.Z' into (major, minor, patch)."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))

def bump_semver(current: str, bump_type: str) -> str | None:
    """Bump version by major/minor/patch. Returns new version string or None."""
    parsed = parse_semver(current)
    if not parsed:
        return None
    major, minor, patch = parsed
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return None


# -- Version readers/writers ---------------------------------------------------

def get_current_version(plugin_root: Path) -> str | None:
    """Read version from .claude-plugin/plugin.json."""
    pj = plugin_root / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        ver = data.get("version")
        return str(ver) if ver is not None else None
    except (json.JSONDecodeError, OSError):
        return None

def update_plugin_json(root: Path, new_ver: str) -> tuple[bool, str]:
    """Write version to .claude-plugin/plugin.json."""
    pj = root / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return False, "plugin.json not found"
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        data["version"] = new_ver
        pj.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True, f"plugin.json -> {new_ver}"
    except (json.JSONDecodeError, OSError) as e:
        return False, f"plugin.json update failed: {e}"

def update_self_marketplace_json(root: Path, new_ver: str) -> tuple[bool, str]:
    """Write version to .claude-plugin/marketplace.json (Layout C — both metadata and self-entry)."""
    mp = root / ".claude-plugin" / "marketplace.json"
    if not mp.is_file():
        return False, "no marketplace.json (not Layout C)"
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return False, f"marketplace.json read failed: {e}"
    # Bump metadata.version if present
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata["version"] = new_ver
    # Bump the self-entry's version (the entry whose name matches plugin.json's name AND source is "./")
    plugin_json_path = root / ".claude-plugin" / "plugin.json"
    plugin_name: str | None = None
    if plugin_json_path.is_file():
        try:
            pdata = json.loads(plugin_json_path.read_text(encoding="utf-8"))
            plugin_name = pdata.get("name")
        except (json.JSONDecodeError, OSError):
            plugin_name = None
    plugins = data.get("plugins")
    bumped_entry = False
    if isinstance(plugins, list):
        for entry in plugins:
            if not isinstance(entry, dict):
                continue
            entry_name = entry.get("name")
            entry_source = entry.get("source")
            is_self = (
                (entry_name == plugin_name or plugin_name is None)
                and entry_source in ("./", {"source": "directory", "path": "./"})
            )
            if is_self:
                entry["version"] = new_ver
                bumped_entry = True
                break
    try:
        mp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        return False, f"marketplace.json write failed: {e}"
    if bumped_entry:
        return True, f"marketplace.json (metadata + self-entry) -> {new_ver}"
    return True, f"marketplace.json (metadata only — no self-entry matched) -> {new_ver}"

def _project_block(content: str) -> tuple[int, int] | None:
    """Char span of the [project] table body, or None if absent.

    The project version lives in the [project] table. A whole-file first-match
    for `version = "..."` writes the WRONG version when a [tool.X] table with
    its own top-level `version` (e.g. [tool.commitizen]) precedes [project].
    When there is no [project] table (poetry keeps it under [tool.poetry]),
    return None so the caller falls back to the legacy whole-file first-match.
    """
    m = re.search(r'^\[project\]\s*$', content, re.MULTILINE)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r'^\[', content[start:], re.MULTILINE)
    return start, (start + nxt.start() if nxt else len(content))

def update_pyproject_toml(root: Path, new_ver: str) -> tuple[bool, str]:
    """Write version to pyproject.toml."""
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return False, "pyproject.toml not found"
    try:
        content = pp.read_text(encoding="utf-8")
        block = _project_block(content)
        if block is not None:
            lo, hi = block
            replaced = re.sub(
                r'^(version\s*=\s*")[^"]*(")',
                rf'\g<1>{new_ver}\2',
                content[lo:hi],
                count=1,
                flags=re.MULTILINE,
            )
            updated = content[:lo] + replaced + content[hi:]
        else:
            updated = re.sub(
                r'^(version\s*=\s*")[^"]*(")',
                rf'\g<1>{new_ver}\2',
                content,
                count=1,
                flags=re.MULTILINE,
            )
        if updated == content:
            return False, "pyproject.toml: version field not found"
        pp.write_text(updated, encoding="utf-8")
        return True, f"pyproject.toml -> {new_ver}"
    except OSError as e:
        return False, f"pyproject.toml update failed: {e}"

def update_python_versions(root: Path, new_ver: str) -> list[tuple[bool, str]]:
    """Update __version__ = '...' in all .py files under scripts/."""
    results: list[tuple[bool, str]] = []
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        return results
    pattern = re.compile(r'^(__version__\s*=\s*["\'])([^"\']*)(["\']\s*)$', re.MULTILINE)
    for py_file in scripts_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if not pattern.search(content):
            continue
        updated = pattern.sub(rf"\g<1>{new_ver}\3", content)
        if updated != content:
            py_file.write_text(updated, encoding="utf-8")
            results.append((True, f"{py_file.relative_to(root)} -> {new_ver}"))
    return results

def check_version_consistency(root: Path) -> tuple[bool, str]:
    """Verify all version sources match. Includes marketplace.json metadata
    and self-entry (Layout C) when present."""
    versions: dict[str, str | None] = {}

    # plugin.json
    pj = root / ".claude-plugin" / "plugin.json"
    if pj.is_file():
        try:
            versions["plugin.json"] = json.loads(pj.read_text(encoding="utf-8")).get("version")
        except (json.JSONDecodeError, OSError):
            versions["plugin.json"] = None

    # marketplace.json (Layout C) — both metadata.version and the self-entry's version
    mp = root / ".claude-plugin" / "marketplace.json"
    if mp.is_file():
        try:
            mp_data = json.loads(mp.read_text(encoding="utf-8"))
            md = mp_data.get("metadata")
            if isinstance(md, dict):
                versions["marketplace.json:metadata"] = md.get("version")
            plugins_arr = mp_data.get("plugins")
            if isinstance(plugins_arr, list):
                for entry in plugins_arr:
                    if not isinstance(entry, dict):
                        continue
                    src = entry.get("source")
                    if src == "./" or (
                        isinstance(src, dict) and src.get("source") == "directory" and src.get("path") == "./"
                    ):
                        versions["marketplace.json:self-entry"] = entry.get("version")
                        break
        except (json.JSONDecodeError, OSError):
            versions["marketplace.json"] = None

    # pyproject.toml — read from the [project] table body when present, else
    # fall back to the whole-file first-match (poetry-style layouts).
    pp = root / "pyproject.toml"
    if pp.is_file():
        pp_text = pp.read_text(encoding="utf-8")
        blk = _project_block(pp_text)
        hay = pp_text[blk[0]:blk[1]] if blk is not None else pp_text
        m = re.search(r'^version\s*=\s*"([^"]*)"', hay, re.MULTILINE)
        versions["pyproject.toml"] = m.group(1) if m else None

    found = {k: v for k, v in versions.items() if v is not None}
    if not found:
        return False, "No version sources found"
    unique = set(found.values())
    if len(unique) == 1:
        return True, f"All versions match: {unique.pop()}"
    details = ", ".join(f"{k}={v}" for k, v in found.items())
    return False, f"Version mismatch: {details}"

def _sync_uv_lock(root: Path) -> None:
    """Re-resolve ``uv.lock`` against the freshly-bumped ``pyproject.toml``.

    Without this, every release leaves ``uv.lock`` stale by one version
    (``pyproject.toml`` says e.g. ``2.66.2`` but ``uv.lock`` still pins the
    root package at ``2.66.1``). The NEXT publish then runs an outer
    ``uv run``/``uv lock``/``uv sync`` which re-syncs that single root-version
    line in place, DIRTYING the working tree — and Gate 1 (clean-tree check)
    aborts that publish before it does anything (issue #149). Co-locating the
    sync in do_bump (the only place pyproject.toml is written) guarantees the
    lock can never be stale after a successful bump. Idempotent; silently
    skipped when neither ``uv`` nor ``uv.lock`` is present (plugins authored
    without uv, or a host where uv isn't installed). ``check=False`` so a uv
    hiccup degrades to a no-op instead of aborting the bump.
    """
    if not (root / "uv.lock").is_file():
        return
    if shutil.which("uv") is None:
        return
    run(["uv", "lock"], root, check=False)

def do_bump(root: Path, new_ver: str, dry_run: bool = False) -> bool:
    """Orchestrate all version updates. Detects Layout C (marketplace.json at repo root)
    and bumps both manifests atomically when present."""
    cprint(f"\n{BOLD}Bumping to {new_ver}{' (dry-run)' if dry_run else ''}{NC}")

    is_layout_c = (root / ".claude-plugin" / "marketplace.json").is_file()

    if dry_run:
        cprint(f"  Would update plugin.json -> {new_ver}")
        if is_layout_c:
            cprint(f"  Would update marketplace.json (metadata + self-entry, Layout C) -> {new_ver}")
        cprint(f"  Would update pyproject.toml -> {new_ver}")
        cprint(f"  Would update __version__ vars -> {new_ver}")
        return True

    ok1, msg1 = update_plugin_json(root, new_ver)
    cprint(f"  {'OK' if ok1 else 'FAIL'}: {msg1}")

    ok_mp = True
    if is_layout_c:
        ok_mp, msg_mp = update_self_marketplace_json(root, new_ver)
        cprint(f"  {'OK' if ok_mp else 'FAIL'}: {msg_mp}")

    ok2, msg2 = update_pyproject_toml(root, new_ver)
    cprint(f"  {'OK' if ok2 else 'FAIL'}: {msg2}")

    py_results = update_python_versions(root, new_ver)
    for ok, msg in py_results:
        cprint(f"  {'OK' if ok else 'FAIL'}: {msg}")

    ok = ok1 and ok2 and ok_mp
    if ok:
        # Bump succeeded — bring uv.lock's root version along so the
        # post-publish tree is clean and the NEXT publish's outer `uv run`
        # doesn't re-sync uv.lock and trip Gate 1 (issue #149).
        _sync_uv_lock(root)
    return ok


# -- Hook installer ------------------------------------------------------------

def install_hook(root: Path) -> int:
    """Copy git-hooks/pre-push to .git/hooks/pre-push and set core.hooksPath."""
    cprint(f"\n{BOLD}Installing git hooks...{NC}")
    source = root / "git-hooks" / "pre-push"
    if not source.is_file():
        cprint(f"  {RED}git-hooks/pre-push not found{NC}")
        return 1
    git_dir = root / ".git"
    if not git_dir.is_dir():
        cprint(f"  {RED}.git/ not found — is this a git repository?{NC}")
        return 1
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-push"
    shutil.copy2(source, dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cprint(f"  {GREEN}Installed: git-hooks/pre-push -> .git/hooks/pre-push{NC}")
    # Also set core.hooksPath so git finds hooks in git-hooks/ directly.
    # The return code IS checked (audit row 15): the old code printed success
    # unconditionally, so a failed config write reported "installed" and every
    # later push was silently ungated — the worst possible thing to be wrong
    # about, because the whole point of this command is that pushes ARE gated.
    cfg = subprocess.run(["git", "config", "core.hooksPath", "git-hooks"],
                         cwd=str(root), check=False)
    if cfg.returncode != 0:
        cprint(f"  {RED}FAILED to set git config core.hooksPath = git-hooks "
               f"(exit {cfg.returncode}).{NC}")
        cprint(f"  {RED}The hook file was copied, but git may not run it. Set it by hand:{NC}")
        cprint(f"  {RED}  git config core.hooksPath git-hooks{NC}")
        return 1
    cprint(f"  {GREEN}Set git config core.hooksPath = git-hooks{NC}")
    return 0


def _get_origin_slug(root: Path) -> str | None:
    """Return OWNER/REPO parsed from the current repo's origin remote, or None."""
    try:
        r = subprocess.run(
            # timeout= for consistency with every sibling git helper in this
            # file (audit row 18): a bare `git config` read is instant, and a
            # hang here would stall the branch-rules install with no bound.
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, cwd=str(root), check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    url = r.stdout.strip()
    # Handle git@github.com:OWNER/REPO.git and https://github.com/OWNER/REPO.git
    if url.startswith("git@"):
        _, _, path = url.partition(":")
    elif "//" in url:
        _, _, path = url.partition("//")
        # path is now "github.com/OWNER/REPO.git"
        path = path.split("/", 1)[1] if "/" in path else ""
    else:
        return None
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}/{parts[1]}"


def install_branch_rules(root: Path) -> int:
    """Apply the cpv-branch-rules ruleset to the repo's GitHub origin.

    Auto-detects the OWNER/REPO slug from `git config remote.origin.url` and
    shells out to `uvx cpv-setup-branch-rules` so downstream plugins do not
    need to vendor setup_branch_rules.py locally. This is the server-side
    gate that enforces CI as a required status check — the local pre-push
    hook alone is bypassable with `git push --no-verify`, but a ruleset is
    enforced by GitHub itself.
    """
    cprint(f"\n{BOLD}Installing branch-protection ruleset...{NC}")
    slug = _get_origin_slug(root)
    if slug is None:
        cprint(f"  {RED}Could not read origin remote URL — skipping.{NC}")
        cprint(f"  {YELLOW}Set `git remote add origin <url>` first, then retry.{NC}")
        return 1
    cprint(f"  Target repo: {slug}")
    try:
        r = subprocess.run(
            [
                "uvx",
                "--from",
                "git+https://github.com/Emasoft/claude-plugins-validation@v5.17.0",
                "--with",
                "pyyaml",
                "cpv-setup-branch-rules",
                slug,
            ],
            cwd=str(root),
            check=False,
            # Audit row 12: a cold uvx build or a network stall would otherwise
            # hang this command forever. Same budget as every other uvx CPV call.
            timeout=_CPV_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        cprint(f"  {RED}cpv-setup-branch-rules timed out after {_CPV_TIMEOUT_SEC:g}s "
               f"— branch rules NOT applied.{NC}")
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        cprint(f"  {RED}uvx call failed: {exc}{NC}")
        return 1
    if r.returncode != 0:
        cprint(f"  {RED}cpv-setup-branch-rules exited with code {r.returncode}{NC}")
        return r.returncode
    cprint(f"  {GREEN}Branch rules applied to {slug}.{NC}")
    return 0


# -- Gate mode (pre-push quality checks) --------------------------------------

def _get_process_ancestry(max_depth: int = 30) -> list[tuple[int, str]]:
    """Walk parent processes via ps(1). Returns [(pid, cmdline), ...] closest-first.

    Used by the orchestrator check to verify that scripts/publish.py is an
    ancestor of the current pre-push gate invocation. Process ancestry is
    non-spoofable (unlike env vars, which a user could set with
    `CPV_PIPELINE=1 git push`).
    """
    ancestry: list[tuple[int, str]] = []
    pid = os.getpid()
    seen: set[int] = set()
    for _ in range(max_depth):
        if pid in seen or pid <= 0:
            break
        seen.add(pid)
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,args="],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if r.returncode != 0:
            break
        line = r.stdout.strip()
        if not line:
            break
        parts = line.split(None, 1)
        if not parts:
            break
        try:
            ppid = int(parts[0])
        except ValueError:
            break
        cmdline = parts[1] if len(parts) > 1 else ""
        ancestry.append((pid, cmdline))
        if ppid <= 1:
            break
        pid = ppid
    return ancestry


def _called_by_publish_orchestrator(root: Path, ancestry: list | None = None) -> bool:
    """Verify that scripts/publish.py (in publish mode, NOT --gate) is an ancestor.

    Expected chain for an orchestrated push:
        publish.py --patch|--minor|--major   (orchestrator)
          └─ git push
              └─ git (runs pre-push hook)
                  └─ sh (hook script)
                      └─ publish.py --gate   (this process)

    Walk the parent chain. At least one ancestor must be scripts/publish.py
    WITHOUT the --gate flag (that is, a publish orchestrator — not our own
    gate-mode re-entry).

    `ancestry` is passed in by callers that already walked it, so the gate does
    ONE walk instead of two (audit row 30 — the walk costs up to 30 `ps`
    subprocesses at 5s apiece and both predicates used to do it separately).
    """
    expected_abs = str((root / "scripts" / "publish.py").resolve())
    expected_rel = "scripts/publish.py"
    for _pid, cmdline in (_get_process_ancestry() if ancestry is None else ancestry):
        if "publish.py" not in cmdline:
            continue
        if "--gate" in cmdline:
            continue
        if expected_abs in cmdline or expected_rel in cmdline:
            return True
    return False


def _push_in_flight(ancestry: list | None = None) -> bool | None:
    """Is a `git push` actually being attempted right now? (issue cpv#192)

    Three-valued on purpose:

    * ``True``  — a ``git … push`` (or the pre-push hook itself) is an
      ancestor: we are running INSIDE a push. G0/G1 must be enforced.
    * ``False`` — ancestry is visible and carries no push: a developer ran
      ``publish.py --gate`` standalone. No push exists for G0/G1 to protect,
      so the quality gates run and nothing is blocked.
    * ``None``  — ancestry could not be determined (``ps`` failed). Callers
      MUST fail closed and treat this like ``True``: an undetectable ancestry
      must never become the bypass G0 exists to prevent.

    The regex deliberately also matches the hook script's own path
    (``.git/hooks/pre-push``) — that process IS push context. A false
    positive (e.g. a wrapper shell whose cmdline mentions ``git push``)
    degrades to enforcing G0, i.e. fails closed.

    `ancestry` is passed in by a caller that already walked it (audit row 30);
    None means "walk it here".
    """
    if ancestry is None:
        ancestry = _get_process_ancestry()
    if not ancestry:
        return None
    return any(re.search(r"\bgit\b.*\bpush\b", cmdline) for _pid, cmdline in ancestry)


# Directories no tree walk in this file should ever descend into: build output,
# vendored deps and VCS internals. One constant, used by the compiled-build gate,
# the shell lint and the fork-parity probe.
_SCAN_SKIP_DIRS = {"target", ".git", "node_modules", ".venv", "vendor",
                   "dist", "build", "obj", "zig-out", "zig-cache", ".zig-cache"}


def _secret_scan(root: Path) -> int:
    """Secret-scan the working tree with trufflehog. 0 = clean, 1 = BLOCK.

    Shared by gate G3s and the `stage_secret_scan` pipeline stage (audit row 6).
    It must exist in BOTH places: the pre-push hook secret-scans FEATURE branches
    only — a default-branch / tag push is gated on publish.py ancestry instead —
    so a plugin that never ran `--install-hook`, or whose `core.hooksPath` was
    reset, would publish having scanned nothing. That is the shape of CPV issue
    #217 (a committed auth key reached main and sat there for 85 days while every
    local gate passed).

    trufflehog is a DEPENDENCY of this pipeline, not a precondition the publisher
    is told to satisfy: if it is missing we INSTALL it. Only a FAILED install
    blocks — and it must block, because "we never looked" and "we looked and
    found nothing" are not the same answer.
    """
    if not shutil.which("trufflehog"):
        cprint(f"  {YELLOW}trufflehog missing — installing it as a pipeline dependency...{NC}")
        # A stalled installer must land on the styled BLOCKED path below, not
        # die with a raw TimeoutExpired traceback (audit row 16).
        try:
            if shutil.which("brew"):
                subprocess.run(["brew", "install", "trufflehog"], timeout=900)
            if not shutil.which("trufflehog") and shutil.which("go"):
                subprocess.run(
                    ["go", "install", "github.com/trufflesecurity/trufflehog/v3@latest"],
                    timeout=900)
                # `go install` drops the binary in GOBIN/GOPATH/bin, which is
                # often not yet on PATH in this process.
                _gobin = os.environ.get("GOBIN") or str(
                    Path(os.environ.get("GOPATH") or (Path.home() / "go")) / "bin")
                os.environ["PATH"] = _gobin + os.pathsep + os.environ.get("PATH", "")
        except subprocess.TimeoutExpired:
            cprint(f"  {YELLOW}The trufflehog installer timed out (>900s).{NC}")
        except (OSError, subprocess.SubprocessError) as _exc:
            cprint(f"  {YELLOW}The trufflehog installer failed to run: {_exc}{NC}")
    if not shutil.which("trufflehog"):
        cprint(f"  {RED}BLOCKED: trufflehog is not installed and could not be installed.{NC}")
        cprint(f"  {RED}The release was NOT secret-scanned — UNKNOWN is not clean.{NC}")
        cprint(f"  {RED}Install it and re-run:  brew install trufflehog{NC}")
        return 1
    # Exclude gitignored-AND-UNTRACKED paths from the WALK (not from the
    # results): scanning a large ignored corpus and discarding the hits is the
    # same verdict for far more work. `--others --ignored` lists exactly the
    # untracked-ignored set, so a TRACKED file stays scanned even when it also
    # matches .gitignore — such a file still ships, and skipping it would be a
    # scan-evasion vector.
    _sec_root = str(root.resolve()).rstrip("/")
    _ign = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
        cwd=str(root), capture_output=True, text=True, timeout=120)
    _ign_paths = [ln.strip() for ln in (_ign.stdout or "").splitlines() if ln.strip()]
    _excl_fh = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        # trufflehog matches these regexes against ABSOLUTE paths.
        _excl_fh.write("^" + re.escape(_sec_root + "/.git") + "\n")
        for _rel in sorted(_ign_paths):
            _excl_fh.write("^" + re.escape(_sec_root + "/" + _rel.rstrip("/")) + "\n")
        _excl_fh.close()
        _th = subprocess.run(
            ["trufflehog", "filesystem", _sec_root, "--json", "--no-update", "--fail",
             # Without the widened set trufflehog OMITS `filtered_unverified`,
             # the bucket an expired / revoked / unreachable credential lands
             # in — a committed secret is a leak whether or not a runner can
             # reach its API (CPV issue #219).
             "--results=verified,unknown,unverified,filtered_unverified",
             "-x", _excl_fh.name],
            capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        cprint(f"  {RED}BLOCKED: trufflehog timed out — scan INCOMPLETE, secrets UNKNOWN.{NC}")
        return 1
    finally:
        try:
            os.unlink(_excl_fh.name)
        except OSError:
            pass
    if _th.returncode == 183:
        _dets = []
        for _ln in (_th.stdout or "").splitlines():
            if not _ln.startswith("{"):
                continue
            try:
                _f = json.loads(_ln)
            except json.JSONDecodeError:
                continue
            _fs = _f.get("SourceMetadata", {}).get("Data", {}).get("Filesystem", {})
            _dets.append(f"{_f.get('DetectorName', '?')} in {_fs.get('file', '?')}")
        cprint(f"  {RED}BLOCKED: {len(_dets)} credential(s) detected.{NC}")
        for _d in _dets[:20]:
            cprint(f"    {RED}{_d}{NC}")
        cprint(f"  {RED}Redaction is NOT done by this gate. A verified live credential"
               f" must be ROTATED and purged from git history.{NC}")
        return 1
    if _th.returncode != 0:
        cprint(f"  {RED}BLOCKED: trufflehog exited {_th.returncode} — scan did not"
               f" complete, so secrets are UNKNOWN (that is not clean).{NC}")
        return 1
    cprint(f"  {GREEN}No credentials detected.{NC}")
    return 0


def _fork_parity_probe(root: Path, suite_timeout: float) -> int:
    """Re-run the suite with multiprocessing forced to fork. 0 = ok, 1 = BLOCK.

    Shared by gate G4b and the `stage_fork_parity` pipeline stage (audit row 14).

    `multiprocessing` defaults to FORK on Linux and SPAWN on macOS. Forking a
    multithreaded process copies mutex state, so a child can inherit a lock
    (sys.stderr's, logging's) held by a thread that does not exist in the child,
    and hang forever on its first write. The defect is INVISIBLE on a macOS dev
    box and fatal in CI: CPV shipped exactly that, turning an 8.7s run into a
    >300s timeout that failed both CI and Release AFTER a fully green local suite
    of 11,484 tests.

    Self-detecting: runs ONLY when this plugin's own Python creates process
    pools. Linux (already forks) -> skip, so CI is never doubled. No fork
    available -> WARN+skip. Blocks only when the probe RAN and the suite failed
    -- and a TIMEOUT counts, because a hang IS the signature.
    """
    _pool_src = []
    for p in root.rglob("*.py"):
        # `is_file()` + suppressed OSError (audit row 23): a DIRECTORY named
        # `*.py`, or an unreadable file, used to raise IsADirectoryError /
        # PermissionError out of read_text() and abort the whole gate with a
        # traceback.
        if not p.is_file():
            continue
        if any(part in _SCAN_SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        try:
            _text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(tok in _text for tok in ("ProcessPoolExecutor", "multiprocessing")):
            _pool_src.append(p)
    if not _pool_src:
        cprint(f"  {GREEN}No process pools in this plugin -- skipped.{NC}")
        return 0
    import multiprocessing as _mp
    if "fork" not in _mp.get_all_start_methods():
        cprint(f"  {YELLOW}WARNING: fork unavailable here -- parity probe SKIPPED.{NC}")
        cprint(f"  {YELLOW}Linux CI is the FIRST place the fork path will run.{NC}")
        return 0
    if _mp.get_start_method(allow_none=False) == "fork":
        cprint(f"  {GREEN}Platform already defaults to fork -- the normal test run covers it.{NC}")
        return 0
    _site = root / ".cpv-forkparity"
    _site.mkdir(exist_ok=True)
    (_site / "sitecustomize.py").write_text(
        "import multiprocessing as _m\n"
        "try:\n    _m.set_start_method('fork', force=True)\n"
        "except Exception:\n    pass\n",
        encoding="utf-8")
    try:
        _env = dict(os.environ)
        _env["PYTHONPATH"] = str(_site) + (
            os.pathsep + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
        try:
            _fp = subprocess.run(
                ["uv", "run", "pytest", "tests/", "-q", "--tb=short"],
                cwd=str(root), env=_env, timeout=suite_timeout).returncode
        except subprocess.TimeoutExpired:
            cprint(f"  {RED}BLOCKED: suite HUNG under the Linux fork default.{NC}")
            cprint(f"  {RED}Something forks a multithreaded process. Pass an explicit{NC}")
            cprint(f"  {RED}mp_context (spawn) to every pool instead of the platform default.{NC}")
            return 1
        if _fp != 0:
            cprint(f"  {RED}BLOCKED: tests fail under the Linux fork default.{NC}")
            cprint(f"  {RED}They pass under spawn -- this is what Linux CI will do to this commit.{NC}")
            return 1
        cprint(f"  {GREEN}Suite passes under the Linux fork default.{NC}")
        return 0
    finally:
        shutil.rmtree(_site, ignore_errors=True)


def run_gate(root: Path) -> int:
    """Quality gate. Blocks on any quality issue; returns 0 if clean.

    G0 (orchestrator) and G1's version-bump block protect a PUSH, so they are
    enforced only while one is in flight; a standalone ``--gate`` run gets the
    quality checks the flag advertises (issue cpv#192).
    """
    cprint(f"\n{BOLD}Pre-push gate checks{NC}\n")

    # Gate 0: Orchestrator check — only publish.py may trigger a push.
    # Prevents a user from running `git push` directly and bypassing the
    # version-bump / changelog / tag / release pipeline. Uses process
    # ancestry (non-spoofable), NOT env vars.
    #
    # Enforced ONLY when a push is actually in flight: G0's invariant is "no
    # PUSH bypasses the pipeline", not "no checks may run". A standalone
    # `--gate` pushes nothing, so blocking it defended nothing and told the
    # user they attempted an action they never took (issue cpv#192). A direct
    # `git push` still carries git-push ancestry and is still blocked.
    cprint(f"{BLUE}[G0] Checking push orchestrator...{NC}")
    # POSIX-only, LOUDLY (audit row 2). Every ancestry probe below shells out to
    # `ps -p`, which does not exist on Windows: there OSError makes the walk
    # return [], `_push_in_flight` returns None (fail-closed) and
    # `_called_by_publish_orchestrator` returns False — so G0 would block EVERY
    # push including publish.py's own, and the plugin could never be released,
    # with no explanation. Refuse explicitly instead of failing mysteriously.
    if os.name != "posix":
        cprint(f"  {RED}publish.py canon is POSIX-only (macOS/Linux); Windows is unsupported{NC}")
        return 1
    # ONE ancestry walk feeds BOTH predicates (audit row 30): the walk costs up
    # to 30 `ps` subprocesses at 5s apiece, and it was being done twice per gate.
    _ancestry = _get_process_ancestry()
    push_ctx = _push_in_flight(_ancestry)
    if push_ctx is False:
        cprint(f"  {YELLOW}No push in flight — standalone gate run; orchestrator check not applicable.{NC}")
    elif not _called_by_publish_orchestrator(root, _ancestry):
        # push_ctx is True (inside a pre-push hook) or None (ancestry
        # unknown — fail CLOSED: treat as a push we cannot vouch for).
        cprint("")
        cprint(f"  {RED}========================================{NC}")
        cprint(f"  {RED}  BLOCKED: Direct push not allowed{NC}")
        cprint(f"  {RED}  This push was not started by{NC}")
        cprint(f"  {RED}  scripts/publish.py, which must drive{NC}")
        cprint(f"  {RED}  every push (bump/changelog/tag/release).{NC}")
        if push_ctx is None:
            cprint(f"  {RED}  (process ancestry unavailable — failing closed){NC}")
        cprint(f"  {RED}  To release, run one of:{NC}")
        cprint(f"  {RED}    uv run python scripts/publish.py --patch{NC}")
        cprint(f"  {RED}    uv run python scripts/publish.py --minor{NC}")
        cprint(f"  {RED}    uv run python scripts/publish.py --major{NC}")
        cprint(f"  {RED}========================================{NC}")
        return 1
    else:
        cprint(f"  {GREEN}Orchestrated by publish.py.{NC}")

    # Gate 1: Version bump check — local vs remote
    # Resolves origin/HEAD dynamically so the gate works on both `main` and
    # `master` default branches (and any other name). If none of the
    # candidates return a remote plugin.json, it's a first push and we allow.
    cprint(f"\n{BLUE}[G1] Checking version bump...{NC}")
    local_ver = get_current_version(root)
    if local_ver:
        # Try origin/HEAD first (most reliable), then explicit main/master
        candidates: list[str] = []
        try:
            sym = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, cwd=str(root), timeout=10,
            )
            if sym.returncode == 0 and sym.stdout.strip():
                # Output looks like "refs/remotes/origin/main"
                branch = sym.stdout.strip().split("/")[-1]
                candidates.append(f"origin/{branch}")
        except (OSError, subprocess.SubprocessError):
            pass
        for fallback in ("origin/main", "origin/master"):
            if fallback not in candidates:
                candidates.append(fallback)
        remote_ver: str | None = None
        matched_ref: str | None = None
        for ref in candidates:
            try:
                r = subprocess.run(
                    ["git", "show", f"{ref}:.claude-plugin/plugin.json"],
                    capture_output=True, text=True, cwd=str(root), timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode == 0 and r.stdout:
                try:
                    data = json.loads(r.stdout)
                    rv = data.get("version")
                    if isinstance(rv, str):
                        remote_ver = rv
                        matched_ref = ref
                        break
                except json.JSONDecodeError:
                    continue
        if remote_ver is None:
            cprint(f"  {YELLOW}No remote plugin.json found (first push?) — skipping version-bump check.{NC}")
        elif local_ver == remote_ver:
            # Same principle as G0 (issue cpv#192): "version must be bumped"
            # is an invariant about a PUSH. During a real publish the bump has
            # already happened when the hook fires, so hitting this in push
            # context means a bypass — block. Standalone, pre-bump is the
            # NORMAL state (the pipeline bumps later); blocking would just
            # recreate the G0 complaint one gate down.
            if push_ctx is False:
                cprint(f"  {YELLOW}Version not bumped yet ({local_ver} == {matched_ref}) — fine for a standalone check; the publish pipeline bumps before pushing.{NC}")
            else:
                cprint(f"  {RED}BLOCKED: Version not bumped — local {local_ver} == {matched_ref} {remote_ver}{NC}")
                return 1
        else:
            cprint(f"  {GREEN}Version bump OK: {remote_ver} → {local_ver} (via {matched_ref}){NC}")

    # Gate 2: Lint with ruff. MANDATORY — missing scripts/ dir is a BLOCK.
    cprint(f"\n{BLUE}[G2] Linting...{NC}")
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        cprint(f"  {RED}BLOCKED: scripts/ directory missing — cannot lint.{NC}")
        return 1
    # 300s, not 120 (audit row 17): `uv run` CREATES/SYNCS the venv on a cold
    # machine before ruff even starts, which routinely exceeds two minutes — so
    # the tighter bound blocked a perfectly healthy tree with "Command failed".
    # Same order as every other gate here.
    lint_result = subprocess.run(
        ["uv", "run", "ruff", "check", "scripts/"],
        cwd=str(root), timeout=300)
    if lint_result.returncode != 0:
        cprint(f"  {RED}BLOCKED: Lint issues found{NC}")
        return 1
    cprint(f"  {GREEN}Lint passed.{NC}")

    # Gate 2b: Copy-paste detection (jscpd) — PARITY with ci.yml Mega-Linter COPYPASTE_JSCPD.
    # CI's Lint job fails on jscpd duplication over the .jscpd.json threshold; surface it locally
    # BEFORE the bump/tag/push. jscpd needs Node/npx; if it cannot be obtained, DEGRADE to a
    # non-blocking WARNING (CI still enforces it) — a green gate then does NOT guarantee green CI
    # for the copy-paste dimension (issue #143). NEVER false-block a push on a tool-install failure.
    cprint(f"\n{BLUE}[G2b] Copy-paste check (jscpd, parity with CI)...{NC}")
    jscpd_bin = shutil.which("jscpd")
    # Resolve npx ONCE into a variable so mypy narrows it (a second
    # shutil.which("npx") call INSIDE the list keeps the element typed
    # `str | None`, making base_cmd `list[str | None]` → subprocess.run
    # [arg-type] under --strict). issue #151.
    npx_bin = shutil.which("npx")
    base_cmd = [jscpd_bin] if jscpd_bin else ([npx_bin, "--yes", "jscpd"] if npx_bin else None)
    if base_cmd is None:
        cprint(f"  {YELLOW}WARNING: jscpd/npx not found — copy-paste check SKIPPED locally.{NC}")
        cprint(f"  {YELLOW}CI's Mega-Linter WILL enforce it (.jscpd.json threshold). A green gate does")
        cprint(f"  {YELLOW}NOT guarantee green CI for the copy-paste dimension (issue #143). Install")
        cprint(f"  {YELLOW}Node/npx for full local parity.{NC}")
    else:
        # Probe distinguishes 'jscpd unavailable/uninstallable' (WARN) from 'jscpd ran, found dupes' (BLOCK).
        probe = subprocess.run(base_cmd + ["--version"], cwd=str(root),
                               capture_output=True, text=True, timeout=180)
        if probe.returncode != 0:
            cprint(f"  {YELLOW}WARNING: jscpd could not run (npx fetch/install failed) — SKIPPED locally.{NC}")
            cprint(f"  {YELLOW}CI's Mega-Linter WILL enforce it; green gate != green CI for copy-paste (issue #143).{NC}")
        else:
            cp = subprocess.run(base_cmd + ["."], cwd=str(root), timeout=300).returncode
            if cp != 0:
                cprint(f"  {RED}BLOCKED: jscpd found copy-paste duplication over the .jscpd.json threshold{NC}")
                cprint(f"  {RED}(parity with CI Mega-Linter). Reduce duplication or raise the threshold in .jscpd.json.{NC}")
                return 1
            cprint(f"  {GREEN}Copy-paste check passed.{NC}")

    # Gate 2c: Workflow-syntax lint (actionlint) — PARITY with ci.yml Lint job.
    # CI runs actionlint on .github/workflows/*; surface a workflow-syntax error
    # locally BEFORE the bump/tag/push. actionlint is a single static binary; if it
    # is not on PATH, DEGRADE to a non-blocking WARNING (CI still enforces it) — a
    # green gate then does NOT guarantee green CI for the workflow-syntax dimension.
    # NEVER false-block a push on a missing-tool case (the issue #143 pattern).
    cprint(f"\n{BLUE}[G2c] Workflow lint (actionlint, parity with CI)...{NC}")
    wf_dir = root / ".github" / "workflows"
    has_workflows = wf_dir.is_dir() and (any(wf_dir.glob("*.yml")) or any(wf_dir.glob("*.yaml")))
    actionlint_bin = shutil.which("actionlint")
    if not has_workflows:
        cprint(f"  {GREEN}No workflows to lint — skipped.{NC}")
    elif actionlint_bin is None:
        cprint(f"  {YELLOW}WARNING: actionlint not found — workflow lint SKIPPED locally.{NC}")
        cprint(f"  {YELLOW}CI's Lint job WILL enforce it. A green gate does NOT guarantee green CI")
        cprint(f"  {YELLOW}for the workflow-syntax dimension. Install actionlint for full parity.{NC}")
    else:
        al = subprocess.run([actionlint_bin], cwd=str(root), timeout=120).returncode
        if al != 0:
            cprint(f"  {RED}BLOCKED: actionlint found workflow-syntax errors (parity with CI Lint job).{NC}")
            return 1
        cprint(f"  {GREEN}Workflow lint passed.{NC}")

    # Gate 2d: Static type-check (mypy) — PARITY with ci.yml Lint job
    # (`uv run mypy scripts/ --ignore-missing-imports`). Surface a type error
    # locally BEFORE the bump/tag/push. A `--version` probe distinguishes
    # 'mypy unavailable' (WARN + skip, never false-block) from 'mypy ran, found
    # errors' (BLOCK) — the issue #143 degrade-gracefully pattern.
    cprint(f"\n{BLUE}[G2d] Type-check (mypy, parity with CI)...{NC}")
    mypy_bin = shutil.which("mypy")
    mypy_cmd = [mypy_bin] if mypy_bin else (["uv", "run", "mypy"] if shutil.which("uv") else None)
    if mypy_cmd is None:
        cprint(f"  {YELLOW}WARNING: mypy/uv not found — type-check SKIPPED locally.{NC}")
        cprint(f"  {YELLOW}CI's Lint job WILL enforce it; a green gate does NOT guarantee green CI for types.{NC}")
    else:
        probe = subprocess.run(mypy_cmd + ["--version"], cwd=str(root),
                               capture_output=True, text=True, timeout=120)
        if probe.returncode != 0:
            cprint(f"  {YELLOW}WARNING: mypy could not run — type-check SKIPPED locally.{NC}")
            cprint(f"  {YELLOW}CI's Lint job WILL enforce it; green gate != green CI for types.{NC}")
        else:
            mt = subprocess.run(mypy_cmd + ["scripts/", "--ignore-missing-imports"],
                                cwd=str(root), timeout=300).returncode
            if mt != 0:
                cprint(f"  {RED}BLOCKED: mypy found type errors in scripts/ (parity with CI Lint job).{NC}")
                return 1
            cprint(f"  {GREEN}Type-check passed.{NC}")

    # Gate 2e: compiled-component build gates (Rust/Go/.NET/Swift/Zig) -- issue #175.
    # Self-detecting + table-driven: for each language, glob its manifest in the tree
    # (a checked-out build-source submodule, or an in-tree component). No manifest ->
    # clean skip. Manifest present but toolchain absent -> WARN+skip (CI / the build
    # pipeline backstops it); toolchain ran + a command failed -> BLOCK. Mirrors the
    # G2b/G2c/G2d degrade-if-absent idiom so a missing toolchain never false-blocks a
    # push. Only Rust + Go get a test command (cargo test / go test are no-ops on zero
    # tests); the others are build-only (their test runners error on "no tests", which
    # would be a false block) -- CI runs the full per-language test matrix.
    cprint(f"\n{BLUE}[G2e] Compiled-component build gates (issue #175)...{NC}")
    _compiled_skip = {"target", ".git", "node_modules", ".venv", "vendor",
                      "dist", "build", "obj", "zig-out", "zig-cache", ".zig-cache"}

    def _find_manifests(pattern):
        found = [
            m for m in root.rglob(pattern)
            if not any(part in _compiled_skip for part in m.relative_to(root).parts)
        ]
        # Keep only top-level manifests: a workspace/module root covers its members, so
        # a nested manifest inside another matched manifest dir is not run standalone.
        return [m for m in found if not any(o is not m and o.parent in m.parents for o in found)]

    # (label, manifest glob, toolchain, builder(manifest) -> [(cmd, cwd), ...])
    _compiled_gates = [
        ("Rust", "Cargo.toml", "cargo", lambda m: [
            (["cargo", "clippy", "--manifest-path", str(m), "--all-targets", "--", "-D", "warnings"], str(root)),
            (["cargo", "test", "--manifest-path", str(m)], str(root)),
        ]),
        ("Go", "go.mod", "go", lambda m: [
            (["go", "vet", "./..."], str(m.parent)),
            (["go", "build", "./..."], str(m.parent)),
            (["go", "test", "./..."], str(m.parent)),
        ]),
        ("C#/.NET", "*.csproj", "dotnet", lambda m: [
            (["dotnet", "build", str(m)], str(root)),
        ]),
        ("Swift", "Package.swift", "swift", lambda m: [
            (["swift", "build"], str(m.parent)),
        ]),
        ("Zig", "build.zig", "zig", lambda m: [
            (["zig", "build"], str(m.parent)),
        ]),
    ]
    _saw_compiled = False
    for _label, _pattern, _tool, _builder in _compiled_gates:
        _manifests = _find_manifests(_pattern)
        if not _manifests:
            continue
        _saw_compiled = True
        if shutil.which(_tool) is None:
            cprint(f"  {YELLOW}WARNING: {_label} component(s) present but `{_tool}` not found -- {_label} build SKIPPED locally.{NC}")
            cprint(f"  {YELLOW}CI / the build pipeline WILL build it; a green gate does NOT guarantee green CI for {_label}.{NC}")
            continue
        for _manifest in _manifests:
            _rel = _manifest.relative_to(root)
            for _cmd, _cwd in _builder(_manifest):
                _shown = " ".join(_cmd)
                try:
                    _rc = subprocess.run(_cmd, cwd=_cwd, timeout=1200).returncode
                except subprocess.TimeoutExpired:
                    cprint(f"  {YELLOW}WARNING: `{_shown}` timed out (>1200s) for {_rel} -- SKIPPED locally; CI backstops.{NC}")
                    break
                if _rc != 0:
                    cprint(f"  {RED}BLOCKED: `{_shown}` failed for {_rel}.{NC}")
                    return 1
        cprint(f"  {GREEN}{_label} build passed ({len(_manifests)} component(s)).{NC}")

    # C/C++: build systems are non-uniform (CMake/Make/Meson/Autotools/Bazel...) with no
    # single false-block-safe local command, and a build depends on system libraries the
    # author machine may lack. So the local gate DETECTS + NOTES (never blocks) -- the
    # plugin build workflow (controlled toolchain) is the authoritative C/C++ builder, and
    # RC-SHIP-BINARY-ONLY (in the remote validator, G3) still enforces the ship-only-binary
    # canon for C/C++.
    def _tree_has(pattern):
        return any(
            not any(part in _compiled_skip for part in p.relative_to(root).parts)
            for p in root.rglob(pattern)
        )

    _cxx_src = any(_tree_has(x) for x in ("*.c", "*.cc", "*.cpp", "*.cxx"))
    _cxx_build = any(_tree_has(x) for x in ("CMakeLists.txt", "Makefile", "meson.build", "configure.ac"))
    if _cxx_src and _cxx_build:
        _saw_compiled = True
        cprint(f"  {YELLOW}NOTE: C/C++ component detected -- its build is authoritative in CI (controlled")
        cprint(f"  {YELLOW}toolchain); the local gate skips non-uniform C/C++ build systems to avoid a")
        cprint(f"  {YELLOW}false block on a missing system dependency. RC-SHIP-BINARY-ONLY still applies.{NC}")

    if not _saw_compiled:
        cprint(f"  {GREEN}No compiled component (Rust/Go/C/C++/.NET/Swift/Zig) -- skipped.{NC}")

    # Gate 2f: Shell lint (shellcheck) -- issue #175.
    # Self-detecting: runs ONLY when the plugin ships shell scripts (*.sh / *.bash).
    # No shell -> skip. Shell present but `shellcheck` absent -> WARN+skip (CI's
    # Mega-Linter BASH_SHELLCHECK backstops); shellcheck ran + found issues -> BLOCK.
    cprint(f"\n{BLUE}[G2f] Shell lint (shellcheck, issue #175)...{NC}")
    _shell_scripts = [
        s for s in list(root.rglob("*.sh")) + list(root.rglob("*.bash"))
        if not any(part in _compiled_skip for part in s.relative_to(root).parts)
    ]
    if not _shell_scripts:
        cprint(f"  {GREEN}No shell scripts -- skipped.{NC}")
    elif shutil.which("shellcheck") is None:
        cprint(f"  {YELLOW}WARNING: shell scripts present but `shellcheck` not found -- shell lint SKIPPED locally.{NC}")
        cprint(f"  {YELLOW}CI's Mega-Linter (BASH_SHELLCHECK) WILL enforce it; green gate != green CI for shell.{NC}")
    else:
        sc = subprocess.run(
            ["shellcheck", *[str(s) for s in sorted(_shell_scripts)]],
            cwd=str(root), timeout=180).returncode
        if sc != 0:
            cprint(f"  {RED}BLOCKED: shellcheck found issues (parity with CI Mega-Linter BASH_SHELLCHECK).{NC}")
            return 1
        cprint(f"  {GREEN}Shell lint passed ({len(_shell_scripts)} script(s)).{NC}")

    # Gate 3: Validate via REMOTE CPV validator. MANDATORY — no skip, no exceptions.
    # CORNERSTONE: a plugin cannot be pushed unless validation passes with 0
    # blocking issues (WARNING allowed). The validator is ALWAYS fetched from
    # GitHub so a tampered local copy cannot weaken the rules.
    cprint(f"\n{BLUE}[G3] Validating plugin (remote CPV)...{NC}")
    if not shutil.which("uvx"):
        cprint(f"  {RED}BLOCKED: uvx not found on PATH.{NC}")
        return 1
    try:
        ve = subprocess.run(
            ["uvx", "--from",
             "git+https://github.com/Emasoft/claude-plugins-validation@v5.17.0",
             "--with", "pyyaml",
             "cpv-remote-validate", "plugin", ".", "--strict"],
            cwd=str(root), timeout=_CPV_TIMEOUT_SEC).returncode
    except subprocess.TimeoutExpired:
        cprint(f"  {RED}BLOCKED: the validator timed out after {_CPV_TIMEOUT_SEC:g}s "
               f"— it did not finish, so the verdict is UNKNOWN (not clean).{NC}")
        return 1
    # FAIL-CLOSED (audit row 1). CPV's verdict vocabulary is 0..4 (0=pass,
    # 1=CRITICAL, 2=MAJOR, 3=MINOR, 4=NIT); WARNING never gets its own exit
    # code. So anything >= 5 is not a verdict at all — it is the validator
    # FAILING TO RUN (127 = entrypoint missing, 137 = OOM-kill, ...). The old
    # `ve != 0 and ve < 5` test let every one of those fall through and print
    # "Validation passed", which is the exact RC-8 fail-open the CI workflows
    # were fixed for and this gate was not.
    if ve == 0:
        cprint(f"  {GREEN}Validation passed (0 blocking issues).{NC}")
    elif ve < 5:
        labels = {1: "CRITICAL", 2: "MAJOR", 3: "MINOR", 4: "NIT"}
        cprint(f"  {RED}BLOCKED: {labels[ve]} issues found{NC}")
        return 1
    else:
        cprint(f"  {RED}BLOCKED: the validator FAILED TO RUN (exit {ve}).{NC}")
        cprint(f"  {RED}That is outside CPV's 0-4 verdict range, so nothing was"
               f" validated — UNKNOWN is not clean.{NC}")
        return 1

    # Gate 3s: Secret scan. A leak BLOCKS the release. Body lives in
    # `_secret_scan` because it is ALSO a pipeline stage (audit row 6): a plugin
    # that never ran --install-hook (or whose core.hooksPath was reset) would
    # otherwise publish with zero secret scanning.
    cprint(f"\n{BLUE}[G3s] Secret scan (trufflehog)...{NC}")
    if _secret_scan(root) != 0:
        return 1

    # Gate 4: Tests. MANDATORY — missing tests/ dir or zero tests is a BLOCK.
    cprint(f"\n{BLUE}[G4] Running tests...{NC}")
    test_dir = root / "tests"
    # Issue #215 — rglob, not glob. `tests/unit/test_*.py` is a layout pytest
    # collects happily, and a top-level-only probe read it as "no tests" and
    # blocked the push mid-way on a repo whose 205-test suite was green. The
    # zero-tests block is unweakened: pytest's exit 5 still catches an empty
    # collection below.
    if not (test_dir.is_dir() and any(test_dir.rglob("test_*.py"))):
        cprint(f"  {RED}BLOCKED: tests/ directory missing or empty.{NC}")
        cprint(f"  {RED}Every CPV plugin MUST ship tests.{NC}")
        return 1
    suite_timeout = _test_suite_timeout()
    try:
        te = subprocess.run(
            ["uv", "run", "pytest", "tests/", "-x", "-q", "--tb=short"],
            cwd=str(root), timeout=suite_timeout).returncode
    except subprocess.TimeoutExpired:
        cprint(f"  {RED}BLOCKED: Tests timed out after {suite_timeout:g}s.{NC}")
        cprint(f"  {RED}If the suite is legitimately longer, raise "
               f"{_TEST_SUITE_TIMEOUT_ENV} — do not trim or skip tests to fit.{NC}")
        return 1
    if te == 5:
        cprint(f"  {RED}BLOCKED: pytest collected 0 tests.{NC}")
        return 1
    if te != 0:
        cprint(f"  {RED}BLOCKED: Tests failed{NC}")
        return 1
    cprint(f"  {GREEN}Tests passed.{NC}")

    # Gate 4b: Linux fork-parity probe. Body lives in `_fork_parity_probe`
    # because it is ALSO a pipeline stage (audit row 14) — with hooks
    # uninstalled the gate copy never runs, and CPV's own publish.py has had it
    # as a stage all along.
    cprint(f"\n{BLUE}[G4b] Linux fork-parity probe...{NC}")
    if _fork_parity_probe(root, suite_timeout) != 0:
        return 1

    cprint(f"\n{GREEN}{BOLD}All gates passed.{NC}")
    return 0


# -- Pipeline stages -----------------------------------------------------------

def stage_bypass_guard() -> None:
    """Step 1: Reject any env var that could bypass a check. No exceptions.

    Issue #22 hardening (v2.86.0): broadened from a fixed allowlist to
    prefix-pattern matching. Any env var matching ``PLUGIN_SKIP_*``,
    ``CPV_SKIP_*``, ``SKIP_*``, or named ``NO_VERIFY`` aborts the publish.
    Closes the loophole where a fresh skip name (e.g. ``CPV_SKIP_GATE7``)
    that was not in the original explicit list would silently slip past.

    Explicit infrastructure exemptions remain — all are read-only
    overrides used by CPV's own integrity / auth subsystems and never
    skip a gate:
        * ``PLUGIN_SKIP_GITHUB_INTEGRITY=1`` — bypasses the GitHub-anchored
          integrity check (see the hash-verify module). That check is a
          defence against tampering, NOT a publish gate.
        * ``CPV_SKIP_GITHUB_INTEGRITY=1`` — the LEGACY spelling of the same
          override, still honoured (deprecated, TRDD-bbff5bc5).
        * ``CPV_SKIP_GH_AUTH_CHECK=1`` — used by `_ensure_gh_auth` to bypass
          the `gh auth status` round-trip on flaky networks. Auth still
          has to work for the actual `git push` / `gh release create`;
          this only skips the precheck.
        * ``PLUGIN_SKIP_INSTALL_SMOKE=1`` — read and documented by
          `stage_install_smoke`, which runs POST-RELEASE and can never fail the
          publish. Without this entry the documented flag aborted the run here
          instead (audit row 5), so the flag was dead code.

    The ``PLUGIN_`` spelling MUST be exempt: the hash-verify module renamed
    the var and instructs users to export it, yet ``PLUGIN_SKIP_`` is a
    forbidden PREFIX here — so without this entry, following that module's
    own deprecation notice aborts the publish as a "bypass attempt". It
    grants NO new capability (the same override is already exempt under its
    legacy name). There is deliberately no ``PLUGIN_SKIP_GH_AUTH_CHECK``
    entry — no such var exists, and exempting a name nothing reads would
    widen the bypass surface for nothing.

    All are documented exemptions, listed below and excluded from the
    pattern match.
    """
    cprint(f"\n{BOLD}[1/15] Checking for bypass attempts...{NC}")
    # Explicit infrastructure exemptions — see docstring above.
    exemptions = {
        "PLUGIN_SKIP_GITHUB_INTEGRITY",
        "CPV_SKIP_GITHUB_INTEGRITY",
        "CPV_SKIP_GH_AUTH_CHECK",
        # `stage_install_smoke` reads this and documents it (audit row 5).
        # Without the exemption, setting the documented flag ABORTED the publish
        # at stage 1 instead of skipping the post-release smoke test — a dead
        # code path. It skips nothing that gates the release: the smoke test
        # runs AFTER the release is already public and can never fail it.
        "PLUGIN_SKIP_INSTALL_SMOKE",
    }
    forbidden_prefixes = ("PLUGIN_SKIP_", "CPV_SKIP_", "SKIP_")
    forbidden_exact = {"NO_VERIFY"}
    attempted = [
        v
        for v in sorted(os.environ)
        if (v.startswith(forbidden_prefixes) or v in forbidden_exact) and v not in exemptions
        if os.environ.get(v)
    ]
    if attempted:
        cprint(f"  {RED}BLOCKED: forbidden env vars set: {', '.join(attempted)}{NC}")
        cprint(f"  {RED}The publish pipeline enforces every check. "
               f"Fix failures, do not skip them.{NC}")
        cprint(f"  {DIM}(infrastructure exemptions: {', '.join(sorted(exemptions))}){NC}")
        sys.exit(1)
    cprint(f"  {GREEN}No bypass vars set.{NC}")

def stage_check_clean(root: Path) -> None:
    """Step 2: Working tree must be clean, apart from a lone re-resolved uv.lock.

    THE uv.lock CARVE-OUT (audit row 26, CPV issue #149). The outer `uv run` that
    launched this pipeline can re-resolve `uv.lock` before publish.py's first
    line executes, so the tree is dirty through no fault of the author, on a file
    the pipeline itself rewrites two stages later. Aborting on it makes the
    publish unrunnable exactly when everything is correct. The carve-out is as
    narrow as it can be: `uv.lock` ALONE and nothing else, auto-committed and
    reported. Any other dirty path — including uv.lock beside a second file —
    still aborts.

    It keys on the porcelain STATUS CODE, not the path alone. `ln[3:]` by itself
    cannot tell ` M uv.lock` (modified — the case above) from `?? uv.lock`
    (UNTRACKED) or `UU uv.lock` (merge conflict), and auto-committing either of
    those is wrong: adding an untracked file here contradicts the #186
    never-sweep-untracked rule this same file enforces at the commit stage, and
    committing a conflicted lockfile is worse. Renames (`R  old -> new`) and
    quoted paths already fail closed — neither string equals `uv.lock`.
    """
    cprint(f"\n{BOLD}[2/15] Checking working tree...{NC}")
    r = run(["git", "status", "--porcelain"], cwd=root, capture=True)
    dirty = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if (len(dirty) == 1 and dirty[0][:2] in (" M", "M ", "MM")
            and dirty[0][3:].strip() == "uv.lock"):
        cprint(f"  {YELLOW}Only uv.lock is dirty (the launching `uv run` re-resolved "
               f"it) — auto-committing it (issue #149).{NC}")
        run(["git", "add", "--", "uv.lock"], cwd=root)
        run(["git", "commit", "-m", "chore: re-resolve uv.lock"], cwd=root)
        dirty = []
    if dirty:
        cprint(f"  {RED}Working tree is dirty. Commit or stash changes first.{NC}")
        cprint(r.stdout)
        sys.exit(1)
    cprint(f"  {GREEN}Clean.{NC}")

def stage_lint(root: Path) -> None:
    """Step 3: Lint + typecheck (ruff + mypy). MANDATORY — no skip.

    Runs ruff for style/syntax and mypy for static types in the same stage.
    Both must succeed — the cornerstone rule forbids any push with lint or
    type errors. Type-checking runs BEFORE the test suite so the cheap fails
    come before the expensive ones.
    """
    cprint(f"\n{BOLD}[3/15] Linting + type-checking...{NC}")
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        cprint(f"  {RED}BLOCKED: scripts/ directory missing — cannot lint.{NC}")
        sys.exit(1)
    cprint(f"  {BLUE}ruff check scripts/{NC}")
    run(["uv", "run", "ruff", "check", "scripts/"], cwd=root)
    cprint(f"  {BLUE}mypy scripts/ --ignore-missing-imports{NC}")
    run(["uv", "run", "mypy", "scripts/", "--ignore-missing-imports"], cwd=root)
    cprint(f"  {GREEN}Lint + typecheck passed.{NC}")

# Issue #31 (v2.98.0): browser-orphan cleanup signatures.
#
# A pytest run that spawns Playwright / dev-browser pages can leave
# behind dozens of `Chrome for Testing` / `chromium` / `headless_shell`
# processes if the test code (or fixtures) forget to close pages. Over
# a long debug session those orphans pile up, exhausting file
# descriptors or RAM and eventually crashing the browser or making
# the machine unresponsive. The baseline-diff cleanup below catches
# every leak regardless of test-code quality. NEVER skips tests — the
# iron rule (no plugin with issues pushed) is preserved.
_BROWSER_ORPHAN_SIGNATURES = (
    "Chrome for Testing",
    "chrome-for-testing",
    "headless_shell",
    "Chromium.app/Contents",
    "chromium-browser",
    "/playwright/",
    "playwright-core",
)


def _ps_table() -> dict:
    """Snapshot the process table as {pid: (pgid, command)}. {} when unreadable.

    Snapshot-then-grep — never live-grep: a `ps | grep <pattern>` pipeline finds
    its own shell as a false positive, because that shell has the pattern in its
    own argv.
    """
    try:
        snap = subprocess.run(
            ["ps", "-eo", "pid,pgid,command"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if snap.returncode != 0 or not snap.stdout:
        return {}
    table = {}
    for raw_line in snap.stdout.strip().split("\n")[1:]:
        parts = raw_line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2])
        except ValueError:
            continue
    return table


def _snapshot_browser_pids() -> set:
    """Browser-signature PIDs currently alive."""
    return {
        pid for pid, (_pgid, cmd) in _ps_table().items()
        if any(sig in cmd for sig in _BROWSER_ORPHAN_SIGNATURES)
    }


def _cleanup_browser_orphans(baseline_pids: set, owner_pgid: int | None = None) -> int:
    """Kill browser-signature PIDs this pytest run actually spawned.

    TWO conditions, both required (audit row 20). Baseline-diff alone was not
    enough: a PID that merely APPEARED during the window can just as easily be a
    browser the maintainer opened by hand mid-run, and killing that is a real
    (and very confusing) loss — appearing-since-baseline does not imply
    ownership.

    * NOT in `baseline_pids` — pre-existing processes are never candidates; and
    * in the PROCESS GROUP that ran pytest. pytest is a child of this process
      and every browser it spawns inherits the group, while a browser opened
      from a GUI or another terminal is in a different session entirely. The
      PPID chain is NOT usable here: once pytest exits its orphans are
      reparented to init and the chain to pytest is gone — the pgid survives
      exactly that.

    `owner_pgid=None` disables the cleanup rather than falling back to the
    unsafe global diff: when ownership cannot be established, killing nothing is
    the correct answer.
    """
    import signal
    import time

    if owner_pgid is None:
        return 0
    new_pids = {
        pid for pid, (pgid, cmd) in _ps_table().items()
        if pid not in baseline_pids
        and pgid == owner_pgid
        and any(sig in cmd for sig in _BROWSER_ORPHAN_SIGNATURES)
    }
    if not new_pids:
        return 0
    killed = 0
    for pid in new_pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if killed:
        time.sleep(1.5)
        for pid in new_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    return killed


def stage_tests(root: Path) -> None:
    """Step 4: Run pytest. MANDATORY — no skip, no exceptions.

    Cornerstone rule: failing tests block the push. Missing tests/ directory
    is a scaffolding bug and must be fixed, not bypassed.

    Order: tests run BEFORE the CPV validator so behavioral regressions fail
    fast on unit tests before the structural validator inspects the manifest.

    Issue #31 (v2.98.0): wrap the pytest invocation in a baseline-diff
    browser-orphan cleanup so dev-browser / Playwright leaks do not
    pile up Chrome-for-Testing processes. Tests still run
    unconditionally — the cleanup is a safety net, not a skip
    mechanism.
    """
    cprint(f"\n{BOLD}[4/15] Running tests...{NC}")
    test_dir = root / "tests"
    if not test_dir.is_dir():
        cprint(f"  {RED}BLOCKED: tests/ directory missing.{NC}")
        cprint(f"  {RED}Every CPV plugin MUST ship a tests/ directory.{NC}")
        sys.exit(1)
    baseline_browser_pids = _snapshot_browser_pids()
    # pytest is a child of this process, so it and everything it spawns inherit
    # THIS process group — which is what lets the cleanup below prove ownership
    # (audit row 20) instead of killing anything that merely appeared during the
    # window. A browser the maintainer opened by hand lives in a different
    # session and a different group, so it is now structurally out of range.
    owner_pgid = os.getpgrp()
    try:
        r = run(["uv", "run", "pytest", "tests/", "-x", "-q", "--tb=short"], cwd=root,
                check=False, timeout=_test_suite_timeout())
    finally:
        killed = _cleanup_browser_orphans(baseline_browser_pids, owner_pgid)
        if killed:
            cprint(f"  {YELLOW}Cleaned up {killed} orphaned browser process(es) spawned by pytest.{NC}")
    if r.returncode == 5:
        # pytest exit 5 = no tests collected. This is ALSO a block — no exceptions.
        cprint(f"  {RED}BLOCKED: pytest collected 0 tests.{NC}")
        cprint(f"  {RED}Every CPV plugin MUST ship at least one test.{NC}")
        sys.exit(1)
    if r.returncode != 0:
        cprint(f"  {RED}BLOCKED: tests failed (exit {r.returncode}).{NC}")
        sys.exit(r.returncode)
    cprint(f"  {GREEN}Tests passed.{NC}")


def stage_validate(root: Path) -> None:
    """Step 5: Validate plugin via REMOTE CPV validator. MANDATORY — no skip.

    Cornerstone rule: a plugin cannot be pushed unless validation passes
    with 0 issues (WARNING allowed). The validator is ALWAYS fetched from
    GitHub (git+https://github.com/Emasoft/claude-plugins-validation@v5.17.0) via
    uvx so a local tampered copy cannot weaken the rules. No exceptions.

    Order: runs AFTER lint + tests so behavioral regressions fail fast
    before the structural validator even looks at the manifest.
    """
    cprint(f"\n{BOLD}[5/15] Validating plugin (remote CPV)...{NC}")
    if not shutil.which("uvx"):
        cprint(f"  {RED}BLOCKED: uvx not found on PATH.{NC}")
        cprint(f"  {RED}Install via: brew install uv  or  pip install uv{NC}")
        sys.exit(1)
    # Fetch CPV from GitHub and run validate_plugin remotely. --strict blocks
    # on CRITICAL(1), MAJOR(2), MINOR(3), NIT(4); WARNING(5+) passes.
    #
    # The budget is the SHARED `_CPV_TIMEOUT_SEC` (audit row 4). This call site
    # used to inherit `run()`'s 300s default while gate G3 gave the IDENTICAL
    # command 600s — three budgets for one command, with the tightest one on the
    # publish path.
    run([
        "uvx", "--from",
        "git+https://github.com/Emasoft/claude-plugins-validation@v5.17.0",
        "--with", "pyyaml",
        "cpv-remote-validate", "plugin", ".", "--strict",
    ], cwd=root, timeout=_CPV_TIMEOUT_SEC)
    cprint(f"  {GREEN}Validation passed (0 blocking issues).{NC}")


def stage_secret_scan(root: Path) -> None:
    """Step 6: Secret scan (trufflehog). MANDATORY — no skip.

    A PIPELINE stage, not only a gate stage (audit row 6). The gate copy runs
    from the pre-push hook; a plugin that never ran `--install-hook`, or whose
    `core.hooksPath` was reset, would otherwise publish with zero secret
    scanning. Runs BEFORE the bump/commit/tag/push, so a detected credential
    aborts with the tree untouched — a gate that fired after the push could not
    un-publish anything.
    """
    cprint(f"\n{BOLD}[6/15] Secret scan (trufflehog)...{NC}")
    if _secret_scan(root) != 0:
        sys.exit(1)


def stage_fork_parity(root: Path) -> None:
    """Step 7: Linux fork-parity probe. Self-detecting; blocks on a real failure.

    A PIPELINE stage for the same reason as `stage_secret_scan` (audit row 14):
    with hooks uninstalled the G4b gate copy never runs at all.

    On a plugin with process pools the suite therefore runs up to FOUR times per
    publish (stage_tests, stage_fork_parity, and the pre-push hook's G4 + G4b);
    plugins without pools self-detect and skip.
    """
    cprint(f"\n{BOLD}[7/15] Linux fork-parity probe...{NC}")
    if _fork_parity_probe(root, _fork_parity_timeout()) != 0:
        sys.exit(1)


def stage_ci_preflight(root: Path) -> None:
    """Step 8: CI-parity preflight via REMOTE CPV. MANDATORY — no skip.

    WHY THIS STAGE EXISTS. `validate_plugin --strict` (stage 4) does NOT run the
    gates this plugin's own GitHub-CI Lint job runs: jscpd copy-paste, actionlint,
    mypy, the `uv sync --extra dev` resolve, the enabled Mega-Linter sub-linters,
    and CPV's static CI-parity defect detectors. Without this stage a publish
    passes every LOCAL gate, bumps the version, commits, TAGS, PUSHES, and cuts a
    GitHub release — and only THEN goes red on GitHub, with the broken pipeline
    already shipped to everyone who installs the plugin.

    PLACEMENT IS LOAD-BEARING: this runs BEFORE stage_bump / stage_commit_and_push
    / stage_gh_release, so a parity failure aborts with the working tree untouched
    instead of leaving a half-published release behind.

    A MISSING TOOL NEVER BLOCKS THE PUBLISH. `ci-preflight` exits non-zero ONLY
    when a gate actually FAILED; every tool-absent case (no npx, no actionlint,
    no checkov, ...) degrades to a non-blocking WARNING and still exits 0. So a
    lean machine publishes exactly as before — it just gets less LOCAL coverage,
    which CI still enforces. Do not "harden" this into a hard tool requirement.
    """
    cprint(f"\n{BOLD}[8/15] CI-parity preflight (remote CPV)...{NC}")
    if not shutil.which("uvx"):
        cprint(f"  {RED}BLOCKED: uvx not found on PATH.{NC}")
        cprint(f"  {RED}Install via: brew install uv  or  pip install uv{NC}")
        sys.exit(1)
    # Explicit timeout (audit row 3): without one a stalled uvx or a network
    # hiccup hung the publish forever with the tree already lint/test/
    # validate-clean. A timeout BLOCKS, as `run()` does — an unfinished preflight
    # is not a passed preflight.
    try:
        rc = subprocess.run([
            "uvx", "--from",
            "git+https://github.com/Emasoft/claude-plugins-validation@v5.17.0",
            "--with", "pyyaml",
            "cpv-remote-validate", "ci-preflight", ".",
        ], cwd=str(root), timeout=_CPV_TIMEOUT_SEC).returncode
    except subprocess.TimeoutExpired:
        cprint(f"  {RED}BLOCKED: CI-parity preflight timed out after "
               f"{_CPV_TIMEOUT_SEC:g}s — it did not finish, so nothing was checked.{NC}")
        sys.exit(1)
    if rc != 0:
        cprint(f"  {RED}BLOCKED: CI-parity preflight FAILED.{NC}")
        cprint(f"  {RED}The gates listed above would fail GitHub CI — and without this{NC}")
        cprint(f"  {RED}stage they would only have failed AFTER the tag and release were{NC}")
        cprint(f"  {RED}pushed. Fix the causes, then re-run publish.py.{NC}")
        sys.exit(1)
    cprint(f"  {GREEN}CI-parity preflight passed.{NC}")


# ── Marketplace-registration helpers (mirror of CPV's own publish.py Gate 6) ─

def _find_parent_marketplace(plugin_root: Path) -> Path | None:
    """Walk up looking for a parent marketplace.json (Layout B signature)."""
    current = plugin_root.resolve().parent
    while current != current.parent:
        mp = current / ".claude-plugin" / "marketplace.json"
        if mp.is_file():
            try:
                rel = plugin_root.resolve().relative_to(current)
                parts = rel.parts
                if len(parts) >= 2 and parts[0] == "plugins":
                    return current
            except ValueError:
                pass
            return None
        current = current.parent
    return None


def _detect_layout(plugin_root: Path) -> tuple[str, dict]:
    """Detect Layout A (standalone+notify), Layout B (nested), or 'none'."""
    parent = _find_parent_marketplace(plugin_root)
    if parent is not None:
        return "B", {"marketplace_root": parent, "plugin_name": plugin_root.name}
    notify_wf = plugin_root / ".github" / "workflows" / "notify-marketplace.yml"
    if notify_wf.is_file():
        try:
            content = notify_wf.read_text(encoding="utf-8")
        except OSError:
            content = ""
        m_owner = re.search(r"^\s*MARKETPLACE_OWNER:\s*[\"']?([^\"'\s]+)[\"']?\s*$", content, re.MULTILINE)
        m_repo = re.search(r"^\s*MARKETPLACE_REPO:\s*[\"']?([^\"'\s]+)[\"']?\s*$", content, re.MULTILINE)
        return "A", {
            "notify_workflow": notify_wf,
            "mkt_owner": m_owner.group(1) if m_owner else None,
            "mkt_repo": m_repo.group(1) if m_repo else None,
        }
    return "none", {}


def _gh_secret_exists(plugin_root: Path, secret_name: str) -> bool:
    """Check whether a GitHub secret with the given name exists on this repo."""
    gh = shutil.which("gh")
    if gh is None:
        return False
    r = subprocess.run([gh, "secret", "list"], cwd=str(plugin_root),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        if line.split("\t", 1)[0].strip() == secret_name:
            return True
    return False


def _current_repo_slug(plugin_root: Path) -> str | None:
    """Return owner/repo slug for current git origin, or None."""
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(plugin_root),
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", r.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _read_plugin_name(plugin_root: Path) -> str:
    pj = plugin_root / ".claude-plugin" / "plugin.json"
    if pj.is_file():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            name = data.get("name")
            if isinstance(name, str) and name:
                return name
        except (OSError, json.JSONDecodeError):
            pass
    return plugin_root.name


def _fetch_remote_marketplace_json(owner: str, repo: str) -> dict | None:
    gh = shutil.which("gh")
    if gh is None:
        return None
    r = subprocess.run(
        [gh, "api", f"repos/{owner}/{repo}/contents/.claude-plugin/marketplace.json",
         "-H", "Accept: application/vnd.github.raw+json"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# Aggregate bound for the per-workflow-file receiver probe below (audit row 19).
# Each `gh api` call was individually bounded at 60s with NO overall ceiling, so
# a marketplace with many workflow files could spend minutes inside what the
# pipeline presents as one quick "check".
_RECEIVER_PROBE_MAX_FILES = 25
_RECEIVER_PROBE_DEADLINE_S = 90.0


def _remote_has_receiver_workflow(owner: str, repo: str) -> bool:
    gh = shutil.which("gh")
    if gh is None:
        return False
    r = subprocess.run(
        [gh, "api", f"repos/{owner}/{repo}/contents/.github/workflows"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return False
    try:
        entries = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    if not isinstance(entries, list):
        return False
    deadline = time.monotonic() + _RECEIVER_PROBE_DEADLINE_S
    checked = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.endswith((".yml", ".yaml")):
            continue
        if checked >= _RECEIVER_PROBE_MAX_FILES or time.monotonic() >= deadline:
            # Out of budget. Returning False is the conservative direction here:
            # the caller reports "no receiver workflow found", a WARNING that
            # asks the maintainer to look, rather than claiming one exists.
            break
        checked += 1
        f = subprocess.run(
            [gh, "api", f"repos/{owner}/{repo}/contents/.github/workflows/{name}",
             "-H", "Accept: application/vnd.github.raw+json"],
            capture_output=True, text=True, timeout=60,
        )
        if f.returncode == 0 and "repository_dispatch" in f.stdout:
            return True
    return False


def _plugin_in_remote_marketplace(mkt_json: dict, plugin_name: str, expected_repo: str | None) -> bool:
    """Accept github/url/git source forms; match URL slug for url|git (issue #25 Defect A)."""
    plugins = mkt_json.get("plugins")
    if not isinstance(plugins, list):
        return False
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != plugin_name:
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        stype = source.get("source") or source.get("type")
        if stype == "github":
            if expected_repo is None or source.get("repo") == expected_repo:
                return True
        elif stype in ("url", "git"):
            url = source.get("url")
            if expected_repo is None:
                return True
            if isinstance(url, str):
                norm = url.removesuffix(".git").rstrip("/")
                if norm.endswith("/" + expected_repo) or norm.endswith(":" + expected_repo):
                    return True
    return False


def stage_marketplace_registration(root: Path) -> None:
    """Step 9: Verify the plugin is wired to its marketplace for auto-updates.

    Mirror of CPV's own publish.py Gate 6. Three modes:
      - Layout A (standalone + notify-marketplace.yml): verifies workflow,
        MARKETPLACE_PAT secret, remote marketplace.json registration,
        remote receiver workflow with repository_dispatch trigger
      - Layout B (nested under <marketplace>/plugins/<name>/): refuses to
        publish from the nested folder, requires running at marketplace root
      - 'none' (no marketplace wiring): emits a WARNING and proceeds — valid
        for first releases or experimental standalone plugins
    """
    cprint(f"\n{BOLD}[9/15] Marketplace-registration check...{NC}")
    layout, details = _detect_layout(root)

    if layout == "none":
        cprint(f"  {YELLOW}WARNING: no marketplace registration found for this plugin.{NC}")
        cprint(f"  {YELLOW}If you intend to publish to a marketplace, run the{NC}")
        cprint(f"  {YELLOW}cpv-setup-marketplace-auto-notification skill to wire up auto-updates.{NC}")
        cprint(f"  {YELLOW}Allowing release to proceed (standalone/experimental mode).{NC}")
        return

    if layout == "A":
        cprint("  Layout A detected (standalone plugin repo)")
        notify_wf = details.get("notify_workflow")
        mkt_owner = details.get("mkt_owner")
        mkt_repo = details.get("mkt_repo")
        if not notify_wf or not Path(notify_wf).is_file():
            cprint(f"  {RED}BLOCKED: .github/workflows/notify-marketplace.yml missing.{NC}")
            sys.exit(1)
        if not mkt_owner or not mkt_repo:
            cprint(f"  {RED}BLOCKED: notify-marketplace.yml has no MARKETPLACE_OWNER/MARKETPLACE_REPO.{NC}")
            sys.exit(1)
        cprint(f"  target marketplace: {mkt_owner}/{mkt_repo}")
        if shutil.which("gh") is None:
            cprint(f"  {RED}BLOCKED: gh CLI not installed — cannot verify secret/marketplace.{NC}")
            sys.exit(1)
        if not _gh_secret_exists(root, "MARKETPLACE_PAT"):
            cprint(f"  {RED}BLOCKED: MARKETPLACE_PAT secret not configured on this plugin repo.{NC}")
            cprint(f"  {RED}  Fix: uv run python scripts/set_marketplace_pat.py {_current_repo_slug(root) or 'OWNER/REPO'}{NC}")
            sys.exit(1)
        cprint(f"  {GREEN}MARKETPLACE_PAT secret configured{NC}")
        mkt_json = _fetch_remote_marketplace_json(mkt_owner, mkt_repo)
        if mkt_json is None:
            cprint(f"  {RED}BLOCKED: cannot fetch marketplace.json from {mkt_owner}/{mkt_repo}.{NC}")
            sys.exit(1)
        plugin_name = _read_plugin_name(root)
        slug = _current_repo_slug(root)
        if not _plugin_in_remote_marketplace(mkt_json, plugin_name, slug):
            cprint(f"  {RED}BLOCKED: plugin '{plugin_name}' not registered in {mkt_owner}/{mkt_repo} marketplace.json.{NC}")
            cprint(f"  {RED}  Add an entry: {{\"name\": \"{plugin_name}\", \"source\": {{\"source\": \"github\", \"repo\": \"{slug}\"}}}}{NC}")
            sys.exit(1)
        cprint(f"  {GREEN}Plugin registered in remote marketplace.json{NC}")
        if not _remote_has_receiver_workflow(mkt_owner, mkt_repo):
            cprint(f"  {RED}BLOCKED: remote marketplace {mkt_owner}/{mkt_repo} has no workflow with repository_dispatch trigger.{NC}")
            cprint(f"  {RED}  See cpv-setup-marketplace-auto-notification skill.{NC}")
            sys.exit(1)
        cprint(f"  {GREEN}Remote marketplace has receiver workflow{NC}")
        cprint(f"  {GREEN}Layout A marketplace registration verified.{NC}")
        return

    if layout == "B":
        cprint("  Layout B detected (nested plugin under marketplace repo)")
        marketplace_root_raw = details.get("marketplace_root")
        marketplace_root: Path | None = marketplace_root_raw if isinstance(marketplace_root_raw, Path) else None
        plugin_name_raw = details.get("plugin_name")
        # Note: no type annotation here — mypy's no-redef rule complains even
        # though the Layout A branch above returns before reaching this
        # point. Plain assignment avoids the false positive in the generated
        # template output (which downstream CI runs with mypy --strict).
        plugin_name = plugin_name_raw if isinstance(plugin_name_raw, str) else root.name
        if marketplace_root is None:
            cprint(f"  {RED}BLOCKED: Layout B detected but marketplace_root unresolved.{NC}")
            sys.exit(1)
        if root.resolve() != marketplace_root.resolve():
            cprint(f"  {RED}BLOCKED: This is a Layout B nested plugin.{NC}")
            cprint(f"  {RED}  publish.py must run at the MARKETPLACE root, not the nested folder.{NC}")
            cprint(f"  {RED}  Bumping a nested plugin alone breaks the atomic marketplace tag.{NC}")
            cprint(f"  {RED}  Fix: cd {marketplace_root} && uv run python scripts/publish.py --patch{NC}")
            sys.exit(1)
        mp_path = marketplace_root / ".claude-plugin" / "marketplace.json"
        try:
            mp_data = json.loads(mp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            cprint(f"  {RED}BLOCKED: cannot read {mp_path}: {e}{NC}")
            sys.exit(1)
        entries = mp_data.get("plugins") if isinstance(mp_data, dict) else None
        if not isinstance(entries, list):
            cprint(f"  {RED}BLOCKED: marketplace.json has no 'plugins' array.{NC}")
            sys.exit(1)
        if not any(isinstance(e, dict) and e.get("name") == plugin_name for e in entries):
            cprint(f"  {RED}BLOCKED: plugin '{plugin_name}' not registered in {mp_path}.{NC}")
            cprint(f"  {RED}  Add: {{\"name\": \"{plugin_name}\", \"source\": \"./plugins/{plugin_name}\"}}{NC}")
            sys.exit(1)
        cprint(f"  {GREEN}Plugin '{plugin_name}' registered in parent marketplace.json{NC}")
        cprint(f"  {GREEN}Layout B marketplace registration verified.{NC}")


def stage_consistency(root: Path) -> None:
    """Step 10: Check version consistency."""
    cprint(f"\n{BOLD}[10/15] Checking version consistency...{NC}")
    ok, msg = check_version_consistency(root)
    cprint(f"  {msg}")
    if not ok:
        cprint(f"  {RED}Fix version mismatch before publishing.{NC}")
        sys.exit(1)
    cprint(f"  {GREEN}Consistent.{NC}")

def _read_remote_version(plugin_root: Path) -> str | None:
    """Read .claude-plugin/plugin.json's `version` from origin/master (or main).

    Idempotency baseline: the publish pipeline reads the REMOTE version, not
    the local one, so an interrupted publish that already bumped + committed
    locally cannot double-bump on re-run. Returns None when offline / no
    remote ref / file missing — caller must fall back to local baseline.
    """
    for ref in ("origin/master", "origin/main", "origin/HEAD"):
        try:
            r = subprocess.run(
                ["git", "show", f"{ref}:.claude-plugin/plugin.json"],
                capture_output=True, text=True, cwd=str(plugin_root),
                check=False, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        try:
            v = json.loads(r.stdout).get("version")
        except json.JSONDecodeError:
            continue
        if isinstance(v, str):
            return v
    return None


def _infer_bump_type(old: str, new: str) -> str | None:
    """Classify a semver delta as 'major', 'minor', 'patch', or None."""
    o = parse_semver(old)
    n = parse_semver(new)
    if o is None or n is None or n <= o:
        return None
    if n[0] != o[0]:
        return "major"
    if n[1] != o[1]:
        return "minor"
    return "patch"


def _git_porcelain_clean(root: Path) -> bool:
    """True iff `git status --porcelain` is empty (working tree clean)."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(root),
            check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and not r.stdout.strip()


def _head_commit_message(root: Path) -> str:
    """Return the subject line of HEAD, or '' on failure."""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            capture_output=True, text=True, cwd=str(root),
            check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _local_tag_exists(root: Path, tag: str) -> bool:
    """True iff `tag` already exists in the local git repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
            capture_output=True, text=True, cwd=str(root),
            check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _remote_tag_state(root: Path, tag: str) -> bool | None:
    """Three-valued remote-tag probe (amvcp TRDD-YY5ISKCJ shape).

    True = ls-remote succeeded and the tag exists; False = succeeded and it
    does not (a real answer — first-publish relies on it); None = the remote
    could NOT be read. None is deliberately DISTINCT from False: any consumer
    about to act destructively (undo a commit, move a tag) must REFUSE on
    None rather than treat an unanswered question as "no tag".
    """
    try:
        r = subprocess.run(
            ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"],
            capture_output=True, text=True, cwd=str(root),
            check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def _remote_tag_exists(root: Path, tag: str) -> bool:
    """True only on a POSITIVE remote answer, per `git ls-remote`.

    Asks the REMOTE rather than trusting that the push stage ran: a push that
    executed and silently failed its ref-update is otherwise indistinguishable
    from one that worked, and the plugin then reports a green publish while
    being undependable (ai-maestro#62 R3).

    Any failure to answer (network down, timeout, git error) maps to False
    here, so the post-push verify reports UNVERIFIED — never a false green.
    A caller that would act DESTRUCTIVELY on "absent" must use
    `_remote_tag_state` and refuse on None instead.
    """
    return _remote_tag_state(root, tag) is True


def _plugin_name(root: Path) -> str | None:
    """Read the plugin name from .claude-plugin/plugin.json."""
    pj = root / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    name = data.get("name")
    return str(name) if name else None


def _dependency_tag_name(root: Path, new_ver: str) -> str | None:
    """The `{plugin-name}--v{version}` tag Claude Code resolves dependencies against.

    Derived from the manifest, never hardcoded, so renaming the plugin cannot silently
    desync the tag from the plugin it names. Returns None when the name is unreadable,
    in which case the caller warns and skips rather than inventing a name.

    This is the exact name `claude plugin tag` produces.
    """
    name = _plugin_name(root)
    return f"{name}--v{new_ver}" if name else None


def _ensure_tag_at_head(root: Path, tag_name: str, message: str) -> bool:
    """Guarantee `tag_name` exists AND points at HEAD, or refuse (audit row 7).

    The old behaviour was "if it exists locally, skip". After a publish that died
    between tagging and the push (a blocking gate, a lost connection), the retry
    then pushed the PREVIOUS attempt's tag — so every commit made between the
    attempts, typically the very fix that made the retry pass, landed on the
    branch but OUTSIDE the released tag, and the release archive differed from
    the tree the gates had just validated.

    FAIL-CLOSED. The tag is moved ONLY on the remote's POSITIVE answer that it is
    unpushed. A tag already on origin is immutable here, and an unreachable
    remote is not consent — an unanswered `ls-remote` must never be read as "the
    tag is not published".

    Returns True when the tag is correct (created, moved, or already at HEAD),
    False when the caller must abort.
    """
    if not _local_tag_exists(root, tag_name):
        run(["git", "tag", "-a", tag_name, "-m", message], cwd=root)
        cprint(f"  {GREEN}Tag {tag_name} created.{NC}")
        return True
    tag_sha = run(["git", "rev-list", "-n", "1", tag_name], cwd=root,
                  check=False, capture=True).stdout.strip()
    head_sha = run(["git", "rev-parse", "HEAD"], cwd=root,
                   check=False, capture=True).stdout.strip()
    if not (tag_sha and head_sha) or tag_sha == head_sha:
        # Already correct, or the shas are unreadable — the latter is the
        # historical behaviour and is safe: nothing is moved on a guess.
        cprint(f"  {GREEN}Tag {tag_name} already present at HEAD.{NC}")
        return True
    remote_state = _remote_tag_state(root, tag_name)
    if remote_state is None:
        cprint(f"  {RED}Local tag {tag_name} points at {tag_sha[:8]} (HEAD {head_sha[:8]}) "
               f"and origin's tags cannot be read (ls-remote failed). REFUSING to move "
               f"the tag — that is only safe when the remote confirms it is unpushed. "
               f"Re-run once the remote is reachable.{NC}")
        return False
    if remote_state is True:
        cprint(f"  {RED}Local tag {tag_name} points at {tag_sha[:8]} but HEAD is "
               f"{head_sha[:8]}, and the tag is ALREADY ON ORIGIN. REFUSING to move a "
               f"published tag. Bump to a new version instead.{NC}")
        return False
    cprint(f"  {YELLOW}Local tag {tag_name} points at {tag_sha[:8]}, HEAD is at "
           f"{head_sha[:8]}. The tag is unpushed; deleting and recreating it at HEAD.{NC}")
    run(["git", "tag", "-d", tag_name], cwd=root)
    run(["git", "tag", "-a", tag_name, "-m", message], cwd=root)
    cprint(f"  {GREEN}Tag {tag_name} re-created at HEAD.{NC}")
    return True


def stage_bump(root: Path, new_ver: str, dry_run: bool) -> None:
    """Step 11: Bump version. Idempotent — skips when local already matches target.

    Recovery semantics: when a previous publish was interrupted between the
    local commit+tag and the push (transient network failure during git push,
    pre-push hook reject, etc.), the local repo is at the bumped version while
    origin is one minor behind. Re-running publish.py would DOUBLE-BUMP
    (read-local-then-add-1 → next minor on top of the already-bumped local).
    The fix: read REMOTE plugin.json as baseline, infer bump type from
    local-vs-remote delta, and skip the bump entirely when local already
    matches the target.
    """
    cprint(f"\n{BOLD}[11/15] Bumping version...{NC}")
    current = get_current_version(root)
    remote = _read_remote_version(root)
    if remote and current and current == new_ver:
        cprint(f"  {YELLOW}Local plugin.json is already at {new_ver} (remote at {remote}) — "
               f"skipping bump (interrupted-publish recovery).{NC}")
        return
    if remote and current and current != remote and current != new_ver:
        cprint(f"  {RED}REFUSED: local plugin.json is at {current} but remote is at "
               f"{remote} and target is {new_ver}. Refuse to guess what state this is.{NC}")
        cprint(f"  {RED}Manual intervention required: align local with remote, then re-run.{NC}")
        sys.exit(1)
    if not do_bump(root, new_ver, dry_run=dry_run):
        cprint(f"  {RED}Version bump failed.{NC}")
        sys.exit(1)
    cprint(f"  {GREEN}Version bumped to {new_ver}.{NC}")

def stage_update_badges(root: Path, old_ver: str, new_ver: str, dry_run: bool) -> None:
    """Step 12: Replace version badge in README.md.

    Strategy:
      1. Try exact-string substitution `version-<old>-blue` → `version-<new>-blue`
      2. If the exact old version is not present, fall back to a regex that
         matches ANY `version-X.Y.Z-blue` pattern (handles drift from a hand-edit
         or a missed release). Prevents the "stale forever" trap that bit CPV
         itself when its README badge fell 20 releases behind.
      3. Emit a WARNING (not silent skip) when no badge is found at all so the
         author notices the README has no shields.io version badge to update.
    """
    cprint(f"\n{BOLD}[12/15] Updating README badge...{NC}")
    readme = root / "README.md"
    if not readme.exists():
        cprint(f"  {YELLOW}WARNING: no README.md — skipping badge update.{NC}")
        return
    content = readme.read_text(encoding="utf-8")
    old_badge = f"version-{old_ver}-blue"
    new_badge = f"version-{new_ver}-blue"

    if old_badge in content:
        if dry_run:
            cprint(f"  Would update badge (exact match): {old_badge} -> {new_badge}")
            return
        readme.write_text(content.replace(old_badge, new_badge, 1), encoding="utf-8")
        cprint(f"  {GREEN}Updated README badge: {old_ver} -> {new_ver}{NC}")
        return

    # Fallback: regex match on any version-X.Y.Z-blue pattern
    badge_re = re.compile(r"version-\d+\.\d+\.\d+-blue")
    match = badge_re.search(content)
    if match is None:
        cprint(f"  {YELLOW}WARNING: no version-X.Y.Z-blue badge found in README.md.{NC}")
        cprint(f"  {YELLOW}Add a shields.io badge so future releases can update it automatically.{NC}")
        return
    found = match.group(0)
    if dry_run:
        cprint(f"  Would update badge (regex match): {found} -> {new_badge}")
        return
    readme.write_text(badge_re.sub(new_badge, content, count=1), encoding="utf-8")
    cprint(f"  {GREEN}Updated README badge (was {found}, now {new_badge}){NC}")

def detect_bump_type(root: Path) -> str:
    """Auto-detect the next bump type from conventional commits via git-cliff.

    Runs `git-cliff --bumped-version` and compares the predicted version to
    the REMOTE one (origin/master) to determine major/minor/patch. Falls back
    to 'patch' on any failure (git-cliff missing, repo empty, parse error) so
    the cornerstone rule — every push is a bump — is never violated.

    Idempotency: when the local repo already has a release commit (interrupted
    publish), reading local plugin.json would over-shoot the bump (current is
    already the bumped version, git-cliff would compute current+1). Reading
    remote/origin gives the true baseline.

    Conventional commit mapping (git-cliff defaults):
      feat:                 -> minor
      fix:/perf:/refactor:  -> patch
      BREAKING CHANGE / !   -> major
    """
    cliff_bin = shutil.which("git-cliff")
    if cliff_bin is None:
        cprint(f"{YELLOW}git-cliff not installed — auto-bump falls back to 'patch'.{NC}")
        return "patch"
    current = _read_remote_version(root) or get_current_version(root)
    if not current:
        cprint(f"{YELLOW}Cannot read current version for auto-bump — falling back to 'patch'.{NC}")
        return "patch"
    try:
        r = subprocess.run(
            [cliff_bin, "--bumped-version"],
            capture_output=True,
            text=True,
            cwd=str(root),
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return "patch"
    if r.returncode != 0:
        return "patch"
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    bumped = out.lstrip("v").strip()
    if not bumped or bumped == current:
        return "patch"
    try:
        cur = [int(p) for p in current.split(".")[:3]]
        nxt = [int(p) for p in bumped.split(".")[:3]]
        while len(cur) < 3:
            cur.append(0)
        while len(nxt) < 3:
            nxt.append(0)
    except ValueError:
        return "patch"
    if nxt[0] > cur[0]:
        return "major"
    if nxt[1] > cur[1]:
        return "minor"
    return "patch"


def _write_release_notes(root: Path, new_ver: str, tag: str) -> None:
    """Render THIS release's notes — the section for `tag` alone — under reports/.

    A SEPARATE artifact from CHANGELOG.md, and the separation is the whole point.
    CHANGELOG.md is FULL HISTORY and has to be (`-o` overwrites, so `--unreleased`
    there destroys every prior entry — ai-maestro#62), which means handing it to
    `gh release create --notes-file` publishes the project's ENTIRE history as
    every single release's body. That is not merely noisy: GitHub caps a release
    body at 125,000 characters, so a long-lived plugin eventually fails the
    release call AFTER its tag is already public.

    `--unreleased` belongs HERE and nowhere else — it renders only the commits
    since the previous tag — and `--strip all` drops the changelog header/footer,
    which are boilerplate inside a release body.

    The output goes to the gitignored `reports/` tree, NEVER into the repo
    proper: a file sitting in the working tree at release time is one `git add`
    away from being swept into the release commit.

    BEST-EFFORT on purpose. Any failure leaves no usable notes file and
    `stage_gh_release` falls back to `--generate-notes`, because publishing a
    best-effort slice is worse than generated notes — a mis-sliced body ships the
    PREVIOUS release's notes under the new tag.
    """
    notes = root / "reports" / "publish" / f"release-notes-{new_ver}.md"
    try:
        notes.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git-cliff", "--unreleased", "--tag", tag, "--strip", "all", "-o", str(notes)],
            cwd=str(root), capture_output=True, text=True, timeout=300, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        cprint(f"  {YELLOW}Release notes could not be rendered ({exc}) — the GitHub "
               f"release will fall back to --generate-notes.{NC}")
        return
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        cprint(f"  {YELLOW}Release notes could not be rendered (git-cliff exit "
               f"{result.returncode}) — the GitHub release will fall back to "
               f"--generate-notes.{NC}")
        for line in tail:
            cprint(f"  {YELLOW}  {line}{NC}")
        return
    cprint(f"  {GREEN}Release notes for {tag} rendered to reports/publish/"
           f"release-notes-{new_ver}.md.{NC}")


def stage_changelog(root: Path, new_ver: str, dry_run: bool) -> None:
    """Step 13: Generate CHANGELOG.md with git-cliff using the bumped tag.

        git cliff --bump --tag v<NEXT> -o CHANGELOG.md

    --bump          promote the unreleased section into a dated tag entry
    --tag v<NEXT>   label the new entry with the computed version (prefixed v)
    -o CHANGELOG.md write the regenerated changelog back to disk

    `--unreleased` MUST NOT appear here. `-o` OVERWRITES the file, so
    restricting the content to the unreleased window and then writing leaves a
    CHANGELOG containing only the release just generated — every prior entry is
    destroyed and the history is unrecoverable from the artifact. Rendering the
    full history from the commit log is also IDEMPOTENT: running this step
    twice for the same version produces the same file. `--prepend` is the WRONG
    fix — it accumulates, so a publish retried after a failed downstream gate
    silently duplicates a section.

    Consequence worth stating on purpose: CHANGELOG.md is fully DERIVED from
    conventional commits, so hand-written prose in it does not survive a
    publish. Put release prose in the commit messages.

    This stage ALSO renders the GitHub release notes (this release's section
    only) via `_write_release_notes` — see there for why the two artifacts must
    never be the same file. It runs here, BEFORE the tag exists, because
    `--unreleased` means "commits after the last tag": once step 10 has tagged
    HEAD there is nothing unreleased left to render.
    """
    cprint(f"\n{BOLD}[13/15] Generating changelog (git-cliff)...{NC}")
    if not shutil.which("git-cliff"):
        cprint(f"  {YELLOW}git-cliff not installed — skipping changelog.{NC}")
        return
    cliff_toml = root / "cliff.toml"
    if not cliff_toml.is_file():
        cprint(f"  {YELLOW}No cliff.toml — skipping changelog.{NC}")
        return
    tag = f"v{new_ver}"
    if dry_run:
        cprint(f"  Would run: git-cliff --bump --tag {tag} -o CHANGELOG.md")
        cprint(f"  Would render release notes: git-cliff --unreleased --tag {tag} --strip all")
        return
    run(
        ["git-cliff", "--bump", "--tag", tag, "-o", "CHANGELOG.md"],
        cwd=root,
    )
    cprint(f"  {GREEN}CHANGELOG.md updated with {tag} (full history).{NC}")
    _write_release_notes(root, new_ver, tag)

def stage_commit_and_push(root: Path, new_ver: str, dry_run: bool) -> None:
    """Step 14: Commit, tag, push. Idempotent on commit + tag.

    Idempotency: if HEAD's subject is already `chore: bump version to <new_ver>`
    AND the working tree is clean, skip the commit step (interrupted-publish
    recovery). If the tag already exists locally, skip the tag step. The push
    always runs — that is what brings the remote into sync.

    TRDD-bbff5bc5 §5: gh-auth precheck runs BEFORE the first push so the
    user gets an actionable error if their gh CLI is unauthed/lacks push
    perm — instead of an opaque git push failure mid-pipeline.
    """
    cprint(f"\n{BOLD}[14/15] Committing and pushing...{NC}")
    tag = f"v{new_ver}"
    # The DEPENDENCY-RESOLUTION tag. Since Claude Code 2.1.110 a version-constrained
    # dependency ({"name": "<plugin>", "version": ">=1.2"}) is resolved by listing this
    # repo's tags, keeping only those starting with "<plugin>--v", and fetching the
    # highest one satisfying the range. The plain vX.Y.Z tag is IGNORED by that
    # resolver, so a plugin shipping only vX.Y.Z cannot be depended upon: every
    # dependent fails to install with `no-matching-tag` and is DISABLED.
    #
    # It stays invisible until someone installs clean (an already-installed dependent
    # keeps working), which is exactly how it went unnoticed in the wild. So both tags
    # are created and pushed in the SAME atomic push -- a release can never ship with
    # one and not the other. NOTE the separator is a DOUBLE hyphen (`--v`); a single
    # `-v` does not match the resolver's prefix filter.
    dep_tag = _dependency_tag_name(root, new_ver)
    expected_subject = f"chore: bump version to {new_ver}"
    head_subject = _head_commit_message(root)
    tree_clean = _git_porcelain_clean(root)
    tag_exists = _local_tag_exists(root, tag)
    dep_tag_exists = dep_tag is not None and _local_tag_exists(root, dep_tag)
    push_refs = ["HEAD", tag] + ([dep_tag] if dep_tag else [])

    if dry_run:
        if head_subject == expected_subject and tree_clean:
            cprint(f"  Would skip commit (HEAD already '{expected_subject}', tree clean)")
        else:
            cprint(f"  Would commit: {expected_subject}")
        if tag_exists:
            cprint(f"  Would skip tag (already exists locally): {tag}")
        else:
            cprint(f"  Would tag: {tag}")
        if dep_tag is None:
            cprint(f"  {YELLOW}Would SKIP the dependency tag - plugin name unreadable.{NC}")
        elif dep_tag_exists:
            cprint(f"  Would skip dependency tag (already exists locally): {dep_tag}")
        else:
            cprint(f"  Would tag (dependency resolution): {dep_tag}")
        cprint(f"  Would push (atomic): origin {' '.join(push_refs)}")
        return

    if head_subject == expected_subject and tree_clean:
        cprint(f"  {YELLOW}HEAD is already '{expected_subject}' and tree is clean — "
               f"skipping commit (interrupted-publish recovery).{NC}")
    else:
        # Issue #186 — stage TRACKED modifications only; never `git add -A`.
        # Untracked files at this point are not part of the release: gate 1
        # already required a clean tree, so anything still untracked is
        # reports/ (which "routinely contain private data — absolute paths,
        # usernames, internal hostnames, tokens caught in logs"), local
        # scratch, or leftovers from a failed run. A release commit is pushed
        # to a public repo AND is the artifact users install, so an accidental
        # inclusion there is unrecoverable in practice.
        run(["git", "add", "-u"], cwd=root)
        # `.plugin-self-hashes.json` is deliberately NOT in this list (audit row
        # 25): nothing in this pipeline generates it, so naming it here promised
        # a hash-refresh stage that does not exist. Add it back together with
        # that stage, never before it.
        for _gen in (".claude-plugin/plugin.json",
                     "CHANGELOG.md", "README.md", "pyproject.toml", "uv.lock"):
            if (root / _gen).exists():
                run(["git", "add", "--", _gen], cwd=root)
        _st = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                             capture_output=True, text=True, check=False)
        _stray = [ln[3:].strip() for ln in _st.stdout.splitlines() if ln.startswith("??")]
        if _stray:
            cprint(f"  {YELLOW}NOT staged — untracked files are never swept into a release (#186):{NC}")
            for _p in _stray[:20]:
                cprint(f"  {YELLOW}    ?? {_p}{NC}")
            cprint(f"  {YELLOW}  If one belongs in the release, `git add` it BY NAME and re-run.{NC}")
        # PRRD G1.1: every commit this tool creates carries an `Agent:` trailer
        # self-identifying which plugin's pipeline authored it. The slug is
        # DERIVED from the manifest (never hardcoded — this file is a shared
        # template), and the trailer carries NO `@`: a bare handle in a commit
        # message pages a real GitHub account. A second `-m` paragraph is a
        # proper git trailer (last paragraph, `Token: value`).
        # Any `@` in the name is stripped: GitHub linkifies `@word` in commit
        # messages, so a handle-shaped name would PAGE a real account.
        _agent_slug = (_plugin_name(root) or "").replace("@", "")
        _commit_cmd = ["git", "commit", "-m", expected_subject]
        if _agent_slug:
            _commit_cmd += ["-m", f"Agent: {_agent_slug}"]
        else:
            cprint(f"  {YELLOW}plugin.json name unreadable — commit carries no Agent: trailer (G1.1).{NC}")
        run(_commit_cmd, cwd=root)

    # Both tags route through _ensure_tag_at_head (audit row 7). "Exists locally
    # -> skip" pushed a PREVIOUS attempt's tag after an interrupted publish, so
    # the released tag no longer pointed at the validated tree.
    if not _ensure_tag_at_head(root, tag, f"Release {tag}"):
        sys.exit(1)

    if dep_tag is None:
        # Warn loudly rather than silently omitting it: a silent skip is precisely how
        # this defect survived unnoticed across many releases.
        cprint(f"  {YELLOW}WARNING: cannot read the plugin name from "
               f".claude-plugin/plugin.json - SKIPPING the dependency tag. Dependent "
               f"plugins will fail to resolve this release with `no-matching-tag`.{NC}")
    elif not _ensure_tag_at_head(root, dep_tag, f"{_plugin_name(root)} {new_ver}"):
        sys.exit(1)

    # gh-auth precheck — fail fast with actionable error if gh missing/unauthed.
    owner, repo = _resolve_owner_repo(root)
    _ensure_gh_auth(owner, repo)
    # Atomic push: commit + tag land together or not at all. Eliminates the
    # half-published-state failure mode where `git push origin HEAD --tags`
    # could push the commit, fail on the tag (rejected/network), and leave
    # the remote with an unreleased commit + no tag. `--atomic` is a single
    # transaction in the wire protocol; the server rolls back if any ref
    # update fails. git_with_retry still wraps the call so transient
    # network hiccups (4xx-class permanent errors fall through immediately).
    cprint(f"  {BLUE}$ git push --atomic origin {' '.join(push_refs)}{NC}")
    # capture_output MUST stay True (the default): the transient classifier
    # reads result.stderr, and with capture_output=False stderr is None, so
    # every failure classified as permanent and the release push could never
    # retry a network blip. Echo the captured stderr so nothing is swallowed.
    try:
        _push_res = git_with_retry(
            ["git", "push", "--atomic", "origin", *push_refs],
            cwd=str(root),
            # issue #224: the pre-push gate runs inside this call's wall clock.
            timeout=_PUSH_TIMEOUT_SEC,
            max_attempts=_PUSH_MAX_ATTEMPTS,
        )
    except subprocess.CalledProcessError as _push_exc:
        if _push_exc.stderr:
            print(_push_exc.stderr, file=sys.stderr, end="")
        raise
    if _push_res.stderr:
        print(_push_res.stderr, file=sys.stderr, end="")
    _pushed = tag if dep_tag is None else f"{tag} + {dep_tag}"
    cprint(f"  {GREEN}Pushed {_pushed} atomically.{NC}")
    # PROVE THE TAG, not the stage (ai-maestro#62 R3). A push stage that ran and
    # silently failed its ref-update looks exactly like one that worked, and the
    # plugin then reports a green publish while being undependable. ls-remote
    # asks the remote itself.
    #
    # Never a false green AND never a false block: the refs are already pushed by
    # this point, so failing the run could not un-push them — an unverifiable tag
    # is reported UNVERIFIED with the command to check it by hand.
    for _verify_tag in (tag, *([dep_tag] if dep_tag else [])):
        if _remote_tag_exists(root, _verify_tag):
            cprint(f"  {GREEN}Verified on remote: {_verify_tag}{NC}")
        else:
            cprint(f"  {YELLOW}Could NOT verify {_verify_tag} on remote (ls-remote found "
                   f"nothing, or the network was unreachable).{NC}")
            cprint(f"  {YELLOW}  Check with: git ls-remote --tags origin '*{_verify_tag}'{NC}")

def stage_gh_release(root: Path, new_ver: str, dry_run: bool) -> None:
    """Step 15: Create GitHub release via gh CLI.

    TRDD-bbff5bc5 §5: re-runs the gh-auth precheck before `gh release
    create` so an auth state change between gates 10 and 11 (token
    revoked, account switched) surfaces as an actionable error.
    """
    cprint(f"\n{BOLD}[15/15] Creating GitHub release...{NC}")
    tag = f"v{new_ver}"
    if not shutil.which("gh"):
        cprint(f"  {YELLOW}gh CLI not installed — skipping release.{NC}")
        return
    if dry_run:
        cprint(f"  Would create release: {tag}")
        return
    owner, repo = _resolve_owner_repo(root)
    _ensure_gh_auth(owner, repo)
    # THIS RELEASE'S notes, never CHANGELOG.md. Step 9 renders the section for
    # this tag alone (`git-cliff --unreleased --strip all`) into the gitignored
    # reports/ tree; CHANGELOG.md is full history, so passing it here would
    # publish the whole project history as this one release's body and would
    # eventually breach GitHub's 125,000-character release-body limit AFTER the
    # tag is already public.
    #
    # The path is spelled out here as well as in `_write_release_notes` so each
    # stage stands alone (they are exercised independently); the two spellings
    # are pinned identical by the canon's tests.
    notes_file = root / "reports" / "publish" / f"release-notes-{new_ver}.md"
    notes_usable = False
    if notes_file.is_file():
        try:
            notes_usable = bool(notes_file.read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            notes_usable = False
    # Passing --notes-file and --generate-notes together is undefined across gh
    # versions (some concatenate, some override) — never both.
    args = ["gh", "release", "create", tag, "--title", tag]
    if notes_usable:
        args.extend(["--notes-file", str(notes_file)])
    else:
        # No notes file (git-cliff absent, no cliff.toml, render failed): let
        # GitHub generate them from the commit range. NEVER fall back to a
        # best-effort slice of CHANGELOG.md — a mis-sliced body ships the
        # PREVIOUS release's notes under this tag, which reads as correct.
        args.append("--generate-notes")
    cprint(f"  {BLUE}$ {' '.join(args)}{NC}")
    result = gh_with_retry(args, cwd=str(root), check=False, capture_output=True)
    if result.stdout and result.stdout.strip():
        cprint(result.stdout.strip())
    if result.stderr and result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode == 0:
        cprint(f"  {GREEN}Release created.{NC}")
        return
    # `gh release create` returns an "already_exists" / "already exists"
    # validation error when a release for this tag is already present. On a
    # re-run or interrupted-publish recovery that is the idempotent-success
    # outcome (the release IS there), so it must NOT abort — match either
    # spelling gh emits, case-insensitively.
    combined_err = f"{result.stdout or ''}\n{result.stderr or ''}"
    if re.search(r"already[ _]exists", combined_err, re.IGNORECASE):
        cprint(f"  {YELLOW}Release {tag} already exists — treating as success (idempotent re-run).{NC}")
        return
    # Any other non-zero exit is a genuine failure (auth revoked mid-pipeline,
    # malformed notes file, network exhausted all retries). The tag is already
    # pushed, but the documented final stage did NOT complete — abort so the
    # pipeline does not falsely report success (fail-fast invariant).
    cprint(f"  {RED}Failed to create release (exit code {result.returncode}).{NC}")
    cprint(f"  {RED}  The tag {tag} is pushed; create the release manually or re-run after fixing the cause.{NC}")
    sys.exit(1)


_SEMVER_RE = re.compile(r"\bv?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)\b")

# The CLI's own wording when a plugin cannot be resolved inside a marketplace.
_NOT_IN_MARKETPLACE_RE = re.compile(
    r"not found in marketplace|marketplace .*not found|marketplace update",
    re.IGNORECASE,
)


def _semvers_in(text: str) -> list[str]:
    """Every semver-shaped token in `text`, in order, without the leading v."""
    return _SEMVER_RE.findall(text)


def _resolve_marketplace_name(root: Path) -> str | None:
    """The marketplace NAME Claude Code installs by (`<plugin>@<name>`).

    Layout B reads the parent marketplace.json directly; Layout A resolves the
    remote marketplace repo from notify-marketplace.yml and reads its manifest.
    Anything unresolvable returns None, which makes the smoke test SKIP with a
    reason — NEVER guess a marketplace name, because installing from the wrong
    one would prove nothing at all.
    """
    layout, details = _detect_layout(root)
    if layout == "B":
        mp_root = details.get("marketplace_root")
        if isinstance(mp_root, Path):
            try:
                data = json.loads((mp_root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            name = data.get("name") if isinstance(data, dict) else None
            return name if isinstance(name, str) and name else None
        return None
    if layout == "A":
        owner, repo = details.get("mkt_owner"), details.get("mkt_repo")
        if not (isinstance(owner, str) and isinstance(repo, str)):
            return None
        data = _fetch_remote_marketplace_json(owner, repo)
        name = data.get("name") if isinstance(data, dict) else None
        return name if isinstance(name, str) and name else None
    return None


def _marketplace_is_registered(claude_bin: str, marketplace: str) -> bool:
    """True when `marketplace` appears in `claude plugin marketplace list`.

    READ-ONLY on purpose: running `marketplace add`/`update` to make the smoke
    test pass would mutate the user's global registry as a side effect of
    publishing.

    FAIL-SAFE towards the HARD FAILURE — any inability to answer returns True
    ("assume registered"), so a genuinely uninstallable release is never
    downgraded to SKIPPED by a probe that simply could not run.
    """
    try:
        listing = subprocess.run(
            [claude_bin, "plugin", "marketplace", "list"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if listing.returncode != 0:
        return True
    return marketplace in ((listing.stdout or "") + (listing.stderr or ""))


def _claude_config_dir() -> Path:
    """Claude Code's config dir — `$CLAUDE_CONFIG_DIR`, else `~/.claude`.

    Audit row 21: hardcoding `~/.claude` meant that on any host setting
    CLAUDE_CONFIG_DIR the registry probe reported "could not read" forever, i.e.
    permanently UNVERIFIED, while looking like a check.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(os.path.expanduser(raw)) if raw else Path.home() / ".claude"


_INSTALLED_PLUGINS_REGISTRY = _claude_config_dir() / "plugins" / "installed_plugins.json"


def _smoke_records_still_registered(
    target: str, smoke_dir: str, registry_path: Path | None = None
) -> list[str] | None:
    """Local-scope records still pointing at `smoke_dir`, or None when unreadable.

    Issue #209: `claude plugin uninstall --scope local` does not always drop the
    local-scope record from installed_plugins.json. When it does not, the temp
    dir is deleted moments later and the record is left pointing at a path that
    no longer exists — invisible, and it accumulates across releases.

    READ-ONLY, deliberately. The registry is Claude Code's shared state, not this
    pipeline's: a publish that edits another tool's state file to tidy up after
    itself is a worse failure mode than the orphan it removes. Reporting is the
    whole remedy.

    Returns None for "could not check" rather than an empty list, so a missing or
    malformed registry can never read as "verified clean" — the same
    cannot-check-is-not-a-pass rule the install smoke applies to itself.

    Paths are compared RESOLVED: on macOS a temp dir is handed out as
    /var/folders/... while the registry records /private/var/folders/..., and a
    raw string compare would find nothing on exactly the platform that reported
    this.
    """
    path = registry_path if registry_path is not None else _INSTALLED_PLUGINS_REGISTRY
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return None
    try:
        wanted = os.path.realpath(str(smoke_dir))
    except (OSError, ValueError):
        return None
    stale: list[str] = []
    for key, records in plugins.items():
        # Scoped to THIS plugin's records: a record another publish left behind
        # is not this run's to report, and claiming it would inflate the signal.
        if key != target or not isinstance(records, list):
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            project_path = rec.get("projectPath")
            if not isinstance(project_path, str) or not project_path:
                continue
            try:
                resolved = os.path.realpath(os.path.expanduser(project_path))
            except (OSError, ValueError):
                continue
            if resolved == wanted:
                stale.append(project_path)
    return stale


def _report_smoke_registry_orphan(target: str, smoke_dir: str) -> None:
    """Print the smoke-install registry-cleanup verdict. Never fatal, never a verdict.

    The release is already public by the time this runs, so a cleanup problem
    must never fail it — and `--keep-data` is NOT the thing to change here: it
    protects the data dir of the author's real USER-scope installation. The
    registry record is a separate surface nothing was asserting anything about.
    """
    stale = _smoke_records_still_registered(target, smoke_dir)
    if stale is None:
        cprint(f"  {YELLOW}Note: could not read {_INSTALLED_PLUGINS_REGISTRY} - smoke-install{NC}")
        cprint(f"  {YELLOW}  registry cleanup UNVERIFIED (not a failure, and not a pass either).{NC}")
        return
    if not stale:
        return
    cprint(f"  {YELLOW}Note: the local-scope record for {target} survived the uninstall and{NC}")
    cprint(f"  {YELLOW}  now points at a temp dir about to be deleted (issue #209):{NC}")
    for project_path in stale[:5]:
        cprint(f"  {YELLOW}    projectPath: {project_path}{NC}")
    cprint(f"  {YELLOW}  Harmless to this release; it accumulates in the registry.{NC}")


def stage_install_smoke(root: Path, new_ver: str, dry_run: bool) -> None:
    """Prove the just-published release actually INSTALLS (ai-maestro#62 R2).

    No static check catches an uninstallable release: the manifest validates,
    the tags exist, the marketplace entry is correct — and eleven plugins still
    shipped releases nobody could install. The only proof is installing it.

    Runs POST-RELEASE by necessity, so by DEFAULT it reports loudly rather than
    failing the run: a non-zero exit here could not un-ship anything. Set
    `PLUGIN_REQUIRE_INSTALL_SMOKE=1` for fail-the-release semantics.

    CANNOT-CHECK IS NEVER A PASS, and never a false FAILURE either: a missing
    `claude` CLI (normal on CI), an unresolvable marketplace, or a marketplace
    this host never registered are each reported SKIPPED with the reason.
    """
    cprint(f"\n{BOLD}[post-release] Proving the release installs...{NC}")
    if dry_run:
        cprint(f"  {YELLOW}(dry-run) skipped.{NC}")
        return
    if os.environ.get("PLUGIN_SKIP_INSTALL_SMOKE") == "1":
        cprint(f"  {YELLOW}SKIPPED - PLUGIN_SKIP_INSTALL_SMOKE=1{NC}")
        return
    strict = os.environ.get("PLUGIN_REQUIRE_INSTALL_SMOKE") == "1"
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        cprint(f"  {YELLOW}SKIPPED - the `claude` CLI is not on PATH (normal on a CI runner).{NC}")
        cprint(f"  {YELLOW}  This is NOT a pass: the release was not proven installable here.{NC}")
        return
    plugin_name = _read_plugin_name(root)
    marketplace = _resolve_marketplace_name(root)
    if not (plugin_name and marketplace):
        cprint(f"  {YELLOW}SKIPPED - could not resolve <plugin>@<marketplace> "
               f"(name={plugin_name!r}, marketplace={marketplace!r}).{NC}")
        cprint(f"  {YELLOW}  Not a pass - nothing was installed.{NC}")
        return
    target = f"{plugin_name}@{marketplace}"
    # `tempfile` is imported at module top; the local re-import here carried the
    # comment "imported only on this path", which was false (audit row 28).
    with tempfile.TemporaryDirectory(prefix="plugin-install-smoke-") as tmp:
        cprint(f"  {BLUE}$ (cd {tmp} && claude plugin install {target} --scope local){NC}")
        try:
            result = subprocess.run(
                [claude_bin, "plugin", "install", target, "--scope", "local"],
                cwd=tmp, capture_output=True, text=True, timeout=300, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            cprint(f"  {YELLOW}SKIPPED - the install could not be run ({exc}). Not a pass.{NC}")
            return
        # Put back what we took. `--scope local` scopes only the SETTINGS file
        # (in this temp dir, about to vanish) - the payload and its marketplace
        # registration land in shared ~/.claude state, so without this every
        # publish leaves another cached copy behind forever.
        #
        # `--scope local` + `--keep-data` are BOTH safety, not tidiness: the
        # author almost certainly has this plugin installed at USER scope, and a
        # wider uninstall - or one that dropped the persistent data dir - would
        # destroy their real installation to clean up after a smoke test.
        if result.returncode == 0:
            try:
                subprocess.run(
                    [claude_bin, "plugin", "uninstall", target,
                     "--scope", "local", "--keep-data", "-y"],
                    cwd=tmp, capture_output=True, text=True, timeout=120, check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                cprint(f"  {YELLOW}Note: smoke-install cleanup did not run ({exc}).{NC}")
            # Nothing above ASSERTS the local-scope record actually went away,
            # and it does not always. Checked here, inside the `with`, so the
            # temp dir still exists and resolves the same way the record
            # recorded it (issue #209).
            _report_smoke_registry_orphan(target, tmp)
    if result.returncode == 0:
        cprint(f"  {GREEN}{target} installs cleanly (dependencies resolved).{NC}")
        # EVIDENCE REQUIRED for the async-lag note: install stdout is a progress
        # line naming the plugin, not the semver, so a bare
        # `new_ver not in stdout` test fires on EVERY successful run - a note
        # that always fires carries no information and trains you to ignore it.
        resolved = _semvers_in(result.stdout or "")
        if resolved and new_ver not in resolved:
            cprint(f"  {YELLOW}Note: the marketplace resolved v{resolved[0]}, not v{new_ver} "
                   f"(async notify lag) - installability is proven, the listing lags.{NC}")
        return
    combined = (result.stderr or "") + "\n" + (result.stdout or "")
    # Distinguish "this host never registered the marketplace" (an environment
    # gap) from "the marketplace IS registered and does not carry this plugin"
    # (a REAL uninstallable release). Both produce the same message, and
    # reporting the first as a hard failure inverts cannot-check-is-not-a-pass
    # into cannot-check-is-a-fail. FAIL-SAFE: only a marketplace PROVEN
    # unregistered downgrades to SKIPPED.
    if _NOT_IN_MARKETPLACE_RE.search(combined) and not _marketplace_is_registered(claude_bin, marketplace):
        cprint(f"  {YELLOW}SKIPPED - the marketplace {marketplace!r} is not registered on this{NC}")
        cprint(f"  {YELLOW}  host, so the install could not resolve {target}. Not a pass.{NC}")
        cprint(f"  {YELLOW}  Register it (`claude plugin marketplace add <source>`) and re-run.{NC}")
        return
    tail = combined.strip().splitlines()[-8:]
    cprint(f"  {RED}RELEASE IS NOT INSTALLABLE: {target}{NC}")
    cprint(f"  {RED}The release is already public - fix forward with a new release.{NC}")
    for ln in tail:
        cprint(f"  {RED}  {ln}{NC}")
    cprint(f"  {RED}Reproduce: cd $(mktemp -d) && claude plugin install {target} --scope local{NC}")
    if strict:
        cprint(f"  {RED}PLUGIN_REQUIRE_INSTALL_SMOKE=1 - failing the publish run.{NC}")
        sys.exit(1)


# How long to wait for the released commit's CI runs to conclude. Generous on
# purpose: a cold runner installing a toolchain can take many minutes, and a gate
# that gives up early reports UNVERIFIED on a healthy run — noise that teaches the
# reader to ignore it. Expiry is never a failure verdict.
#
# PLACEMENT IS LOAD-BEARING. This line opens the CI-verify unit that
# `migrate_publish_py_ci_verify` lifts VERBATIM out of rendered canon, and its
# test fixture strips exactly the span from here to the main-section banner.
# Anything added inside that span is stripped by the fixture and NOT restored by
# the migrator, which breaks the byte-identity test that is the only thing
# stopping migrator and generator from silently diverging. So: add unrelated
# helpers ABOVE this line, never below it.
#
# Keep this prose free of the literal anchor strings themselves — the fixture
# searches the rendered text for them, so quoting one in a comment relocates the
# span or trips the fixture's own sanity assertion.
CI_VERIFY_TIMEOUT_S = 900


def stage_verify_ci_green(root: Path, dry_run: bool) -> None:
    """Confirm CI went GREEN on the commit that was just released.

    WHY THIS EXISTS. `--install-branch-rules` makes CI a required status check,
    but the release push goes straight to the default branch and the maintainer
    role can bypass the ruleset — GitHub then reports `Bypassed rule violations
    … required status checks are expected` and lets the push through. The bypass
    is what makes a scripted release possible at all, but it means **the required
    checks never actually gate the release**: tag, GitHub release and marketplace
    notification are all public before CI has said a word. Without this stage,
    "CI must be green" lives only in agent prose, and prose is skippable.

    NEVER ABORTS, and that asymmetry is deliberate rather than a weak gate: by
    the time this runs the release is already published, so exiting non-zero
    could not un-ship it — it would only discard the report that is the whole
    point. A RED result is surfaced as a loud notice naming the failing runs and
    the exact follow-up command, which is what lets the caller enter the
    fix→re-publish loop.

    "Cannot check" is never reported as green: no gh, no network, no runs found,
    or a timeout are each reported UNVERIFIED with the reason, never as a pass.
    """
    # Deliberately NOT numbered into the [N/M] sequence: those steps run BEFORE
    # anything is public and any of them can abort the publish. This one runs
    # after the release exists and never aborts, so numbering it as a 12th step
    # would misrepresent it as another gate the publish is conditional on.
    cprint(f"\n{BOLD}[post-release] Verifying CI on the released commit...{NC}")
    if dry_run:
        cprint(f"  {YELLOW}(dry-run) skipped.{NC}")
        return

    if shutil.which("gh") is None:
        cprint(f"  {YELLOW}UNVERIFIED - gh CLI not installed, cannot check CI.{NC}")
        cprint(f"  {YELLOW}  The release IS published; verify manually.{NC}")
        return

    # Every subprocess in this stage carries a timeout (audit row 11). The
    # deadline below is only consulted BETWEEN calls, so a single hung `gh`
    # blocked past CI_VERIFY_TIMEOUT_S indefinitely. A timed-out call is treated
    # as UNVERIFIED — never as green.
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                              capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        cprint(f"  {YELLOW}UNVERIFIED - `git rev-parse HEAD` timed out.{NC}")
        return
    if head.returncode != 0:
        cprint(f"  {YELLOW}UNVERIFIED - could not resolve HEAD.{NC}")
        return
    sha = head.stdout.strip()

    deadline = time.monotonic() + CI_VERIFY_TIMEOUT_S
    while True:
        try:
            listed = subprocess.run(
                ["gh", "run", "list", "--commit", sha, "--limit", "20",
                 "--json", "name,status,conclusion,headBranch"],
                cwd=str(root), capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            cprint(f"  {YELLOW}UNVERIFIED - `gh run list` timed out (>120s).{NC}")
            cprint(f"  {YELLOW}  Verify manually: gh run list --commit {sha}{NC}")
            return
        if listed.returncode != 0:
            cprint(f"  {YELLOW}UNVERIFIED - `gh run list` exited {listed.returncode}.{NC}")
            return
        try:
            runs = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError:
            cprint(f"  {YELLOW}UNVERIFIED - unparseable `gh run list` output.{NC}")
            return

        if not runs:
            # A workflow takes a few seconds to register after the push.
            if time.monotonic() >= deadline:
                cprint(f"  {YELLOW}UNVERIFIED - no CI runs found for {sha[:8]}.{NC}")
                return
            time.sleep(15)
            continue

        if not [r for r in runs if r.get("status") != "completed"]:
            break
        if time.monotonic() >= deadline:
            cprint(f"  {YELLOW}UNVERIFIED - still running after {CI_VERIFY_TIMEOUT_S}s.{NC}")
            # Full sha on purpose: `gh run list --commit` with a short sha
            # silently matches nothing and exits 0.
            cprint(f"  {YELLOW}  Check with: gh run list --commit {sha}{NC}")
            return
        time.sleep(15)

    # `skipped`/`neutral` are not failures — a dormant optional workflow
    # (e.g. a PyPI publish that only fires on a tag) always reports skipped.
    # `cancelled` is not automatically a failure either: the concurrency group
    # cancels a run that a newer push to the same branch superseded, which
    # looks identical to a genuine user-cancel unless we go find that newer
    # run ourselves.
    cancelled_runs = [r for r in runs if r.get("conclusion") == "cancelled"]
    successors = _resolve_ci_run_successors(root, sha, cancelled_runs) if cancelled_runs else {}
    failed, unknown = classify_ci_runs(runs, successors)
    if failed:
        detail = ", ".join(f"{r.get('name', '?')}={r.get('conclusion')}" for r in failed)
        cprint(f"  {RED}CI IS RED on the released commit {sha[:8]}: {detail}{NC}")
        cprint(f"  {RED}  The tag and GitHub release are ALREADY PUBLISHED - the ruleset{NC}")
        cprint(f"  {RED}  bypass meant no required check gated them. Fix the cause and{NC}")
        cprint(f"  {RED}  publish a follow-up patch; do NOT mute the check.{NC}")
        # Two steps, pasteable as written: `gh run view` has no --commit flag,
        # and `gh run list --commit` needs the FULL sha (short sha = silent []).
        cprint(f"  {RED}  Logs: gh run list --commit {sha}{NC}")
        cprint(f"  {RED}        then: gh run view --log-failed <run-id>{NC}")
        return

    if unknown:
        names = ", ".join(sorted({str(r.get("name", "?")) for r in unknown}))
        cprint(f"  {YELLOW}? CI verdict UNKNOWN on {sha[:8]}: run cancelled and no successor found - not checked ({names}).{NC}")
        cprint(f"  {YELLOW}  Verify manually: gh run list --commit {sha}{NC}")
        return

    names = ", ".join(sorted({str(r.get("name", "?")) for r in runs}))
    cprint(f"  {GREEN}CI green on {sha[:8]} ({names}){NC}")


def classify_ci_runs(runs: list[dict], successors: dict[str, bool]) -> tuple[list[dict], list[dict]]:
    """Split completed CI runs for one commit into (failed, unknown).

    A `success`/`skipped`/`neutral` conclusion is fine and appears in neither
    list. Any OTHER conclusion is a genuine failure — EXCEPT `cancelled`,
    which used to be treated identically to a real failure even though
    GitHub's own concurrency-group cancellation reports it that way for a run
    that was merely SUPERSEDED by a newer push to the same branch. `successors`
    disambiguates: it maps a cancelled run's workflow NAME to whether a newer
    run of that same workflow was found on a commit descended from the one
    being verified. Found -> the cancellation was benign supersession,
    excluded from both lists. Not found -> we cannot tell a genuine
    user-cancel from a lost successor, so it goes to `unknown` and the caller
    must report UNKNOWN, never green.
    """
    failed: list[dict] = []
    unknown: list[dict] = []
    for r in runs:
        conclusion = r.get("conclusion")
        if conclusion in ("success", "skipped", "neutral"):
            continue
        if conclusion == "cancelled":
            if successors.get(str(r.get("name", "?"))):
                continue
            unknown.append(r)
            continue
        failed.append(r)
    return failed, unknown


def _resolve_ci_run_successors(root: Path, sha: str, cancelled_runs: list[dict]) -> dict[str, bool]:
    """For each cancelled run, look for a newer descendant-commit successor.

    A `cancelled` run counts as superseded-not-failed only when a LATER run
    of the SAME workflow exists on a commit that is a git descendant of
    `sha` — i.e. a newer push to the same branch genuinely superseded this
    one. Any failure to determine that (no branch info, an unresolved
    merge-base) leaves that workflow's entry absent from the returned map,
    which `classify_ci_runs` treats as "no successor found".
    """
    result: dict[str, bool] = {}
    for r in cancelled_runs:
        name = str(r.get("name", "?"))
        if name in result:
            continue
        branch = r.get("headBranch")
        if not branch:
            continue
        # Per-call timeouts here too (audit row 11): a hung `gh` or `git` in this
        # loop had no bound at all. A timeout leaves the workflow ABSENT from the
        # returned map, which `classify_ci_runs` reads as "no successor found" —
        # i.e. UNKNOWN, the conservative direction.
        try:
            listed = subprocess.run(
                ["gh", "run", "list", "--workflow", name, "--branch", str(branch),
                 "--limit", "20", "--json", "headSha,conclusion,status,createdAt"],
                cwd=str(root), capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            continue
        if listed.returncode != 0:
            continue
        try:
            candidates = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for c in candidates:
            candidate_sha = c.get("headSha")
            if not candidate_sha or candidate_sha == sha:
                continue
            try:
                ancestry = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", sha, candidate_sha],
                    cwd=str(root), capture_output=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                continue
            if ancestry.returncode == 0:
                result[name] = True
                break
    return result


# -- Main ----------------------------------------------------------------------

# ── Canon version reporting (--canon-version) ────────────────────────────────
#
# This pipeline is a COPY of the CPV publish canon, so "which canon am I on?"
# cannot be answered from the copy alone. CANON_VERSION records the canon this
# file was generated (or migrated) from; the latest is read from the canon
# repo's manifest. The generator rewrites the placeholder below at scaffold
# time — an un-rewritten value means this file was hand-copied rather than
# generated, and is reported as-is rather than guessed.
CANON_VERSION = "5.17.0"
CANON_LATEST_URL = "https://raw.githubusercontent.com/Emasoft/claude-plugins-validation/master/.claude-plugin/plugin.json"
CANON_FETCH_TIMEOUT_S = 6


def fetch_latest_canon_version():
    """Newest canon version per the canon repo's manifest, or None.

    EVERY failure (offline, DNS, timeout, HTTP error, malformed JSON) returns
    None rather than raising: --canon-version is an INFORMATION command, and an
    info command that fails without network is a bug. None renders as an
    explicit "unknown", never as "up to date".
    """
    import urllib.request

    # nosec B310 - CANON_LATEST_URL is a fixed https literal; nothing
    # caller-supplied ever reaches this call. The rationale sits ABOVE the
    # constructor line on purpose: a trailing comment there completes an SSRF
    # network pattern, so the canon would emit code its own validator blocks
    # (upstream issue #199).
    req = urllib.request.Request(  # nosec B310
        CANON_LATEST_URL, headers={"User-Agent": "cpv-publish-canon-version"}
    )
    try:
        # nosec B310: CANON_LATEST_URL is a module-level https literal. B310 exists
        # to catch a scheme an attacker can choose (file:/, custom); no
        # caller-supplied value reaches this URL at all, so the finding cannot
        # apply here.
        with urllib.request.urlopen(req, timeout=CANON_FETCH_TIMEOUT_S) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def print_canon_version() -> int:
    """Print the canon version report. Always returns 0 — info never fails."""
    latest = fetch_latest_canon_version()
    print("Emasoft CPV Plugin Publishing Pipeline Canon")
    print()
    print(f"* Installed Canon Version:  {CANON_VERSION}")
    print(f"* Latest Version Available: {latest or 'unknown (could not reach GitHub)'}")
    print()
    if latest and CANON_VERSION == latest:
        print("The canon is up to date.")
    else:
        print('Run "/cpv-agent update the canon" to update')
        print("the plugin to the latest canon.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified publish pipeline for Claude Code plugins.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Mutually exclusive modes: side-modes (--gate / --install-hook /
    # --install-branch-rules) are distinct entry points; --patch/--minor/--major
    # are OPTIONAL overrides for the auto-bump default. Calling publish.py with
    # no flags runs the full publish pipeline with an auto-detected bump type.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--gate", action="store_true",
                            help="Pre-push gate mode: lint + copy-paste (jscpd) + validate + tests only (no bump/push)")
    mode_group.add_argument("--install-hook", action="store_true",
                            help="Install pre-push hook into .git/hooks/ and set core.hooksPath")
    mode_group.add_argument("--install-branch-rules", action="store_true",
                            dest="install_branch_rules",
                            help="Apply the cpv-branch-rules ruleset to the GitHub origin "
                                 "(enforces CI as a required status check — the server-side gate)")
    mode_group.add_argument("--patch", action="store_const", dest="bump", const="patch",
                            help="Force a patch bump (override auto-detection)")
    mode_group.add_argument("--minor", action="store_const", dest="bump", const="minor",
                            help="Force a minor bump (override auto-detection)")
    mode_group.add_argument("--major", action="store_const", dest="bump", const="major",
                            help="Force a major bump (override auto-detection)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    parser.add_argument("--canon-version", action="store_true",
                        help="Report the installed vs latest CPV publish-canon version and exit")
    parser.add_argument("--print-gates", action="store_true", dest="print_gates",
                        help="Print the numbered pipeline stage list and exit (no side effects)")
    # NOTE: --skip-tests was intentionally removed. The cornerstone rule is that
    # every CPV plugin MUST pass validation with 0 issues (WARNING allowed) before
    # any push. Skipping tests would bypass that guarantee — there are no exceptions.
    args = parser.parse_args()

    # Answered BEFORE the repo lookup and every gate: it reads a manifest and
    # reports. Someone debugging a stale canon usually has a dirty tree, and
    # refusing to answer because of it would make the command useless exactly
    # when it is needed.
    if args.canon_version:
        return print_canon_version()

    # Pure information, answered BEFORE the repo lookup and every gate, with
    # zero side effects — the same contract as the version report above.
    if args.print_gates:
        return print_gates()

    root = get_repo_root()

    # --install-hook mode: just set up the hook and exit
    if args.install_hook:
        return install_hook(root)

    # --install-branch-rules mode: apply the server-side GitHub ruleset
    if args.install_branch_rules:
        return install_branch_rules(root)

    # --gate mode: run quality checks only (called by pre-push hook)
    if args.gate:
        return run_gate(root)

    # Full publish pipeline — auto-detect bump type unless user forced one.
    # Idempotency: read REMOTE plugin.json (origin/master) as the bump
    # baseline. When local is ahead (interrupted publish: bumped + committed
    # but not pushed), bumping from local would double-bump. From remote,
    # bumping recomputes the SAME target as the original interrupted run,
    # and stage_bump's "already-at-target" guard then skips the bump.
    local = get_current_version(root)
    if not local:
        cprint(f"{RED}Cannot read version from .claude-plugin/plugin.json{NC}")
        return 1
    remote = _read_remote_version(root)
    baseline = remote or local

    if args.bump is None:
        bump_type = detect_bump_type(root)
        cprint(f"{BLUE}Bump type: {bump_type} (auto-detected from git-cliff){NC}")
    else:
        bump_type = args.bump
        cprint(f"{BLUE}Bump type: {bump_type} (forced via --{bump_type}){NC}")

    new_ver = bump_semver(baseline, bump_type)
    if not new_ver:
        cprint(f"{RED}Cannot parse baseline version: {baseline}{NC}")
        return 1

    if remote and local != remote:
        cprint(f"{YELLOW}Local plugin.json is at {local} but origin is at {remote} — "
               f"using remote as bump baseline (interrupted-publish recovery).{NC}")
    current = baseline

    cprint(f"\n{BOLD}Publish pipeline: {current} -> {new_ver}{NC}")
    if args.dry_run:
        cprint(f"{YELLOW}(dry-run mode — no changes will be made){NC}")

    # Gate 0: reject bypass attempts BEFORE running any other stage.
    # Pipeline order (per the cornerstone rule "every push is a bump"):
    #   lint+typecheck → tests → validate → ci-preflight → marketplace-reg →
    #   consistency → bump → badge → changelog → commit → push → github release
    # Lint runs before tests (cheap fails first). Tests run before validate
    # so behavioral regressions fail the test suite before the structural
    # validator inspects the manifest.
    #
    # EVERY check above runs BEFORE stage_bump. That ordering is the whole point
    # of stage_ci_preflight: a CI-parity defect aborts the publish with the tree
    # untouched, instead of being discovered on GitHub after the tag was pushed
    # and the release was cut.
    stage_bypass_guard()
    stage_check_clean(root)
    stage_lint(root)
    stage_tests(root)  # MANDATORY — no skip flag, no exceptions
    stage_validate(root)
    # Both of these exist as gate stages too. They are ALSO pipeline stages
    # because a plugin whose hooks were never installed would otherwise publish
    # having run neither (audit rows 6 and 14).
    stage_secret_scan(root)
    stage_fork_parity(root)
    stage_ci_preflight(root)  # MANDATORY — the gates validate_plugin omits
    stage_marketplace_registration(root)  # Gate 6 parity with CPV's own publish.py
    stage_consistency(root)
    stage_bump(root, new_ver, args.dry_run)
    stage_update_badges(root, current, new_ver, args.dry_run)
    stage_changelog(root, new_ver, args.dry_run)
    stage_commit_and_push(root, new_ver, args.dry_run)
    stage_gh_release(root, new_ver, args.dry_run)
    # Runs AFTER the release on purpose — it verifies the commit that actually
    # shipped. Never aborts (the release is already public; see its docstring).
    stage_verify_ci_green(root, args.dry_run)
    # Same post-release contract: no static check catches an uninstallable
    # release, so the only proof is installing it (ai-maestro#62 R2).
    stage_install_smoke(root, new_ver, args.dry_run)

    cprint(f"\n{GREEN}{BOLD}Published {new_ver} successfully!{NC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
