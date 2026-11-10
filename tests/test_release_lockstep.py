"""noeta and noeta-tui are released together, at one version (docs/releasing.md)."""

from __future__ import annotations

import json
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


def test_the_web_packages_carry_noetas_version() -> None:
    version = _project(ROOT / "pyproject.toml")["version"]
    web = ROOT / "web"
    sdk = json.loads((web / "sdk" / "package.json").read_text())
    ui = json.loads((web / "ui" / "package.json").read_text())
    app = json.loads((web / "package.json").read_text())
    assert sdk["version"] == ui["version"] == version
    assert ui["dependencies"][sdk["name"]] == version
    assert app["dependencies"][sdk["name"]] == app["dependencies"][ui["name"]] == version
