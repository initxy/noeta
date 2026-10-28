"""config.toml: the plugin tables, and what the ready-made front ends build
from them."""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.cli import config
from noeta.cli.config import from_settings, load_settings, merge_plugin_settings
from noeta.plugins.web import Web


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "HOME", tmp_path)
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
        '[plugins.memory]\nread_only = true\n'
        '[agents.explore]\nadd_plugins = ["ask_user"]\n')
    app = from_settings(load_settings(), cwd=str(home))
    agents = app.runtime.agents
    names = {n: {e.name for e in a.extensions} for n, a in agents.items()}
    assert "web" not in names["main"] and "web" not in names["general-purpose"]
    assert "ask_user" in names["explore"]
    [memory] = [e for e in agents["main"].extensions if e.name == "memory"]
    # The front end's root, with config.toml's arguments laid over it.
    assert memory.root == str(home / "memory") and memory.read_only  # type: ignore[attr-defined]
    # MCP is on: its servers come from settings.json and consented .mcp.json.
    [mcp] = [e for e in agents["main"].extensions if e.name == "mcp"]
    assert mcp._settings_files == (str(home / "settings.json"),)  # type: ignore[attr-defined]
