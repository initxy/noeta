"""Run import-linter against the ``.importlinter`` config in the repo root.

The layering contracts are the architecture's only mechanical enforcement:
``noeta.protocols`` is the typed boundary that may import nothing in-project,
``noeta.core`` sees only ``noeta.protocols``, and nothing statically imports
``noeta.builtins`` — the plugin loader's dynamic ``ref`` resolution is the
sole doorway. Shelling out to ``lint-imports`` exercises the same path CI
runs, so a contract cannot pass here and fail there.
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

# Skip only when import-linter itself is absent (the dev extra is missing and
# there is nothing to run). The skip is deliberately NOT keyed on
# ``shutil.which("lint-imports")``: the binary may live next to
# ``sys.executable`` in a venv without being on the shell PATH, so
# ``find_spec`` keeps the check independent of shell state.
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
