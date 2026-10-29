"""Settings for the ``noeta`` command and other ready-made front ends: which
model, which provider, where tasks are stored, and how the plugins are set.
Read from the environment, then ``~/.noeta/config.toml``; explicit arguments
win. The plugin part of ``config.toml``::

    [plugins.shell]            # arguments of a plugin, by its name
    background = true
    [plugins.web]
    enabled = false            # off for every agent
    [plugins.my_plugin]        # a third-party plugin's arguments

    [agents.main]
    add_plugins = ["my_plugin", {hooks = {}}]  # after the agent file's own
    [agents."*"]               # every agent
    add_plugins = []

    [channels.chat_bridge]     # a channel's arguments; run by `noeta worker` / `noeta serve`
    token = "{env:CHAT_TOKEN}"
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
import tomllib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from noeta.agents.agent_files import (
    AgentFile,
    PluginEntry,
    PluginSetting,
    parse_plugin_list,
)
from noeta.cli import home_dir
from noeta.cli.claude_plugins import ClaudePlugin, find_claude_plugins, plugin_settings
from noeta.cli.plugin_policy import plugin_policy
from noeta.cli.settings_files import (
    FileSettings,
    apply_env,
    managed_dir,
    permission_arguments,
    settings_layers,
)
from noeta.cli.settings_files import load as load_file_settings
from noeta.cli.trust import (
    PROJECT_GATED_DIRS,
    project_trusted,
    untrusted_notice,
)
from noeta.cli.trust import trust_project as trust_project
from noeta.core import models, registry
from noeta.core.events import DEFAULT_TENANT
from noeta.core.execenv import ExecEnv
from noeta.core.provider import Provider
from noeta.toolkit.paths import make_tenant_temp_dir
from noeta.toolkit.sandbox import SandboxSettings
from noeta.toolkit.tenant_fence import TenantFence, store_files

if TYPE_CHECKING:
    from noeta.core.agent import Agent
    from noeta.core.channel import Channel
    from noeta.core.loop.state import TaskState
    from noeta.host.local import LocalApp

log = logging.getLogger("noeta.cli")


@dataclass
class Settings:
    model: str = ""
    #: ``anthropic`` | ``openai_compat`` | ``openai_responses``.
    provider: str = "anthropic"
    base_url: str | None = None
    api_key: str | None = None
    #: ``sqlite:///path`` or ``postgres://...``.
    store: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    effort: str | None = None
    #: A JSON file of extra models: ``{"models": [{"id", "context_window",
    #: "max_output_tokens", "supports_vision"?, "family"?}]}``.
    models_file: str | None = None
    #: Prompt overrides for every task this front end starts (name -> text,
    #: see ``noeta.core.prompts``): ``--system-prompt`` sets ``system/main``,
    #: ``--append-system-prompt`` ``system/main.append``.
    prompts: dict[str, str] = field(default_factory=dict)
    #: Per plugin name: its arguments (``enabled = false`` turns it off).
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Per agent name (``"*"``: every agent): more plugins, after its own.
    add_plugins: dict[str, tuple[PluginEntry, ...]] = field(default_factory=dict)
    #: Per channel name: its arguments (``enabled = false`` turns it off).
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``[server]``: ``noeta serve`` / ``noeta worker`` switches
    #: (``shared_os_user_tenants``).
    server: dict[str, Any] = field(default_factory=dict)

    def make_provider(self) -> Provider:
        kw: dict[str, Any] = {}
        if self.extra_headers:
            kw["extra_headers"] = dict(self.extra_headers)
        if self.provider == "anthropic":
            kw["device_id"] = home_dir.device_id()
        return registry.provider(self.provider, base_url=self.base_url, api_key=self.api_key,
                                 **kw)

    @property
    def store_url(self) -> str:
        return self.store or f"sqlite:///{home_dir.HOME / 'tasks.db'}"


def file_settings(cwd: str | None, *, sources: Collection[str] | None = None,
                  flag: Path | None = None) -> FileSettings:
    """The settings files for a front end started in ``cwd``
    (:mod:`noeta.cli.settings_files`), the project's once it is trusted;
    ``sources`` and ``flag`` are ``--setting-sources`` and ``--settings``."""
    trusted = bool(cwd) and project_trusted(str(cwd))
    layers = settings_layers(cwd, noeta_home=home_dir.HOME, trusted=trusted, managed=managed_dir(),
                             sources=sources, flag=flag)
    return load_file_settings([p for p, _ in layers], [k for _, k in layers])


def load_settings(*, cwd: str | None = None, sources: Collection[str] | None = None,
                  flag: Path | None = None, **overrides: Any) -> Settings:
    """``cwd``: where the front end runs; its settings files set
    environment variables (applied here, before the rest is read) and the
    model when nothing else does. ``sources`` / ``flag``: as
    :func:`file_settings`."""
    found = file_settings(cwd, sources=sources, flag=flag)
    apply_env(found)
    data: dict[str, Any] = {}
    path = home_dir.HOME / "config.toml"
    if path.is_file():
        data.update(tomllib.loads(path.read_text("utf-8")))
    env = {
        "model": os.environ.get("NOETA_MODEL"),
        "provider": os.environ.get("NOETA_PROVIDER"),
        "base_url": os.environ.get("NOETA_BASE_URL"),
        "api_key": os.environ.get("NOETA_API_KEY"),
        "store": os.environ.get("NOETA_STORE"),
        "effort": os.environ.get("NOETA_EFFORT"),
        "models_file": os.environ.get("NOETA_MODELS_FILE"),
    }
    data.update({k: v for k, v in env.items() if v})
    data.update({k: v for k, v in overrides.items() if v is not None})
    if not data.get("model") and found.model:
        data["model"] = found.model
    data["plugins"] = _plugin_tables(data.get("plugins", {}))
    data["channels"] = _plugin_tables(data.get("channels", {}), "channels")
    data["add_plugins"] = _agent_tables(data.pop("agents", {}))
    if not isinstance(data.get("server", {}), dict):
        raise ValueError("config.toml: [server] is a table of switches")
    known = {f for f in Settings.__dataclass_fields__}
    settings = Settings(**{k: v for k, v in data.items() if k in known})
    if settings.models_file:
        register_models_file(settings.models_file)
    return settings


def _plugin_tables(value: Any, table: str = "plugins") -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
        raise ValueError(f"config.toml: [{table}.<name>] tables hold one's arguments")
    return {name: dict(args) for name, args in value.items()}


def _agent_tables(value: Any) -> dict[str, tuple[PluginEntry, ...]]:
    if not isinstance(value, dict):
        raise ValueError("config.toml: [agents.<name>] tables say what to add to an agent")
    out: dict[str, tuple[PluginEntry, ...]] = {}
    for name, table in value.items():
        unknown = set(table) - {"add_plugins"} if isinstance(table, dict) else {"?"}
        if unknown:
            raise ValueError(f"config.toml: [agents.{name}] takes only add_plugins")
        out[name] = parse_plugin_list(table.get("add_plugins", []),
                                      f"config.toml [agents.{name}] add_plugins")
    return out


def _permission_arguments(found: FileSettings) -> dict[str, Any]:
    """The permissions plugin's arguments from the settings files, those it
    takes; a ``defaultMode`` it does not know is left out (warned)."""
    import inspect

    try:
        plugin = registry.load(registry.PLUGINS, "permissions")
    except registry.UnknownName:
        return {}
    accepted = set(inspect.signature(plugin).parameters)
    args = permission_arguments(found, accepted)
    if "mode" in args:
        try:
            plugin(mode=args["mode"])
        except ValueError:
            log.warning("settings: permission mode %r is not one noeta has; ignored",
                        args.pop("mode"))
    return args


def local_env_for(sandbox: SandboxSettings | None,
                      fence: TenantFence | None = None) -> Any:
    """The runtime's ``env_for``: each task's local env on its ``cwd``, with
    the settings' sandbox (when enabled); a task of a tenant other than the
    default one also gets ``fence``, checked on every file it opens, its
    tenant's own temporary directory, and the sandbox
    :meth:`TenantFence.confine` makes of the settings."""
    def env_for(state: TaskState) -> ExecEnv:
        cwd = str(state.config.get("cwd") or os.getcwd())
        kw: dict[str, Any] = {"sandbox": sandbox} if sandbox is not None else {}
        if fence is not None and state.tenant != DEFAULT_TENANT:
            kw["fence"] = functools.partial(_fence_check, fence, state, cwd)
            kw["temp_dir"] = make_tenant_temp_dir(fence.temp_dir(state.tenant))
            if sandbox is not None:
                kw["sandbox"] = fence.confine(sandbox, tenant=state.tenant,
                                              task_id=state.task_id,
                                              root_id=state.root_id or state.task_id, cwd=cwd)
        return registry.execenv("local", cwd, **kw)

    return env_for


def _fence_check(fence: TenantFence, state: TaskState, cwd: str, path: str,
                 write: bool) -> str | None:
    return fence.refusal(path, tenant=state.tenant, task_id=state.task_id,
                         root_id=state.root_id or state.task_id, cwd=cwd, write=write)


def merge_plugin_settings(*tables: Mapping[str, PluginSetting]) -> dict[str, PluginSetting]:
    """Plugin settings laid over each other: two argument tables of one
    plugin merge key by key; anything else (an instance, on/off) replaces."""
    out: dict[str, PluginSetting] = {}
    for table in tables:
        for name, value in table.items():
            prev = out.get(name)
            if isinstance(prev, Mapping) and isinstance(value, Mapping):
                out[name] = {**prev, **value}
            else:
                out[name] = value
    return out


def register_models_file(path: str) -> None:
    """Register the models a JSON file describes (gateway models the shipped
    catalog does not know), so they run at their real limits."""
    import json

    from noeta.core.models import ModelSpec, find_spec, register_models

    rows = json.loads(Path(path).expanduser().read_text("utf-8"))
    rows = rows.get("models", rows) if isinstance(rows, dict) else rows
    specs = {}
    for row in rows:
        if not row.get("context_window") or not row.get("max_output_tokens"):
            continue
        if find_spec(row["id"]) is not None:
            continue
        specs[row["id"]] = ModelSpec(
            real_model_id=row["id"], context_window=int(row["context_window"]),
            max_output_tokens=int(row["max_output_tokens"]),
            supports_vision=bool(row.get("supports_vision", False)),
            is_reasoning=bool(row.get("efforts")), vendor=row.get("family"))
    if specs:
        register_models(specs)


def open_store(url: str) -> Any:
    """``sqlite:///path`` or ``postgres://…`` → a store."""
    return registry.store(url)


