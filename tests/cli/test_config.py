"""config.toml: the plugin tables, and what the ready-made front ends build
from them."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from noeta.cli import config
from noeta.cli.config import from_settings, load_settings, merge_plugin_settings
from noeta.cli.settings_files import permission_arguments
from noeta.plugins.web import Web


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "HOME", tmp_path)
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
    # MCP is on: its servers come from settings.json and consented .noeta/mcp.json.
    [mcp] = [e for e in agents["main"].extensions if e.name == "mcp"]
    assert mcp._settings_files == (  # type: ignore[attr-defined]
        str(home / "settings.json"), str(home / "managed" / "managed-settings.json"))


def test_settings_files_layer_in_claude_codes_precedence(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = home / "proj"
    (project / ".noeta").mkdir(parents=True)
    (home / "managed").mkdir()
    monkeypatch.setenv("PARITY_A", "unset")
    monkeypatch.setenv("PARITY_B", "unset")
    (home / "settings.json").write_text(json.dumps({
        "model": "user-model", "env": {"PARITY_A": "user", "PARITY_B": "user"},
        "permissions": {"allow": ["Bash(make:*)"], "defaultMode": "acceptEdits"}}))
    (project / ".noeta" / "settings.json").write_text(json.dumps({
        "env": {"PARITY_A": "project"},
        "permissions": {"allow": ["Read", "Bash(make:*)"], "deny": ["WebFetch"],
                        "additionalDirectories": ["~/shared"]}}))
    (project / ".noeta" / "settings.local.json").write_text(json.dumps({
        "permissions": {"ask": ["Bash(npm publish:*)"], "defaultMode": "bypassPermissions"}}))
    (home / "managed" / "managed-settings.json").write_text(json.dumps({
        "permissions": {"deny": ["Bash(curl:*)"]}}))
    # Untrusted: the project's files are not read.
    found = config.file_settings(str(project))
    assert found.permissions == {"allow": ["Bash(make:*)"], "deny": ["Bash(curl:*)"],
                                 "defaultMode": "acceptEdits"}
    config.trust_project(str(project))
    found = config.file_settings(str(project))
    assert found.env == {"PARITY_A": "project", "PARITY_B": "user"}
    assert found.permissions == {
        "allow": ["Bash(make:*)", "Read"], "deny": ["WebFetch", "Bash(curl:*)"],
        "ask": ["Bash(npm publish:*)"], "additionalDirectories": ["~/shared"],
        "defaultMode": "bypassPermissions"}
    s = load_settings(cwd=str(project), api_key="k")
    assert s.model == "user-model"
    assert os.environ["PARITY_A"] == "project" and os.environ["PARITY_B"] == "user"
    # The permissions plugin gets plain data, only the arguments it takes.
    assert permission_arguments(found, {"mode", "allow"}) == {
        "mode": "bypassPermissions", "allow": ["Bash(make:*)", "Read"]}
    app = from_settings(s, cwd=str(project))
    exts = {e.name: e for e in app.runtime.agents["main"].extensions}
    assert exts["permissions"].mode == "bypassPermissions"  # type: ignore[attr-defined]
    assert exts["instructions"].user_dir == str(home)  # type: ignore[attr-defined]
    assert exts["hooks"] is not None


def test_claude_codes_own_places_are_never_read(
        home: Path, tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """noeta reads only its own places: a trusted project's ``.claude/``,
    ``CLAUDE.md`` and ``.mcp.json`` and the user's ``~/.claude/`` are left
    alone, even with consent to every project MCP server."""
    from noeta.plugins.mcp.settings import read_servers

    user_home = tmp_path_factory.mktemp("user")
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user_home / ".claude"))
    monkeypatch.setenv("PARITY_C", "unset")
    front = "---\nname: {0}\ndescription: {0}\n---\nbody\n"
    project = home / "proj"
    for root in (user_home / ".claude", project / ".claude"):
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "cc-agent.md").write_text(front.format("cc-agent"))
        (root / "skills" / "cc-skill").mkdir(parents=True)
        (root / "skills" / "cc-skill" / "SKILL.md").write_text(front.format("cc-skill"))
        (root / "settings.json").write_text(json.dumps({
            "model": "cc-model", "env": {"PARITY_C": "claude"},
            "permissions": {"deny": ["Read"]}}))
        (root / "CLAUDE.md").write_text("claude instructions\n")
    (project / "CLAUDE.md").write_text("claude instructions\n")
    (project / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"cc": {"command": "cc-server"}}}))
    (project / ".noeta").mkdir()
    (project / ".noeta" / "settings.json").write_text(json.dumps(
        {"enableAllProjectMcpServers": True}))
    config.trust_project(str(project))

    found = config.file_settings(str(project))
    assert not any(".claude" in str(f) for f in found.files)
    assert found.model is None and found.env == {} and found.permissions == {}
    assert read_servers([str(f) for f in found.files], [str(project)]) == []
    app = from_settings(load_settings(cwd=str(project), api_key="k", model="m"),
                        cwd=str(project))
    assert os.environ["PARITY_C"] == "unset"
    assert "cc-agent" not in app.runtime.agents
    exts = {e.name: e for e in app.runtime.agents["main"].extensions}
    assert exts["instructions"].user_dir == str(home)  # type: ignore[attr-defined]
    tiers = exts["skills"].tiers(str(project))  # type: ignore[attr-defined]
    assert tiers and not any(".claude" in d for d, _tier in tiers)


def test_sandbox_settings_merge_and_reach_the_env(home: Path) -> None:
    """``sandbox`` lists add up; a project may tighten the sandbox, never
    loosen it; the front end's tasks get a local env with it."""
    from noeta.cli.settings_files import merge_sandbox
    from noeta.core.loop.state import TaskState

    merged = merge_sandbox([
        ({"enabled": True, "allowUnsandboxedCommands": True, "bwrapPath": "/u/bwrap",
          "network": {"allowedDomains": ["a.com"], "allowLocalBinding": True}}, "user"),
        ({"enabled": False, "allowUnsandboxedCommands": False, "allowNestedUserNamespaces": True,
          "filesystem": {"disabled": True, "denyRead": ["x"], "allowWrite": ["~"],
                         "allowRead": ["~/.ssh"]},
          "excludedCommands": ["*"],
          "network": {"allowedDomains": ["b.com", "a.com"], "deniedDomains": ["c.com"],
                      "allowUnixSockets": ["/var/run/docker.sock"]}}, "project"),
        ({"bwrapPath": "/m/bwrap", "excludedCommands": ["docker *"]}, "managed")])
    assert merged == {
        "enabled": True, "allowUnsandboxedCommands": False, "bwrapPath": "/m/bwrap",
        "network": {"allowedDomains": ["a.com"], "allowLocalBinding": True,
                    "deniedDomains": ["c.com"]},
        "filesystem": {"denyRead": ["x"]}, "excludedCommands": ["docker *"]}
    assert merge_sandbox([({"bwrapPath": "/u/bwrap"}, "user")]) == {}
    assert merge_sandbox([({"enabled": True}, "project")]) == {"enabled": True}

    project = home / "proj"
    project.mkdir()
    (home / "settings.json").write_text(json.dumps({
        "sandbox": {"enabled": True, "network": {"allowedDomains": ["a.com"]}},
        "permissions": {"allow": ["WebFetch(domain:b.com)"]}}))
    found = config.file_settings(str(project))
    assert found.sandbox == {"enabled": True, "network": {"allowedDomains": ["a.com"]}}
    app = from_settings(load_settings(api_key="k", model="m"), cwd=str(project))
    env = app.runtime.env_for(TaskState(task_id="t", config={"cwd": str(project)}))
    sandbox = getattr(env, "sandbox", None)
    assert sandbox is not None and sandbox.settings.allowed_domains == ("a.com", "b.com")

    # A trusted project's permission rules widen what its tools may do
    # unasked, not the sandbox: its allow rules and directories stay out.
    (project / ".noeta").mkdir()
    (project / ".noeta" / "settings.json").write_text(json.dumps({"permissions": {
        "allow": ["WebFetch(domain:evil.com)", "Edit(~/**)"],
        "additionalDirectories": ["/"], "deny": ["Read(secret/**)"]}}))
    config.trust_project(str(project))
    found = config.file_settings(str(project))
    assert "WebFetch(domain:evil.com)" in found.permissions["allow"]
    assert found.sandbox_permissions == {"deny": ["Read(secret/**)"],
                                         "allow": ["WebFetch(domain:b.com)"]}
    app = from_settings(load_settings(api_key="k", model="m"), cwd=str(project))
    env = app.runtime.env_for(TaskState(task_id="t", config={"cwd": str(project)}))
    settings = env.sandbox.settings  # type: ignore[attr-defined]
    assert settings.allowed_domains == ("a.com", "b.com") and settings.allow_write == ()
    assert settings.deny_read == ("secret",)
