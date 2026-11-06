"""noeta and noeta-tui are released together, at one version (docs/releasing.md)."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text())["project"]


def test_both_packages_carry_one_version_and_pin_each_other() -> None:
    noeta = _project(ROOT / "pyproject.toml")
    tui = _project(ROOT / "tui" / "pyproject.toml")
    version = noeta["version"]
    assert tui["version"] == version
    assert noeta["optional-dependencies"]["tui"] == [f"noeta-tui=={version}"]
    assert f"noeta=={version}" in tui["dependencies"]


def test_the_terminal_ui_ships_the_same_license() -> None:
    assert (ROOT / "tui" / "LICENSE").read_text() == (ROOT / "LICENSE").read_text()