def _shipped(name: str) -> bool:
    return registry.sources(registry.PLUGINS).get(name) == "package noeta"


def load_plugin_files(cwd: str | None) -> list[str]:
    """Load the plugin files of ``~/.noeta/plugins`` and, for a trusted
    project, ``<cwd>/.noeta/plugins``. Returns notices for the person: a
    project's plugin directory left alone because it is not trusted."""
    dirs = [home_dir.HOME / "plugins"]
    notice = untrusted_notice(cwd, ".noeta/plugins")
    if cwd and notice is None and (Path(cwd).expanduser() / ".noeta" / "plugins").is_dir():
        dirs.append(Path(cwd).expanduser().resolve() / ".noeta" / "plugins")
    registry.load_plugin_files(dirs, admit=plugin_policy(managed_dir()).admits)
    return [notice] if notice else []


def _permission_settings(found: FileSettings, cwd: str | None,
                         permissions: Mapping[str, Any] | None) -> dict[str, Any]:
    """The permissions plugin's arguments: the settings files', with the
    command line's (``permissions``) rule lists and directories added and
    its other arguments laid over."""
    permission_args = _permission_arguments(found)
    for key, value in (permissions or {}).items():
        if key in ("allow", "deny", "ask", "additional_directories"):
            permission_args[key] = [*permission_args.get(key, ()), *value]
        else:
            permission_args[key] = value
    if permission_args.get("disable_bypass_mode") and \
            permission_args.get("mode") == "bypassPermissions":
        log.warning("permission mode bypassPermissions is disabled here; ignored")
        del permission_args["mode"]
    if cwd and project_trusted(cwd):
        permission_args["trusted_workspace"] = True
    return permission_args


