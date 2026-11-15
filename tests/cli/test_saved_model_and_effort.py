"""The model and effort the terminal UI's ``/model`` and ``/effort`` save,
in Claude Code's settings format: read back over config.toml's, under the
environment's and the arguments'."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from noeta.cli import config, home_dir
from noeta.cli.config import load_settings
from noeta.cli.settings_files import load, save_effort, save_model


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(home_dir, "HOME", tmp_path)
    monkeypatch.setattr(config, "managed_dir", lambda: tmp_path / "managed")
    for var in ("NOETA_MODEL", "NOETA_PROVIDER", "NOETA_STORE", "NOETA_EFFORT"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write(path: Path, data: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_a_models_own_effort_wins_within_a_file_and_a_higher_files_wins_over_it(
        tmp_path: Path) -> None:
    user = _write(tmp_path / "user.json", {
        "effortLevel": "medium",
        "modelSettings": {"m-a": {"effortLevel": "low"}, "m-b": {"effortLevel": "max"},
                          "m-c": "high"}})
    found = load([user])
    # max lasts a session and is never kept; a malformed entry is skipped.
    assert (found.effort, found.model_efforts) == ("medium", {"m-a": "low"})
    project = _write(tmp_path / "project.json", {"modelSettings": {"m-b": {"effortLevel": "xhigh"}}})
    assert load([user, project]).model_efforts == {"m-a": "low", "m-b": "xhigh"}
    managed = _write(tmp_path / "managed.json", {"effortLevel": "high"})
    found = load([user, project, managed])
    assert (found.effort, found.model_efforts) == ("high", {})


def test_saved_choices_outrank_config_toml_and_yield_to_the_environment_and_arguments(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "config.toml").write_text('model = "toml-model"\neffort = "low"\n'
                                      'provider = "anthropic"\napi_key = "k"\n')
    s = load_settings()
    assert (s.model, s.effort_for(s.model)) == ("toml-model", "low")
    save_model(home / "settings.json", "saved-model")
    save_effort(home / "settings.json", "saved-model", "high")
    s = load_settings()
    assert s.model == "saved-model"
    assert s.effort_for("saved-model") == "high"
    assert s.effort_for("other-model") == "low"  # config.toml's, for a model with none saved
    assert s.effort == "low"  # what serve / worker / acp start every task with
    assert load_settings(model="flag-model").model == "flag-model"
    given = load_settings(effort="xhigh")
    assert given.effort_for("saved-model") == "xhigh"
    monkeypatch.setenv("NOETA_MODEL", "env-model")
    monkeypatch.setenv("NOETA_EFFORT", "medium")
    s = load_settings()
    assert (s.model, s.effort_for("saved-model")) == ("env-model", "medium")


def test_saving_keeps_the_rest_of_the_file_and_its_mode(home: Path) -> None:
    path = _write(home / "settings.json", {
        "env": {"SECRET": "s"}, "permissions": {"allow": ["Read"]},
        "modelSettings": {"other": {"effortLevel": "low", "maxEffortLevel": "high"}}})
    os.chmod(path, 0o600)
    save_model(path, "m")
    save_effort(path, "m", "xhigh")
    save_effort(path, "other", None)
    assert json.loads(path.read_text()) == {
        "env": {"SECRET": "s"}, "permissions": {"allow": ["Read"]}, "model": "m",
        "modelSettings": {"other": {"maxEffortLevel": "high"}, "m": {"effortLevel": "xhigh"}}}
    assert path.stat().st_mode & 0o777 == 0o600
    save_effort(path, "m", None)
    save_effort(path, "other", None)
    data = json.loads(path.read_text())
    assert data["modelSettings"] == {"other": {"maxEffortLevel": "high"}}
    _write(path, {"modelSettings": {"m": {"effortLevel": "low"}}})
    save_effort(path, "m", None)
    assert json.loads(path.read_text()) == {}  # no empty tables left behind
    assert list(home.glob(".settings.json.*")) == []


def test_a_file_that_is_not_json_is_left_as_it_is(home: Path) -> None:
    path = home / "settings.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        save_model(path, "m")
    assert path.read_text() == "{not json"
    save_model(home / "new" / "settings.json", "m")  # a missing file is made
    assert json.loads((home / "new" / "settings.json").read_text()) == {"model": "m"}
