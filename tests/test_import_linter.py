"""Run import-linter against the .importlinter config in the repo root.

Phase 0 enforces an L0-L3 import topology:

* L0 ``noeta.protocols`` — typed boundaries, must not import any
  other in-project module.
* L1 ``noeta.core`` — may import L0 only.
* L2 ``noeta.runtime`` / ``noeta.context`` / ``noeta.storage`` /
  ``noeta.policies`` / ``noeta.tools`` — may import L0 / L1; cross-L2
  edges only where unavoidable.

This test shells out to ``lint-imports`` so the same path CI runs is
exercised.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / ".importlinter"

# We skip only when import-linter itself is not installed in this Python
# environment — that means the dev extra is missing and there's nothing
# to run. We do not key the skip on ``shutil.which("lint-imports")``
# because the binary may live next to ``sys.executable`` (a venv) without
# being on the user's shell PATH; using ``importlib.util.find_spec``
# makes the check independent of shell state.
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("importlinter") is None,
    reason="import-linter (dev extra) not installed",
)


def _lint_imports_command() -> list[str]:
    """Locate the ``lint-imports`` executable that goes with this Python.

    Prefers the binary sitting next to ``sys.executable`` (works for any
    venv layout); falls back to ``PATH``; if neither exists we re-invoke
    Python with a small entry-point shim so the test still runs even
    when ``[scripts]`` was not installed.
    """
    here = Path(sys.executable).parent / "lint-imports"
    if here.is_file():
        return [str(here)]
    found = shutil.which("lint-imports")
    if found is not None:
        return [found]
    return [
        sys.executable,
        "-c",
        "from importlinter.cli import lint_imports_command; "
        "raise SystemExit(lint_imports_command())",
    ]


def test_importlinter_config_exists() -> None:
    assert CONFIG.is_file(), f"missing {CONFIG}"


def test_importlinter_passes_in_repo() -> None:
    cmd = _lint_imports_command() + ["--config", str(CONFIG)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"import-linter failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