def _base_plugin_settings(found: FileSettings, cwd: str | None,
                          permissions: Mapping[str, Any] | None, *,
                          project: bool) -> dict[str, PluginSetting]:
    """The built-in plugins' settings from noeta's places and the settings
    files; ``project``: the project's MCP servers may load."""
    setting_files = [str(f) for f in found.files]
    permission_args = _permission_settings(found, cwd, permissions)
    # The file tools may write in the additional directories too.
    write_roots = [str(Path(cwd or os.getcwd(), d))
                   for d in permission_args.get("additional_directories") or ()]
    return merge_plugin_settings(
        {"memory": {"home": str(home_dir.HOME)},
         "hooks": {"settings_files": setting_files},
         "mcp": {"settings_files": setting_files,
                 "project": project_trusted if project else False},
         "skills": {"trusted": project_trusted, "global_dir": str(home_dir.HOME / "skills")},
         "instructions": {"user_dir": str(home_dir.HOME), "managed_dir": str(managed_dir())},
         "permissions": permission_args, "fs": {"write_roots": write_roots}})


def _apply_sandbox(found: FileSettings, cwd: str | None, runtime_kw: dict[str, Any],
                   fence: TenantFence) -> list[str]:
    """Run tasks' commands in the settings' sandbox (``runtime_kw``'s
    ``env_for``) when it is enabled, and tenants' file reads and writes
    behind ``fence``; a notice when the sandbox cannot start here."""
    sandbox = SandboxSettings.from_settings(found.sandbox, found.sandbox_permissions,
                                            base_dir=str(cwd or os.getcwd()))
    runtime_kw.setdefault("env_for", local_env_for(
        sandbox if sandbox.enabled else None, fence))
    if not sandbox.enabled:
        return []
    probe = registry.execenv("local", str(cwd or os.getcwd()), sandbox=sandbox)
    reason = getattr(getattr(probe, "sandbox", None), "unavailable", lambda: None)()
    if reason is None:
        return []
    notice = (f"sandbox.enabled is set but the sandbox cannot start: {reason}. "
              + ("Commands that would run in it are refused (failIfUnavailable)."
                 if sandbox.fail_if_unavailable else
                 "Commands run without it."))
    log.warning("%s", notice)
    return [notice]


