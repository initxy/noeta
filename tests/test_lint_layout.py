"""``scripts/lint-layout.py`` passes on this repository and catches code put
in the wrong package."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lint-layout.py"


def lint(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), str(root)],
                          capture_output=True, text=True, check=False)


def write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_this_repository_is_clean() -> None:
    result = lint(ROOT)
    assert result.returncode == 0, result.stdout


def test_a_kernel_module_only_a_plugin_uses_fails(tmp_path: Path) -> None:
    write(tmp_path, "noeta/__init__.py")
    write(tmp_path, "noeta/core/__init__.py")
    write(tmp_path, "noeta/core/types.py", "from noeta.core import runner\n")
    write(tmp_path, "noeta/core/runner.py", "from . import types\n")
    write(tmp_path, "noeta/core/questions.py")
    write(tmp_path, "noeta/core/commands.py")
    write(tmp_path, "noeta/core/shared.py")
    write(tmp_path, "noeta/plugins/fs.py", "from noeta.core.shared import x\n")
    result = lint(tmp_path)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "lint-layout: noeta/core/shared.py: the kernel does not use it; move it to noeta/toolkit"
        " (shared by plugins) or a plugin, or list it in scripts/lint-layout.py KERNEL_SURFACE"
        " with the reason",
    ]


def test_an_unknown_top_level_package_fails(tmp_path: Path) -> None:
    write(tmp_path, "noeta/core/__init__.py")
    write(tmp_path, "noeta/utils/__init__.py")
    result = lint(tmp_path)
    assert result.returncode == 1
    assert "noeta/utils: not a known top-level package" in result.stdout
