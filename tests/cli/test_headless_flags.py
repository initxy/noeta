"""Claude Code's headless settings flags, applied: ``--settings`` (a layer
above the project's), ``--setting-sources`` (which layers load),
``--mcp-config`` / ``--strict-mcp-config`` and ``--agents``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import noeta.cli.main as cli
from noeta.cli import config, headless, home_dir
from noeta.cli.config import from_settings, load_settings
from noeta.cli.settings_files import load, settings_layers


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(home_dir, "HOME", home)
    monkeypatch.setattr(config, "managed_dir", lambda: tmp_path / "managed")
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n')
    (home / "settings.json").write_text(json.dumps(
        {"permissions": {"deny": ["Bash(ls *)"]},
         "mcpServers": {"from_user": {"command": "true"}}}))
    return home


def _parse(*argv: str) -> Any:
    return cli._front_parser().parse_args(["-p", "go", *argv])


def test_layers_follow_the_sources_and_put_the_flag_below_managed(tmp_path: Path) -> None:
    flag = tmp_path / "flag.json"
    layers = settings_layers(str(tmp_path), noeta_home=tmp_path / "h", trusted=True,
                             managed=tmp_path / "m", sources=("local",), flag=flag)
    assert layers == [(tmp_path / ".noeta" / "settings.local.json", "project"),
                      (flag, "user"), (tmp_path / "m" / "managed-settings.json", "managed")]
    # A flag layer may widen the sandbox like the user's own settings.
    flag.write_text(json.dumps({"sandbox": {"network": {"allowedDomains": ["x.org"]}}}))
    found = load([p for p, _ in layers], [k for _, k in layers])
    assert found.sandbox == {"network": {"allowedDomains": ["x.org"]}}


def test_the_flags_reach_the_agents(home: Path, tmp_path: Path) -> None:
    args = _parse(
        "--settings", json.dumps({"permissions": {"deny": ["WebFetch"]}}),
        "--setting-sources", "project",
        "--mcp-config", json.dumps({"mcpServers": {"echo": {"command": "echo"}}}),
        "--strict-mcp-config",
        "--agents", json.dumps({"rev": {"description": "Reviews diffs.",
                                        "prompt": "You review.", "tools": ["Read"]}}))
    layers = {"sources": headless.setting_sources(args),
              "flag": headless.settings_layer(args, home / "flag-settings")}
    assert layers["sources"] == ("project",) and layers["flag"].parent == home / "flag-settings"
    mcp = headless.mcp_arguments(args)
    app = from_settings(load_settings(**layers), cwd=str(tmp_path), plugins={"mcp": mcp},
                        setting_sources=layers["sources"], flag_settings=layers["flag"],
                        agent_files=headless.agent_files(args))
    main = app.runtime.agents["main"]
    exts = {e.name: e for e in main.extensions}
    deny = [rule.text for rule in exts["permissions"].deny_rules]  # type: ignore[attr-defined]
    assert deny == ["WebFetch"]  # the user's file is not a chosen source
    assert exts["mcp"]._servers == {"echo": {"command": "echo"}}  # type: ignore[attr-defined]
    assert exts["mcp"]._settings_files == ()  # type: ignore[attr-defined]
    rev = app.runtime.agents["rev"]
    assert rev.description == "Reviews diffs." and "You review." in rev.system_prompt


def test_without_strict_mcp_the_settings_servers_stay(home: Path, tmp_path: Path) -> None:
    args = _parse("--mcp-config", json.dumps({"mcpServers": {"a": {"command": "a"}}}),
                  json.dumps({"mcpServers": {"b": {"url": "https://b.example"}}}))
    assert headless.mcp_arguments(args) == {
        "servers": {"a": {"command": "a"}, "b": {"url": "https://b.example"}}}
    assert headless.mcp_arguments(_parse()) is None


def test_bad_flag_values_are_refused(tmp_path: Path) -> None:
    with pytest.raises(headless.FlagError, match="unknown source"):
        headless.setting_sources(_parse("--setting-sources", "user,cloud"))
    assert headless.setting_sources(_parse("--setting-sources=")) == ()
    with pytest.raises(headless.FlagError, match="mcpServers"):
        headless.mcp_arguments(_parse("--mcp-config", "{}"))
    with pytest.raises(headless.FlagError, match="cannot read"):
        headless.settings_layer(_parse("--settings", str(tmp_path / "none.json")), tmp_path)
    with pytest.raises(headless.FlagError, match="prompt"):
        headless.agent_files(_parse("--agents", '{"x": {"description": "d"}}'))
    file = tmp_path / "s.json"
    file.write_text("{}")
    assert headless.settings_layer(_parse("--settings", str(file)), tmp_path) == file.resolve()


def test_the_command_line_passes_the_flags_on(home: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, Any] = {}

    def app(s: Any, **kw: Any) -> Any:
        seen.update(kw)
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_app", app)
    with pytest.raises(SystemExit):
        cli.main(["-p", "go", "--setting-sources=", "--strict-mcp-config",
                  "--agents", '{"rev": {"description": "d", "prompt": "p"}}',
                  "--cwd", str(tmp_path)])
    assert seen["setting_sources"] == () and seen["plugins"] == {
        "mcp": {"servers": {}, "settings_files": [], "project": False}}
    assert [a.name for a in seen["agent_files"]] == ["rev"]
    assert "ignored" not in capsys.readouterr().err
    with pytest.raises(SystemExit) as bad:
        cli.main(["-p", "go", "--agents", "[]", "--cwd", str(tmp_path)])
    assert "--agents" in str(bad.value.code)


def test_an_unknown_model_is_announced_once(home: Path, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str],
                                            caplog: pytest.LogCaptureFixture) -> None:
    from noeta.testing import ScriptedProvider, reply

    monkeypatch.setattr(config.Settings, "make_provider",
                        lambda self: ScriptedProvider([reply("ok")]))
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "go", "--model", "gateway-model-x1", "--cwd", str(tmp_path)])
    assert done.value.code == 0
    said = [*capsys.readouterr().err.splitlines(), *(r.getMessage() for r in caplog.records)]
    limits = [line for line in said if "gateway-model-x1" in line and "context window" in line]
    assert len(limits) == 1  # once, not from the loader, the front end and the catalog each
    # ... and its price is in that one notice, not announced again.
    assert [line for line in said if "gateway-model-x1" in line] == limits


def test_an_unknown_model_from_the_settings_file_is_announced_once(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    from noeta.testing import ScriptedProvider, reply

    (home / "config.toml").write_text(
        f'api_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n')
    (home / "settings.json").write_text(json.dumps({"model": "gateway-model-y2"}))
    monkeypatch.setattr(config.Settings, "make_provider",
                        lambda self: ScriptedProvider([reply("ok")]))
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "go", "--cwd", str(tmp_path)])
    assert done.value.code == 0
    said = [*capsys.readouterr().err.splitlines(), *(r.getMessage() for r in caplog.records)]
    named = [line for line in said if "gateway-model-y2" in line]
    assert len(named) == 1 and "doesn't recognize" in named[0], named
