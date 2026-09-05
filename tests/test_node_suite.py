"""Pytest bridge that runs the Node.js test suite declared in the skill's package.json."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "agents-discipline"
PACKAGE_JSON = SKILL_DIR / "package.json"

_package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
_test_script: str = _package["scripts"]["test"]
_commands: list[str] = [part.strip() for part in _test_script.split("&&") if part.strip()]


@pytest.mark.parametrize("command", _commands)
def test_node_command(command: str) -> None:
    """Run one `&&`-separated command from package.json's `scripts.test` and assert success."""
    node = shutil.which("node")
    assert node is not None, "node executable not found on PATH"
    assert command.startswith("node "), f"unexpected non-node command: {command}"
    args = [node, command[len("node ") :]]
    result = subprocess.run(args, cwd=SKILL_DIR, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
    assert result.returncode == 0
