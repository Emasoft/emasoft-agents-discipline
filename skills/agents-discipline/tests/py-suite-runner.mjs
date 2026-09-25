// Runs the Python test suites from `npm test` with a Windows-safe interpreter name.
// `python3` does not exist on the GitHub Windows runners (setup-python exposes no
// python3 shim -- see test-matrix.yml), so the name is overridable via PYTHON, the
// same convention the AD_RUNTIME suites already use (run-tests.mjs PY, ledger-tests.mjs).
// The workflow sets PYTHON=python on Windows and python3 elsewhere, so the runner cell
// that needs it never resolves a bare python3.
import { spawnSync } from "node:child_process";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const py = process.env.PYTHON || "python3";
// cmdrun_stress.py is NOT in this list: the full stress run (all seeds) is a
// coordinator step, not a per-PR gate -- CI runs the deterministic unit suite.
// cmdrun_tests.py is POSIX-only BY DESIGN (see its module docstring): it kills
// process groups via os.killpg, snapshots the table via `ps`, and imports the
// POSIX-only `resource` module -- none of which exist on Windows. Gating ~150
// rows by hand would be a rewrite; the runner gates the whole suite by platform
// instead. The Windows cells still exercise cmdrun itself via python-lib-checks
// (parity) and via the winproof suite once it lands (TRDD-2U56GG7S step 8).
const suites =
  process.platform === "win32"
    ? ["tests/python-lib-checks.py"]
    : ["tests/cmdrun_tests.py", "tests/python-lib-checks.py"];

for (const suite of suites) {
  const r = spawnSync(py, [resolve(root, suite)], { stdio: "inherit" });
  if (r.error) {
    console.error(`py-suite-runner: cannot run ${py} ${suite}: ${r.error.message}`);
    process.exit(1);
  }
  if (r.status !== 0) {
    console.error(`py-suite-runner: ${suite} exited ${r.status}`);
    process.exit(r.status || 1);
  }
}
