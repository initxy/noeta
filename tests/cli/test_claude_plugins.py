"""Claude Code plugin directories in noeta's own plugin places: skills,
agents, commands, hooks and MCP servers load through the plugins that read
those formats; nothing is read from Claude Code's own places."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from noeta.cli import home_dir, main
from noeta.cli.config import from_settings, load_settings
from noeta.cli.trust import trust_project
from noeta.core.commands import expand_command
from noeta.core.extension import CommandContext
from noeta.toolkit.commands import command_message


def make_plugin(root: Path, *, manifest: bool = True) -> Path:
    if manifest:
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": "deploy-tools", "version": "1.2.0",
             "commands": ["./commands/", "./extra/about.md"]}))
    (root / "skills" / "deploy").mkdir(parents=True)
    (root / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Deploy the app\n---\nRun the deploy.\n")
    (root / "commands").mkdir()
    (root / "commands" / "status.md").write_text(
        "---\ndescription: Show the status\nargument-hint: '[env]'\n"
        "allowed-tools: [Read, 'Bash(${CLAUDE_PLUGIN_ROOT}/bin/status:*)']\n---\n"
        "Show the deploy status of $ARGUMENTS using ${CLAUDE_PLUGIN_ROOT}/bin/status.\n")
    (root / "extra").mkdir()
    (root / "extra" / "about.md").write_text("Explain this plugin.\n")
    (root / "agents").mkdir()
    (root / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews deploys\n---\nScripts: ${CLAUDE_PLUGIN_ROOT}\n")
    (root / "agents" / "Explore.md").write_text("---\nname: Explore\n---\nmine\n")
    (root / "agents" / "ops").mkdir()
    (root / "agents" / "ops" / "pager.md").write_text("---\ndescription: Pages\n---\nPage.\n")
    (root / "commands" / "ops").mkdir()
    (root / "commands" / "ops" / "rollback.md").write_text("Roll back $ARGUMENTS.\n")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": '"${CLAUDE_PLUGIN_ROOT}"/check.sh'}]}]}}))
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"deploy-api": {
        "command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"],
        "env": {"CACHE": "${CLAUDE_PLUGIN_DATA}"}}}}))
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


async def test_a_claude_code_plugin_loads_through_noetas_plugins(home: Path,
                                                                 tmp_path: Path) -> None:
    root = make_plugin(home / "plugins" / "deploy").resolve()
    app = from_settings(load_settings(), cwd=str(tmp_path))
    skills = ext(app, "main", "skills")
    assert skills.plugin_dirs == {str(root / "skills"): "deploy-tools"}
    assert [c.name for c in skills._commands(str(tmp_path))] == ["deploy-tools:deploy"]
    [matcher] = ext(app, "main", "hooks").matchers["PreToolUse"]
    assert matcher.hooks[0].command == f'"{root}"/check.sh'
    assert ext(app, "main", "mcp")._servers == {"deploy-api": {
        "command": "node", "args": [f"{root}/server.js"],
        "env": {"CACHE": str(home / "plugin-data" / "deploy-tools")}}}
    # Claude Code's names: <plugin>:<agent>, a subdirectory before the name.
    reviewer = app.runtime.agents["deploy-tools:reviewer"]
    assert reviewer.system_prompt == f"Scripts: {root}\n"
    assert "reviewer" not in app.runtime.agents
    assert app.runtime.agents["Explore"].system_prompt != "mine\n"
    assert app.runtime.agents["deploy-tools:Explore"].system_prompt == "mine\n"
    assert "deploy-tools:ops:pager" in app.runtime.agents
    ctx = CommandContext(cwd=str(tmp_path))
    main_agent = app.runtime.agents["main"]
    assert await expand_command(main_agent, ctx, "/deploy-tools:status prod") == \
        command_message("deploy-tools:status", "prod")
    commands = ext(app, "main", "markdown_commands")
    assert commands.render("deploy-tools:status", "prod") == \
        f"Show the deploy status of prod using {root}/bin/status."
    assert commands._allow["deploy-tools:status"] == ("Read", f"Bash({root}/bin/status:*)")
    assert commands.render("deploy-tools:about", "") == "Explain this plugin."
    assert commands.render("deploy-tools:ops:rollback", "v2") == "Roll back v2."
    await app.store.close()


async def test_a_project_plugin_loads_once_trusted_without_its_mcp_servers(
        home: Path, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    make_plugin(project / ".noeta" / "plugins" / "deploy", manifest=False)
    app = from_settings(load_settings(), cwd=str(project))
    assert "deploy:reviewer" not in app.runtime.agents
    await app.store.close()
    trust_project(str(project))
    app = from_settings(load_settings(), cwd=str(project))
    assert "deploy:reviewer" in app.runtime.agents
    assert ext(app, "main", "mcp")._servers == ()
    assert any("do not start from a project" in n for n in app.notices)
    await app.store.close()


def test_noeta_plugins_lists_them(home: Path, tmp_path: Path,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    root = make_plugin(home / "plugins" / "deploy").resolve()
    main.main(["--cwd", str(tmp_path), "plugins"])
    assert f"deploy-tools\t{root}\t(Claude Code plugin 1.2.0)" in capsys.readouterr().out


async def test_skills_dirs_in_config_toml_still_add_their_skills_beside_a_plugins(
        home: Path, tmp_path: Path) -> None:
    """``[plugins.skills] dirs`` (as written for 0.x) keeps working and no
    longer replaces a Claude Code plugin's skill directories."""
    root = make_plugin(home / "plugins" / "deploy").resolve()
    mine = tmp_path / "mine"
    (mine / "tidy").mkdir(parents=True)
    (mine / "tidy" / "SKILL.md").write_text("---\nname: tidy\ndescription: Tidy.\n---\nx\n")
    with (home / "config.toml").open("a") as f:
        f.write(f'\n[plugins.skills]\ndirs = ["{mine}"]\n')
    app = from_settings(load_settings(), cwd=str(tmp_path))
    skills = ext(app, "main", "skills")
    assert skills.dirs == [str(mine)]
    assert skills.plugin_dirs == {str(root / "skills"): "deploy-tools"}
    assert sorted(c.name for c in skills._commands(str(tmp_path))) == [
        "deploy-tools:deploy", "tidy"]
    await app.store.close()
