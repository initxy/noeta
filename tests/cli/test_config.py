"""config.toml: the plugin tables, and what the ready-made front ends build
from them."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from noeta.cli import config, home_dir, trust
from noeta.cli.config import from_settings, load_settings, merge_plugin_settings
from noeta.cli.settings_files import permission_arguments
from noeta.plugins.web import Web


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(home_dir, "HOME", tmp_path)
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


def test_an_unknown_top_level_key_warns_and_is_ignored(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    (home / "config.toml").write_text(
        'model = "m"\napikey = "k"\n[agents.main]\nadd_plugins = []\n[plugins.web]\n')
    with caplog.at_level("WARNING", logger="noeta.cli"):
        s = load_settings()
    assert s.model == "m" and s.api_key != "k"
    assert [r.getMessage() for r in caplog.records] == [
        f"{home / 'config.toml'}: unknown top-level keys ['apikey']; ignored"]


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


@pytest.mark.parametrize("front_end", [True, False])
def test_only_a_front_end_names_the_persons_home_for_mentions(home: Path,
                                                              front_end: bool) -> None:
    (home / "config.toml").write_text(
        f'model = "m"\nprovider = "anthropic"\napi_key = "k"\nstore = "sqlite:///{home / "t.db"}"\n')
    app = from_settings(load_settings(), cwd=str(home), own_tasks_only=front_end)
    [fs] = [e for e in app.runtime.agents["main"].extensions if e.name == "fs"]
    assert fs.home == (str(Path.home()) if front_end else None)  # type: ignore[attr-defined]


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
    trust.trust_project(str(project))
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


def test_trusting_a_project_applies_its_settings_files_at_once(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.cli.main import trust_here

    project = home / "proj"
    (project / ".noeta").mkdir(parents=True)
    (home / "config.toml").write_text(
        f'model = "m"\nprovider = "anthropic"\napi_key = "k"\n'
        f'store = "sqlite:///{home / "t.db"}"\n')
    monkeypatch.delenv("TRUSTED_ONLY", raising=False)
    (project / ".noeta" / "settings.json").write_text(json.dumps({
        "env": {"TRUSTED_ONLY": "1"}, "permissions": {"deny": ["WebFetch"]}}))
    app = from_settings(load_settings(cwd=str(project)), cwd=str(project))

    def denied() -> list[str]:
        [perms] = [e for e in app.runtime.agents["main"].extensions if e.name == "permissions"]
        return [str(rule) for rule in perms.deny_rules]  # type: ignore[attr-defined]

    assert not any("WebFetch" in rule for rule in denied())
    trust_here(app, str(project))
    assert any("WebFetch" in rule for rule in denied())  # no restart needed
    # what is read only at the start says so, on every new task's start
    assert "the settings files' env changed: restart noeta to apply it" in app.notices
    assert "TRUSTED_ONLY" not in os.environ


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
    trust.trust_project(str(project))

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
    trust.trust_project(str(project))
    found = config.file_settings(str(project))
    assert "WebFetch(domain:evil.com)" in found.permissions["allow"]
    assert found.sandbox_permissions == {"deny": ["Read(secret/**)"],
                                         "allow": ["WebFetch(domain:b.com)"]}
    app = from_settings(load_settings(api_key="k", model="m"), cwd=str(project))
    env = app.runtime.env_for(TaskState(task_id="t", config={"cwd": str(project)}))
    settings = env.sandbox.settings  # type: ignore[attr-defined]
    assert settings.allowed_domains == ("a.com", "b.com") and settings.allow_write == ()
    assert settings.deny_read == ("secret",)


def test_serve_refuses_bypass_in_the_plugin_itself(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``noeta serve --disable-bypass-permissions`` turns on the plugin's own
    refusal, so no way in (a signal, an answer, a settings file's
    ``defaultMode``) can switch a served task into bypassPermissions;
    without it bypass is allowed."""
    import asyncio
    import types

    import uvicorn

    from noeta.cli import main

    (home / "settings.json").write_text(json.dumps(
        {"permissions": {"defaultMode": "bypassPermissions"}}))
    (home / "config.toml").write_text(
        'model = "m"\nprovider = "anthropic"\napi_key = "k"\n'
        f'store = "sqlite:///{home / "t.db"}"\n')
    apps: list[Any] = []
    real_app = main._app

    def capture(s: Any, **kw: Any) -> Any:
        apps.append(real_app(s, **kw))
        return apps[-1]

    class _Server:
        def __init__(self, _config: object) -> None:
            pass

        async def serve(self) -> None:
            return None

    monkeypatch.setattr(main, "_app", capture)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    for allow in (False, True):
        args = types.SimpleNamespace(host="127.0.0.1", no_worker=True, no_token=True,
                                     allow_origin=[], disable_bypass_permissions=not allow,
                                     allow_host=[], port=0, web=None,
                                     allow_plugin_install=False,
                                     permission_mode=None)
        asyncio.run(main._serve(load_settings(cwd=str(home)), args, str(home)))
        exts = {e.name: e for e in apps[-1].runtime.agents["main"].extensions}
        perms = exts["permissions"]
        assert perms.disable_bypass is not allow
        assert (perms.mode == "bypassPermissions") is allow