def from_settings(settings: Settings, *, cwd: str | None = None,
                  plugins: Mapping[str, PluginSetting] | None = None,
                  permissions: Mapping[str, Any] | None = None,
                  own_tasks_only: bool = False, with_channels: bool = False,
                  setting_sources: Collection[str] | None = None,
                  flag_settings: Path | None = None,
                  agent_files: Sequence[AgentFile] = (),
                  tenant_workspaces: Mapping[str, str] | None = None,
                  **runtime_kw: Any) -> LocalApp:
    """The default agents on the configured store, model and provider, with
    the user's plugin files (``~/.noeta/plugins``, and a trusted project's
    ``<cwd>/.noeta/plugins``), memory (``~/.noeta/projects/<workspace>/memory``), prompt
    overrides (``~/.noeta/prompts``), the settings files (env, model,
    permissions, hooks, MCP consent; :mod:`noeta.cli.settings_files`) and
    instruction files (``~/.noeta/NOETA.md`` and ``rules/``, the managed
    ones), the ``~/.noeta/skills`` skills, the ``.noeta/mcp.json`` servers the
    settings consent to, agent files (``~/.noeta/agents``, then
    ``<cwd>/.noeta/agents``; a later file wins a name) and the plugin
    settings of ``config.toml``; ``plugins`` (the front end's own) goes over
    those. ``permissions``: the permissions plugin's arguments from the
    command line (``--allowedTools`` …); rule lists and directories are added
    to the settings files' own. A project's :data:`PROJECT_GATED_DIRS` load only once it is
    trusted; each one skipped is a notice in ``app.notices``. Front ends
    (``-p``, ``acp``, the TUI) pass ``own_tasks_only`` so their worker never
    picks up another process's task from the shared store; ``worker`` and
    ``serve`` serve every task and pass ``with_channels`` to run the
    channels ``config.toml`` enables. ``setting_sources`` / ``flag_settings``
    (``--setting-sources`` / ``--settings``) choose the settings layers
    (:func:`file_settings`); a left-out ``user`` source also skips
    ``~/.noeta/agents``, a left-out ``project`` source the project's agents
    and its ``.noeta/mcp.json``. ``agent_files`` (``--agents``) come last
    and win a name. ``tenant_workspaces`` (tenant -> workspace) joins the
    places a non-default tenant's task may not touch
    (:mod:`noeta.toolkit.tenant_fence`), with noeta's home, the store's
    files and the spill directory."""
    from noeta.agents import default_agents
    from noeta.agents.agent_files import read_agent_dir
    from noeta.host.local import LocalApp

    home_dir.ensure_home()
    notices = home_dir.loose_permissions(settings.store_url)
    notices += load_plugin_files(cwd)
    notices += [n for sub in PROJECT_GATED_DIRS if sub != ".noeta/plugins"
                if (n := untrusted_notice(cwd, sub)) is not None]
    if (unknown := models.unknown_model_notice(settings.model)) is not None:
        notices.append(unknown)
    for notice in notices:
        log.warning("%s", notice)
    found = file_settings(cwd, sources=setting_sources, flag=flag_settings)
    apply_env(found)
    if not settings.model and found.model:
        settings = dataclasses.replace(settings, model=found.model)

    def source(name: str) -> bool:
        return setting_sources is None or name in setting_sources

    runtime_kw.setdefault("spill_root", str(home_dir.HOME / "tool-results"))
    fence = TenantFence(home=str(home_dir.HOME), spill_root=runtime_kw["spill_root"],
                        plans_dir=str(home_dir.HOME / "plans"),
                        store_files=store_files(settings.store_url),
                        workspaces=dict(tenant_workspaces or {}),
                        server_dirs=tuple((permissions or {}).get("server_dirs") or ()))
    permissions = {"tenant_workspaces": dict(fence.workspaces),
                   "store_files": list(fence.store_files),
                   "spill_root": fence.spill_root, **(permissions or {})}
    base = _base_plugin_settings(found, cwd, permissions, project=source("project"))
    made: dict[Any, Any] = {}
    model, add_plugins = settings.model, settings.add_plugins

    def make_agents() -> list[Agent]:
        """The agents from the agent files and plugins found now; a plugin
        whose code and arguments did not change is the one made before."""
        claude, found_notices = claude_plugins(cwd)
        notices.extend(n for n in found_notices if n not in notices)
        files = [a for p in claude for a in p.agents if a.name not in BUILTIN_AGENTS]
        if source("user"):
            files += read_agent_dir(home_dir.HOME / "agents")
        if cwd and project_trusted(cwd) and source("project"):
            files += read_agent_dir(Path(cwd) / ".noeta" / "agents")
        files += agent_files
        from_plugins, plugin_notices = plugin_settings(claude)
        notices.extend(n for n in plugin_notices if n not in notices)
        table = merge_plugin_settings(base, from_plugins, settings.plugins, plugins or {})
        policy = plugin_policy(managed_dir())
        table.update({name: False for name in registry.available(registry.PLUGINS)
                      if not policy.admits(name, shipped=_shipped(name))})
        add = {agent: tuple(entries) for agent, entries in add_plugins.items()}
        for agent, names in registry.added_by_files().items():
            add[agent] = (*add.get(agent, ()), *((name, {}) for name in names))
        if "markdown_commands" in from_plugins:
            add["*"] = (*add.get("*", ()), ("markdown_commands", {}))
        return default_agents(model, plugins=table, add=add, extra_files=files, made=made)

    agents = make_agents()
    notices += _apply_sandbox(found, cwd, runtime_kw, fence)
    runtime_kw.setdefault("prompt_dirs", [str(home_dir.HOME / "prompts")])
    runtime_kw.setdefault("workspace_trusted", project_trusted)
    app = LocalApp(open_store(settings.store_url), agents,
                   settings.make_provider(), own_tasks_only=own_tasks_only,
                   task_config={"prompts": settings.prompts} if settings.prompts else None,
                   channels=make_channels(settings.channels) if with_channels else (),
                   **runtime_kw)
    app.notices = notices

    def reload_plugins() -> list[str]:
        registry.forget_plugin_files()
        found = load_plugin_files(cwd)
        app.replace_agents(make_agents())
        return found

    app.reload_plugins = reload_plugins
    return app


