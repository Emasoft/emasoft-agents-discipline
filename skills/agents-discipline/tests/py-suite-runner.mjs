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
// cmdrun_tests.py is import-safe on Windows (resource import guarded, ps helpers
// return "" and every row that depends on seeing a live process gates on WIN),
// so it runs on ALL cells and the Windows ones produce real port findings.
const suites = ["tests/cmdrun_tests.py", "tests/python-lib-checks.py"];

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
