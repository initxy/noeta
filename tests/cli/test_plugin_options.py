"""A Claude Code plugin's ``userConfig`` and ``settings.json``, as Claude
Code takes them: values asked for on ``noeta plugin add`` (or given with
``--config``), kept in ``~/.noeta/settings.json`` ``pluginConfigs`` (a
sensitive one in the keyring, by reference), substituted into MCP servers,
hooks, commands and agents; the plugin's ``agent`` setting picks the agent
front ends start with, below the person's own settings."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from noeta.cli import home_dir, main, plugin_options
from noeta.cli.config import from_settings, load_settings

FIELDS = {
    "region": {"type": "string", "title": "Region", "description": "Where to deploy",
               "default": "eu"},
    "token": {"type": "string", "title": "Token", "description": "API token",
              "sensitive": True, "required": True},
    "retries": {"type": "number", "title": "Retries", "description": "How often",
                "min": 0, "max": 5},
    "dry": {"type": "boolean", "title": "Dry run", "description": "Only print"},
}


def make_plugin(root: Path, *, settings: dict[str, Any] | None = None) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "deploy-tools", "userConfig": FIELDS}))
    (root / "commands").mkdir()
    (root / "commands" / "go.md").write_text(
        "Deploy to ${user_config.region} with ${user_config.token}; "
        "${user_config.missing} stays.\n")
    (root / "agents").mkdir()
    (root / "agents" / "deployer.md").write_text(
        "---\nname: deployer\ndescription: Deploys\n---\nRegion ${user_config.region}\n")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "check", "args": ["${user_config.region}"]},
            {"type": "command", "command": "echo ${user_config.region}"},
            {"type": "command", "command": "plain"}]}]}}))
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "api": {"command": "node", "env": {"TOKEN": "${user_config.token}",
                                           "REGION": "${user_config.region}"}},
        "other": {"command": "x", "args": ["${user_config.unknown}"]}}}))
    if settings is not None:
        (root / "settings.json").write_text(json.dumps(settings))
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    monkeypatch.setattr(home_dir, "HOME", home)
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n')
    return home


def ext(app: Any, agent: str, name: str) -> Any:
    return next(e for e in app.runtime.agents[agent].extensions if e.name == name)


def kept(home: Path) -> dict[str, Any]:
    return dict(json.loads((home / "settings.json").read_text())["pluginConfigs"]
                ["deploy-tools"]["options"])


async def test_values_are_substituted_as_claude_code_does(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOY_TOKEN", "s3cret")
    root = make_plugin(home / "plugins" / "deploy").resolve()
    notes = plugin_options.configure("deploy-tools", FIELDS,
                                     ["token={env:DEPLOY_TOKEN}", "retries=3"],
                                     interactive=False)
    assert notes == ["1 userConfig option not yet set: dry — run noeta plugin configure "
                     "deploy-tools, or pass --config KEY=VALUE"]
    assert kept(home) == {"token": "{env:DEPLOY_TOKEN}", "retries": 3, "region": "eu"}
    app = from_settings(load_settings(), cwd=str(tmp_path))
    # MCP servers get every value, the sensitive one resolved only when the
    # plugin is made; a server naming an unset key does not start.
    assert ext(app, "main", "mcp")._servers == {
        "api": {"command": "node", "env": {"TOKEN": "s3cret", "REGION": "eu"}}}
    [matcher] = ext(app, "main", "hooks").matchers["PreToolUse"]
    hooks = matcher.hooks
    assert [h.command for h in hooks] == ["check", "plain"]  # shell form with a value: out
    assert hooks[0].args == ("eu",)
    assert dict(hooks[1].env)["CLAUDE_PLUGIN_OPTION_REGION"] == "eu"
    assert dict(hooks[1].env)["CLAUDE_PLUGIN_OPTION_TOKEN"] == "s3cret"
    # Commands and agents: non-sensitive values only.
    assert ext(app, "main", "markdown_commands").render("deploy-tools:go", "") == (
        "Deploy to eu with [sensitive option 'token' not available in skill content]; "
        "${user_config.missing} stays.")
    assert app.runtime.agents["deployer"].system_prompt == "Region eu\n"
    assert root.is_dir()
    await app.store.close()


def test_a_sensitive_value_goes_to_the_keyring(home: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[tuple[str, str], str] = {}
    fake = types.ModuleType("keyring")
    fake.set_password = lambda s, a, v: saved.__setitem__((s, a), v)  # type: ignore[attr-defined]
    fake.delete_password = lambda s, a: saved.pop((s, a))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    answers = iter(["", "tok", "9", "2", "maybe", "yes"])
    plugin_options.configure("deploy-tools", FIELDS, interactive=True,
                             ask=lambda _p: next(answers), ask_secret=lambda _p: next(answers))
    assert saved == {("noeta-plugin", "deploy-tools/token"): "tok"}
    assert kept(home) == {"region": "eu", "token": "{keyring:noeta-plugin/deploy-tools/token}",
                          "retries": 2, "dry": True}
    plugin_options.forget("deploy-tools")
    assert saved == {}
    assert "pluginConfigs" not in json.loads((home / "settings.json").read_text())


def test_bad_config_values_are_refused(home: Path) -> None:
    for given, said in ((["nope=1"], "isn't declared"), (["retries=9"], "above its maximum"),
                        (["dry=perhaps"], "not a boolean"), (["region"], "KEY=VALUE"),
                        (["region="], "value is empty")):
        with pytest.raises(plugin_options.OptionError, match=said):
            plugin_options.configure("deploy-tools", FIELDS, given, interactive=False)
    with pytest.raises(plugin_options.OptionError, match="declares no userConfig"):
        plugin_options.configure("other", {}, ["a=b"], interactive=False)


def test_plugin_add_and_configure_take_config(home: Path, tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    source = make_plugin(tmp_path / "src" / "deploy")
    with pytest.raises(SystemExit) as done:
        main.main(["plugin", "add", str(source), "--config", "token={env:T}"])
    assert done.value.code == 0
    assert kept(home)["token"] == "{env:T}"
    assert "2 userConfig options not yet set: retries, dry" in capsys.readouterr().err
    with pytest.raises(SystemExit) as done:
        main.main(["plugin", "configure", "deploy-tools", "--config", "dry=off"])
    assert done.value.code == 0
    assert kept(home)["dry"] is False
    with pytest.raises(SystemExit) as done:
        main.main(["plugin", "remove", "deploy-tools"])
    assert "pluginConfigs" not in json.loads((home / "settings.json").read_text())


async def test_a_plugins_agent_setting_picks_the_agent_front_ends_start_with(
        home: Path, tmp_path: Path) -> None:
    make_plugin(home / "plugins" / "deploy", settings={"agent": "deploy-tools:deployer",
                                                        "statusLine": {"type": "command"}})
    app = from_settings(load_settings(), cwd=str(tmp_path))
    assert app.main_agent == "deployer"
    await app.store.close()
    # The person's own settings win over a plugin's.
    (home / "settings.json").write_text(json.dumps({"agent": "Explore"}))
    app = from_settings(load_settings(), cwd=str(tmp_path))
    assert app.main_agent == "Explore"
    await app.store.close()
    (home / "settings.json").write_text(json.dumps({"agent": "nobody"}))
    app = from_settings(load_settings(), cwd=str(tmp_path))
    assert app.main_agent == "main"
    assert any("'nobody'" in n for n in app.notices)
    await app.store.close()


async def test_a_hook_runs_with_its_option_environment(tmp_path: Path) -> None:
    from noeta.plugins.hooks.config import HookCommand
    from noeta.plugins.hooks.runners import run_command

    hook = HookCommand(command='printf %s "$CLAUDE_PLUGIN_OPTION_REGION"',
                       env=(("CLAUDE_PLUGIN_OPTION_REGION", "eu"),))
    assert (await run_command(hook, {}, str(tmp_path))).stdout == "eu"