def make_channels(tables: Mapping[str, Mapping[str, Any]]) -> list[Channel]:
    """The channels ``config.toml`` sets (``[channels.<name>]``, by name in
    the ``noeta.channels`` group or a plugin file), made with their
    arguments; ``enabled = false`` leaves one out."""
    from noeta.agents.agent_files import resolve_secrets

    out: list[Channel] = []
    policy = plugin_policy(managed_dir())
    for name, table in tables.items():
        args = dict(table)
        if args.pop("enabled", True) is False or not policy.admits(name):
            continue
        cls = registry.load(registry.CHANNELS, name)
        out.append(cls(**resolve_secrets(args, f"channel {name!r}")))
    return out


#: The built-in agents' names: a plugin's agent file never replaces one.
BUILTIN_AGENTS = frozenset({"main", "general-purpose", "Explore", "Plan"})


def claude_plugins(cwd: str | None) -> tuple[list[ClaudePlugin], list[str]]:
    """The Claude Code plugin directories of ``~/.noeta/plugins`` and, for a
    trusted project, ``<cwd>/.noeta/plugins`` (:mod:`noeta.cli.claude_plugins`),
    with notices for the person (an agent named like a built-in one)."""
    places = [(home_dir.HOME / "plugins", False)]
    if cwd and project_trusted(cwd):
        places.append((Path(cwd).expanduser().resolve() / ".noeta" / "plugins", True))
    policy = plugin_policy(managed_dir())
    found = [p for p in find_claude_plugins(places, home_dir.HOME / "plugin-data")
             if policy.admits(p.name)]
    notices = [f"plugin {p.name}: agent {a.name!r} is a built-in agent's name; not loaded"
               for p in found for a in p.agents if a.name in BUILTIN_AGENTS]
    return found, notices


def plugin_changes(before: Mapping[str, Agent], after: Mapping[str, Agent]) -> list[str]:
    """What a reload changed, one line per plugin added, removed or changed
    (its code or its arguments), over every agent."""
    from noeta.core.registry import pin

    def pins(agents: Mapping[str, Agent]) -> dict[str, dict[str, str]]:
        return {e.name: pin(e) for a in agents.values() for e in a.extensions}

    old, new = pins(before), pins(after)
    lines = [f"added {n}" for n in sorted(new.keys() - old.keys())]
    lines += [f"removed {n}" for n in sorted(old.keys() - new.keys())]
    lines += [f"changed {n}" for n in sorted(old.keys() & new.keys()) if old[n] != new[n]]
    return lines
