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
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import secrets
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from noeta.agents.agent_files import (
    PluginEntry,
    PluginSetting,
    parse_plugin_list,
)
from noeta.cli.settings_files import (
    FileSettings,
    apply_env,
    managed_dir,
    permission_arguments,
    settings_files,
)
from noeta.cli.settings_files import load as load_file_settings
from noeta.core import models, registry
from noeta.core.execenv import ExecEnv
from noeta.core.provider import Provider
from noeta.toolkit.sandbox import SandboxSettings

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState
    from noeta.host.local import LocalApp

log = logging.getLogger("noeta.cli")

HOME = Path(os.environ.get("NOETA_HOME") or Path.home() / ".noeta")


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

    def make_provider(self) -> Provider:
        kw: dict[str, Any] = {}
        if self.extra_headers:
            kw["extra_headers"] = dict(self.extra_headers)
        if self.provider == "anthropic":
            kw["device_id"] = device_id()
        return registry.provider(self.provider, base_url=self.base_url, api_key=self.api_key,
                                 **kw)

    @property
    def store_url(self) -> str:
        return self.store or f"sqlite:///{HOME / 'tasks.db'}"


def device_id() -> str | None:
    """This machine's random id, made on first use (``~/.noeta/device_id``):
    the stable device id Claude Code sends in ``metadata.user_id``. ``None``
    when it cannot be kept (the provider then uses one per process)."""
    path = HOME / "device_id"
    try:
        text = path.read_text("utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    value = secrets.token_hex(32)
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", "utf-8")
    except OSError:
        return None
    return value


def server_token(*, create: bool = False) -> str | None:
    """The bearer token of ``noeta serve``: ``NOETA_SERVER_TOKEN``, else the
    one kept in ``~/.noeta/server-token`` (owner-only), made when
    ``create`` and missing."""
    if os.environ.get("NOETA_SERVER_TOKEN"):
        return os.environ["NOETA_SERVER_TOKEN"]
    path = HOME / "server-token"
    try:
        text = path.read_text("utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    if not create:
        return None
    value = secrets.token_urlsafe(32)
    HOME.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    return value


def file_settings(cwd: str | None) -> FileSettings:
    """The settings files for a front end started in ``cwd``
    (:mod:`noeta.cli.settings_files`), the project's once it is trusted."""
    trusted = bool(cwd) and project_trusted(str(cwd))
    return load_file_settings(settings_files(cwd, noeta_home=HOME, trusted=trusted,
                                             managed=managed_dir()))


def load_settings(*, cwd: str | None = None, **overrides: Any) -> Settings:
    """``cwd``: where the front end runs; its settings files set
    environment variables (applied here, before the rest is read) and the
    model when nothing else does."""
    found = file_settings(cwd)
    apply_env(found)
    data: dict[str, Any] = {}
    path = HOME / "config.toml"
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
    data["add_plugins"] = _agent_tables(data.pop("agents", {}))
    known = {f for f in Settings.__dataclass_fields__}
    settings = Settings(**{k: v for k, v in data.items() if k in known})
    if settings.models_file:
        register_models_file(settings.models_file)
    return settings


def _plugin_tables(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
        raise ValueError("config.toml: [plugins.<name>] tables hold a plugin's arguments")
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


def sandboxed_env_for(sandbox: SandboxSettings) -> Any:
    """The runtime's ``env_for``: each task's local env on its ``cwd``, with
    the settings' sandbox."""
    def env_for(state: TaskState) -> ExecEnv:
        cwd = str(state.config.get("cwd") or os.getcwd())
        return registry.execenv("local", cwd, sandbox=sandbox)

    return env_for


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


#: The projects whose gated directories (``PROJECT_GATED_DIRS``) may load:
#: ``{"projects": [path, ...]}``,
#: written by ``noeta trust``.
TRUST_FILE = "trusted.json"


def trusted_projects() -> list[str]:
    import json

    path = HOME / TRUST_FILE
    if not path.is_file():
        return []
    data = json.loads(path.read_text("utf-8"))
    projects = data.get("projects", []) if isinstance(data, dict) else []
    return [str(Path(p).expanduser().resolve()) for p in projects if isinstance(p, str)]


def trust_project(directory: str) -> str:
    """Add ``directory`` to the trusted projects; returns it resolved."""
    import json

    resolved = str(Path(directory).expanduser().resolve())
    projects = trusted_projects()
    if resolved not in projects:
        projects.append(resolved)
        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / TRUST_FILE).write_text(json.dumps({"projects": projects}, indent=2) + "\n",
                                       "utf-8")
    return resolved


def project_trusted(directory: str) -> bool:
    """Has the person trusted the project at ``directory`` (``noeta trust``)?"""
    return str(Path(directory).expanduser().resolve()) in trusted_projects()


#: A project's directories that load only once it is trusted, with what each
#: holds: code, or what changes an agent's plugins, permissions and prompts.
PROJECT_GATED_DIRS = {
    ".noeta/plugins": "plugins",
    ".noeta/agents": "agent files",
    ".noeta/prompts": "prompt overrides",
    ".noeta/skills": "skills",
    ".agents/skills": "skills",
}


def _untrusted_notice(cwd: str | None, sub: str) -> str | None:
    """The notice for ``<cwd>/<sub>`` left alone because the project is not
    trusted; ``None`` when it is absent or the project is trusted."""
    if not cwd:
        return None
    project = Path(cwd).expanduser().resolve()
    local = project / sub
    if not local.is_dir() or str(project) in trusted_projects():
        return None
    return (f"{local} was not loaded: this project is not trusted "
            f"(run `noeta trust` in {project} to load its {PROJECT_GATED_DIRS[sub]})")


def load_plugin_files(cwd: str | None) -> list[str]:
    """Load the plugin files of ``~/.noeta/plugins`` and, for a trusted
    project, ``<cwd>/.noeta/plugins``. Returns notices for the person: a
    project's plugin directory left alone because it is not trusted."""
    dirs = [HOME / "plugins"]
    notice = _untrusted_notice(cwd, ".noeta/plugins")
    if cwd and notice is None and (Path(cwd).expanduser() / ".noeta" / "plugins").is_dir():
        dirs.append(Path(cwd).expanduser().resolve() / ".noeta" / "plugins")
    registry.load_plugin_files(dirs)
    return [notice] if notice else []


def from_settings(settings: Settings, *, cwd: str | None = None,
                  plugins: Mapping[str, PluginSetting] | None = None,
                  permissions: Mapping[str, Any] | None = None,
                  own_tasks_only: bool = False, **runtime_kw: Any) -> LocalApp:
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
    ``serve`` serve every task."""
    from noeta.agents import default_agents
    from noeta.agents.agent_files import read_agent_dir
    from noeta.host.local import LocalApp

    notices = load_plugin_files(cwd)
    notices += [n for sub in PROJECT_GATED_DIRS if sub != ".noeta/plugins"
                if (n := _untrusted_notice(cwd, sub)) is not None]
    if (unknown := models.unknown_model_notice(settings.model)) is not None:
        notices.append(unknown)
    for notice in notices:
        log.warning("%s", notice)
    files = read_agent_dir(HOME / "agents")
    if cwd and project_trusted(cwd):
        files += read_agent_dir(Path(cwd) / ".noeta" / "agents")
    found = file_settings(cwd)
    apply_env(found)
    if not settings.model and found.model:
        settings = dataclasses.replace(settings, model=found.model)
    setting_files = [str(f) for f in found.files]
    permission_args = _permission_arguments(found)
    for key, value in (permissions or {}).items():
        if key in ("allow", "deny", "ask", "additional_directories"):
            permission_args[key] = [*permission_args.get(key, ()), *value]
        else:
            permission_args[key] = value
    if cwd and project_trusted(cwd):
        permission_args["trusted_workspace"] = True
    # The file tools may write in the additional directories too.
    write_roots = [str(Path(cwd or os.getcwd(), d))
                   for d in permission_args.get("additional_directories") or ()]
    table = merge_plugin_settings(
        {"memory": {"home": str(HOME)},
         "hooks": {"settings_files": setting_files},
         "mcp": {"settings_files": setting_files, "project": project_trusted},
         "skills": {"trusted": project_trusted, "global_dir": str(HOME / "skills")},
         "instructions": {"user_dir": str(HOME), "managed_dir": str(managed_dir())},
         "permissions": permission_args, "fs": {"write_roots": write_roots}},
        settings.plugins, plugins or {})
    add = {agent: tuple(entries) for agent, entries in settings.add_plugins.items()}
    for agent, names in registry.added_by_files().items():
        add[agent] = (*add.get(agent, ()), *((name, {}) for name in names))
    agents = default_agents(settings.model, plugins=table, add=add, extra_files=files)
    sandbox = SandboxSettings.from_settings(found.sandbox, found.sandbox_permissions,
                                            base_dir=str(cwd or os.getcwd()))
    if sandbox.enabled:
        runtime_kw.setdefault("env_for", sandboxed_env_for(sandbox))
        probe = registry.execenv("local", str(cwd or os.getcwd()), sandbox=sandbox)
        reason = getattr(getattr(probe, "sandbox", None), "unavailable", lambda: None)()
        if reason is not None:
            notice = (f"sandbox.enabled is set but the sandbox cannot start: {reason}. "
                      + ("Commands that would run in it are refused (failIfUnavailable)."
                         if sandbox.fail_if_unavailable else
                         "Commands run without it."))
            log.warning("%s", notice)
            notices.append(notice)
    runtime_kw.setdefault("prompt_dirs", [str(HOME / "prompts")])
    runtime_kw.setdefault("workspace_trusted", project_trusted)
    runtime_kw.setdefault("spill_root", str(HOME / "tool-results"))
    app = LocalApp(open_store(settings.store_url), agents,
                   settings.make_provider(), own_tasks_only=own_tasks_only,
                   task_config={"prompts": settings.prompts} if settings.prompts else None,
                   **runtime_kw)
    app.notices = notices
    return app
