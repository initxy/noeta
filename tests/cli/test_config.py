"""config.toml: the plugin tables, and what the ready-made front ends build
from them."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from noeta.cli import config
from noeta.cli.claude_settings import permission_arguments
from noeta.cli.config import from_settings, load_settings, merge_plugin_settings
from noeta.plugins.web import Web


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "CLAUDE_HOME", tmp_path / "claude")
    monkeypatch.setattr(config, "managed_dir", lambda: tmp_path / "managed")
    for var in ("NOETA_MODEL", "NOETA_PROVIDER", "NOETA_STORE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_plugin_and_agent_tables(home: Path) -> None:
    (home / "config.toml").write_text(
        'model = "m"\n'
        '[plugins.shell]\nbackground = true\n'
        '[plugins.web]\nenabled = false\n'
        '[agents.main]\nadd_plugins = ["structured_output", {hooks = {}}]\n'
        '[agents."*"]\nadd_plugins = ["ask_user"]\n')
    s = load_settings()
    assert s.plugins == {"shell": {"background": True}, "web": {"enabled": False}}
    assert s.add_plugins == {"main": (("structured_output", {}), ("hooks", {})),
                             "*": (("ask_user", {}),)}


@pytest.mark.parametrize("text", [
    "[plugins]\nweb = 1\n",
    "[agents.main]\nplugins = []\n",
    "[agents.main]\nadd_plugins = \"hooks\"\n",
])
def test_bad_tables(home: Path, text: str) -> None:
    (home / "config.toml").write_text(text)
    with pytest.raises(ValueError):
        load_settings()


def test_merge() -> None:
    web = Web()
    assert merge_plugin_settings(
        {"memory": {"root": "a"}, "web": {"x": 1}},
        {"memory": {"read_only": True}, "web": web},
        {"memory": False}) == {"memory": False, "web": web}
    assert merge_plugin_settings({"memory": {"root": "a"}}, {"memory": {"read_only": True}}) \
        == {"memory": {"root": "a", "read_only": True}}


def test_from_settings_applies_the_tables(home: Path) -> None:
    (home / "config.toml").write_text(
        'model = "m"\nprovider = "anthropic"\napi_key = "k"\n'
        f'store = "sqlite:///{home / "t.db"}"\n'
        '[plugins.web]\nenabled = false\n'
        '[plugins.memory]\nrecall_model = "small"\n'
        '[agents.Explore]\nadd_plugins = ["ask_user"]\n')
    app = from_settings(load_settings(), cwd=str(home))
    agents = app.runtime.agents
    names = {n: {e.name for e in a.extensions} for n, a in agents.items()}
    assert "web" not in names["main"] and "web" not in names["general-purpose"]
    assert "ask_user" in names["Explore"]
    [memory] = [e for e in agents["main"].extensions if e.name == "memory"]
    # The front end's root, with config.toml's arguments laid over it.
    assert str(memory.home) == str(home) and memory.recall_model == "small"  # type: ignore[attr-defined]
    # MCP is on: its servers come from settings.json and consented .mcp.json.
    [mcp] = [e for e in agents["main"].extensions if e.name == "mcp"]
    assert mcp._settings_files == (  # type: ignore[attr-defined]
        str(home / "claude" / "settings.json"), str(home / "settings.json"),
        str(home / "managed" / "managed-settings.json"))


def test_claude_code_settings_layer_in_its_precedence(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = home / "proj"
    (project / ".claude").mkdir(parents=True)
    (home / "claude").mkdir()
    (home / "managed").mkdir()
    monkeypatch.setenv("PARITY_A", "unset")
    monkeypatch.setenv("PARITY_B", "unset")
    (home / "claude" / "settings.json").write_text(json.dumps({
        "model": "user-model", "env": {"PARITY_A": "user", "PARITY_B": "user"},
        "permissions": {"allow": ["Bash(make:*)"], "defaultMode": "acceptEdits"}}))
    (project / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"PARITY_A": "project"},
        "permissions": {"allow": ["Read", "Bash(make:*)"], "deny": ["WebFetch"],
                        "additionalDirectories": ["~/shared"]}}))
    (project / ".claude" / "settings.local.json").write_text(json.dumps({
        "permissions": {"ask": ["Bash(npm publish:*)"], "defaultMode": "bypassPermissions"}}))
    (home / "managed" / "managed-settings.json").write_text(json.dumps({
        "permissions": {"deny": ["Bash(curl:*)"]}}))
    # Untrusted: the project's files are not read.
    claude = config.claude_settings(str(project))
    assert claude.permissions == {"allow": ["Bash(make:*)"], "deny": ["Bash(curl:*)"],
                                  "defaultMode": "acceptEdits"}
    config.trust_project(str(project))
    claude = config.claude_settings(str(project))
    assert claude.env == {"PARITY_A": "project", "PARITY_B": "user"}
    assert claude.permissions == {
        "allow": ["Bash(make:*)", "Read"], "deny": ["WebFetch", "Bash(curl:*)"],
        "ask": ["Bash(npm publish:*)"], "additionalDirectories": ["~/shared"],
        "defaultMode": "bypassPermissions"}
    s = load_settings(cwd=str(project), api_key="k")
    assert s.model == "user-model"
    assert os.environ["PARITY_A"] == "project" and os.environ["PARITY_B"] == "user"
    # The permissions plugin gets plain data, only the arguments it takes.
    assert permission_arguments(claude, {"mode", "allow"}) == {
        "mode": "bypassPermissions", "allow": ["Bash(make:*)", "Read"]}
    app = from_settings(s, cwd=str(project))
    exts = {e.name: e for e in app.runtime.agents["main"].extensions}
    assert exts["permissions"].mode == "bypassPermissions"  # type: ignore[attr-defined]
    assert exts["instructions"].user_dir == str(home / "claude")  # type: ignore[attr-defined]
    assert exts["hooks"] is not None