def test_secrets_noetas_config_hands_its_plugins_stay_out_of_commands(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A chat channel's shared secret in a command's environment would let an
    # injected command forge the chat app's requests.
    from noeta.toolkit import local_env

    monkeypatch.setattr(local_env, "_withheld", set())
    for name in ("CHAT_HOOK_TOKEN", "SEARCH_KEY", "HOOK_SECRET", "AGENT_SECRET", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "x")
    (home / "config.toml").write_text(
        'model = "m"\nprovider = "anthropic"\napi_key = "k"\n'
        f'store = "sqlite:///{home / "t.db"}"\n'
        '[channels.chat_approvals]\ntoken = "{env:CHAT_HOOK_TOKEN}"\n'
        '[plugins.web]\nsearch_api_key = "{env:SEARCH_KEY}"\n'
        '[agents.main]\nadd_plugins = [{hooks = {secret = "Bearer {env:HOOK_SECRET}"}}]\n')
    s = load_settings()
    (home / "agents").mkdir()
    (home / "agents" / "helper.md").write_text(
        "---\nname: helper\nplugins:\n  - web: {search_api_key: '{env:AGENT_SECRET}'}\n"
        "---\nHelp.\n")
    from_settings(s, cwd=str(home))
    env = local_env.child_env()
    assert not {"CHAT_HOOK_TOKEN", "SEARCH_KEY", "HOOK_SECRET", "AGENT_SECRET"} & set(env)
    assert env["GITHUB_TOKEN"] == "x"  # the person's own, as Claude Code passes it


def test_language_and_attribution_settings_reach_the_environment(home: Path) -> None:
    (home / "config.toml").write_text(
        f'model = "m"\nprovider = "anthropic"\napi_key = "k"\nstore = "sqlite:///{home / "t.db"}"\n')
    (home / "managed").mkdir()
    (home / "settings.json").write_text(json.dumps({
        "language": "Deutsch", "attribution": {"commit": "Assisted-by: AI", "pr": "x"}}))
    (home / "managed" / "managed-settings.json").write_text(json.dumps({
        "attribution": {"pr": ""}}))
    found = config.file_settings(str(home))
    assert found.language == "Deutsch"
    assert found.attribution == {"commit": "Assisted-by: AI", "pr": ""}
    assert config.environment_arguments(found) == {
        "language": "Deutsch", "attribution_commit": "Assisted-by: AI", "attribution_pr": ""}
    app = from_settings(load_settings(cwd=str(home)), cwd=str(home))
    [env] = [e for e in app.runtime.agents["main"].extensions if e.name == "environment"]
    assert env.language == "Deutsch"  # type: ignore[attr-defined]
    assert (env.attribution_commit, env.attribution_pr) == ("Assisted-by: AI", "")  # type: ignore[attr-defined]
    # includeCoAuthoredBy: false (the older setting) leaves both lines out.
    (home / "settings.json").write_text(json.dumps({"includeCoAuthoredBy": False}))
    (home / "managed" / "managed-settings.json").write_text("{}")
    assert config.file_settings(str(home)).attribution == {"commit": "", "pr": ""}


# -- providers --------------------------------------------------------------


def test_one_provider_config_is_unchanged(home: Path) -> None:
    from noeta.backends.providers.anthropic import AnthropicProvider

    (home / "config.toml").write_text('model = "m"\nprovider = "anthropic"\napi_key = "k"\n')
    s = load_settings()
    assert s.providers == {} and s.model_providers == {}
    router = s.make_router()
    assert list(router.endpoints) == ["default"] and router.family == "anthropic"
    assert isinstance(router.provider_for("claude-opus-5-5"), AnthropicProvider)
    # An uncatalogued model is settled (its window looked up) first.
    assert router.provider_for("m") is not router.provider_for("claude-opus-5-5")
    router.prepare("m")
    assert router.provider_for("m") is router.provider_for("claude-opus-5-5")


def test_provider_tables_route_models(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.backends.providers.anthropic import AnthropicProvider
    from noeta.backends.providers.openai_compat import OpenAICompatProvider
    from noeta.core import models

    monkeypatch.setenv("DS_KEY", "sk-ds")
    big = f"ds-big-{os.getpid()}"
    (home / "models.json").write_text(json.dumps({"models": [
        {"id": "gw-chat", "context_window": 64000, "max_output_tokens": 8000,
         "provider": "gw"}]}))
    (home / "config.toml").write_text(
        'model = "claude-opus-5-5"\napi_key = "k"\n'
        f'models_file = "{home / "models.json"}"\n'
        f'store = "sqlite:///{home / "t.db"}"\n'
        '[providers.deepseek]\nfamily = "openai_compat"\n'
        'base_url = "https://api.deepseek.com/v1"\napi_key = "{env:DS_KEY}"\n'
        'headers = {"x-team" = "a"}\n'
        f'models = ["deepseek-chat", {{id = "{big}", context_window = 131072, '
        'max_output_tokens = 65536}]\n'
        '[providers.gw]\nfamily = "openai_compat"\nbase_url = "https://gw/v1"\n'
        'api_key = "g"\nlist_models = false\n')
    s = load_settings()
    assert s.model_providers == {"gw-chat": "gw", "deepseek-chat": "deepseek", big: "deepseek"}
    assert models.model_limits(big) == (131072, 65536)
    app = from_settings(s, cwd=str(home))
    assert app.prepare_model is not None
    app.prepare_model("claude-opus-5-5")  # reads the (empty) listings

    def route(model: str) -> Any:
        provider = app.runtime.provider_for(model)
        return getattr(provider, "_inner", provider)

    assert isinstance(route("claude-opus-5-5"), AnthropicProvider)
    deepseek = route("deepseek-chat")
    assert isinstance(deepseek, OpenAICompatProvider) and deepseek is route(big)
    assert deepseek._client.headers["authorization"] == "Bearer sk-ds"  # type: ignore[attr-defined]
    assert deepseek._client.headers["x-team"] == "a"  # type: ignore[attr-defined]
    assert route("gw-chat") is not deepseek
    assert app.list_models is not None and app.prepare_model is not None
    assert ("deepseek-chat", "deepseek") in app.list_models()
    assert app.prepare_model("deepseek-chat") is not None  # uncatalogued, unlisted
    assert app.prepare_model(big) is None


def test_a_table_can_be_the_default_provider(home: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.cli.config import MissingAPIKey

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (home / "config.toml").write_text(
        'model = "deepseek-chat"\nprovider = "deepseek"\n'
        '[providers.deepseek]\nfamily = "openai_compat"\nbase_url = "https://x/v1"\n'
        'api_key = "k"\n'
        '[providers.other]\nfamily = "openai_compat"\nbase_url = "https://y/v1"\n')
    s = load_settings()
    router = s.make_router()
    assert list(router.endpoints) == ["deepseek", "other"]
    assert s.make_provider().family == "openai_compat"
    with pytest.raises(MissingAPIKey, match="provider 'other'"):
        s._table_provider("other")


async def test_a_tables_missing_secret_fails_only_its_calls(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.core.errors import Fatal
    from noeta.core.types import ModelRequest

    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    (home / "config.toml").write_text(
        'model = "claude-opus-5-5"\napi_key = "k"\n'
        '[providers.x]\nfamily = "openai_compat"\nbase_url = "https://x/v1"\n'
        'api_key = "{env:NOT_SET_ANYWHERE}"\nmodels = ["x-model"]\n')
    router = load_settings().make_router()
    router.provider_for("claude-opus-5-5")
    with pytest.raises(Fatal, match="NOT_SET_ANYWHERE"):
        await router.provider_for("x-model").complete(ModelRequest(model="x-model"))


def test_the_top_level_key_reads_secret_references(home: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.backends.providers.openai_compat import OpenAICompatProvider
    from noeta.toolkit import local_env

    monkeypatch.setattr(local_env, "_withheld", set())
    monkeypatch.delenv("NOETA_API_KEY", raising=False)
    monkeypatch.setenv("TOP_KEY", "sk-top")
    monkeypatch.setenv("TOP_TEAM", "a")
    (home / "config.toml").write_text(
        'model = "m"\nprovider = "openai_compat"\nbase_url = "https://x/v1"\n'
        'api_key = "{env:TOP_KEY}"\nextra_headers = {"x-team" = "{env:TOP_TEAM}"}\n')
    s = load_settings()
    assert s.api_key == "{env:TOP_KEY}"  # the reference is what settings hold
    provider = s.make_provider()
    inner = getattr(provider, "_inner", provider)
    assert isinstance(inner, OpenAICompatProvider)
    assert inner._client.headers["authorization"] == "Bearer sk-top"  # type: ignore[attr-defined]
    assert inner._client.headers["x-team"] == "a"  # type: ignore[attr-defined]
    assert not {"TOP_KEY", "TOP_TEAM"} & set(local_env.child_env())
    monkeypatch.delenv("TOP_KEY")
    with pytest.raises(ValueError, match="TOP_KEY"):
        s.make_provider()


def test_an_anthropic_provider_takes_the_one_hour_cache(home: Path) -> None:
    (home / "config.toml").write_text(
        'model = "claude-opus-5-5"\napi_key = "k"\nprompt_cache_ttl = "1h"\n'
        '[providers.plain]\nfamily = "anthropic"\napi_key = "k"\n'
        '[providers.hour]\nfamily = "anthropic"\napi_key = "k"\nprompt_cache_ttl = "1h"\n'
        '[providers.gw]\nfamily = "openai_compat"\nbase_url = "https://x/v1"\napi_key = "k"\n'
        'prompt_cache_ttl = "1h"\n')
    s = load_settings()
    assert s.make_provider()._cache_ttl == "1h"  # type: ignore[attr-defined]
    assert s._table_provider("plain")._cache_ttl == "5m"  # type: ignore[attr-defined]
    assert s._table_provider("hour")._cache_ttl == "1h"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="anthropic provider's setting"):
        s._table_provider("gw")


@pytest.mark.parametrize(("text", "match"), [
    ('[providers.a]\nbase_url = "x"\n', "family is required"),
    ('[providers.a]\nfamily = "openai_compat"\nkey = "x"\n', "unknown keys"),
    ('[providers.default]\nfamily = "openai_compat"\n', "pick another name"),
    ('[providers.a]\nfamily = "openai_compat"\nmodels = [1]\n', "models is a list"),
    ('[providers.a]\nfamily = "openai_compat"\nheaders = "h"\n', "headers"),
    ('[providers]\na = 1\n', "tables"),
])
def test_bad_provider_tables(home: Path, text: str, match: str) -> None:
    (home / "config.toml").write_text(text)
    with pytest.raises(ValueError, match=match):
        load_settings()


def test_a_row_without_supports_vision_still_sends_images(home: Path) -> None:
    """Leaving the field out means "unknown", not "text-only": a table of
    gateway models that only gives limits must not strip every image."""
    from noeta.backends.providers._common import model_admits_images

    unsaid, text_only = f"gw-unsaid-{os.getpid()}", f"gw-text-{os.getpid()}"
    (home / "config.toml").write_text(
        '[providers.gw]\nfamily = "anthropic"\nbase_url = "https://gw"\napi_key = "g"\n'
        f'models = [{{id = "{unsaid}", context_window = 1000000, max_output_tokens = 128000}},'
        f' {{id = "{text_only}", context_window = 1000, max_output_tokens = 100,'
        ' supports_vision = false}]\n')
    load_settings()
    assert model_admits_images(unsaid)
    assert not model_admits_images(text_only)


def test_a_models_file_row_names_a_configured_provider(home: Path) -> None:
    (home / "models.json").write_text(json.dumps([{"id": "q", "provider": "nobody"}]))
    (home / "config.toml").write_text(f'models_file = "{home / "models.json"}"\n')
    with pytest.raises(ValueError, match="nobody"):
        load_settings()


def test_a_model_named_for_two_providers_is_an_error(home: Path) -> None:
    (home / "models.json").write_text(json.dumps([{"id": "m1", "provider": "b"}]))
    tables = ('[providers.a]\nfamily = "openai_compat"\nmodels = ["m1"]\n'
              '[providers.b]\nfamily = "openai_compat"\nmodels = ["m2"]\n')
    (home / "config.toml").write_text(tables.replace('"m2"', '"m1"'))
    with pytest.raises(ValueError, match=r"'m1' is named for two providers: 'a' "
                                         r"\(\[providers.a\]\) and 'b' \(\[providers.b\]\)"):
        load_settings()
    (home / "config.toml").write_text(f'models_file = "{home / "models.json"}"\n' + tables)
    with pytest.raises(ValueError, match=r"models\.json"):
        load_settings()
