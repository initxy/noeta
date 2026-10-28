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
    add_plugins = ["my_plugin", {todo = {}}]   # after the agent file's own
    [agents."*"]               # every agent
    add_plugins = []
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from noeta.core import registry
from noeta.core.agent_files import (
    PluginEntry,
    PluginSetting,
    parse_plugin_list,
)
from noeta.core.provider import Provider

if TYPE_CHECKING:
    from noeta.host.local import LocalApp

log = logging.getLogger("noeta.apps")

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
        return registry.provider(self.provider, base_url=self.base_url, api_key=self.api_key,
                                 **kw)

    @property
    def store_url(self) -> str:
        return self.store or f"sqlite:///{HOME / 'tasks.db'}"


def load_settings(**overrides: Any) -> Settings:
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
            is_reasoning=bool(row.get("efforts")), family=row.get("family"))
    if specs:
        register_models(specs)


def open_store(url: str) -> Any:
    """``sqlite:///path`` or ``postgres://…`` → a store."""
    return registry.store(url)


#: The projects whose ``.noeta/plugins`` may load: ``{"projects": [path, ...]}``,
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


def load_plugin_files(cwd: str | None) -> list[str]:
    """Load the plugin files of ``~/.noeta/plugins`` and, for a trusted
    project, ``<cwd>/.noeta/plugins``. Returns notices for the person: a
    project's plugin directory left alone because it is not trusted."""
    dirs = [HOME / "plugins"]
    notices = []
    if cwd:
        project = Path(cwd).expanduser().resolve()
        local = project / ".noeta" / "plugins"
        if local.is_dir():
            if str(project) in trusted_projects():
                dirs.append(local)
            else:
                notices.append(f"{local} was not loaded: this project is not trusted "
                               f"(run `noeta trust` in {project} to load its plugins)")
    registry.load_plugin_files(dirs)
    return notices


def from_settings(settings: Settings, *, cwd: str | None = None,
                  plugins: Mapping[str, PluginSetting] | None = None,
                  own_tasks_only: bool = False, **runtime_kw: Any) -> LocalApp:
    """The default agents on the configured store, model and provider, with
    the user's plugin files (``~/.noeta/plugins``, and a trusted project's
    ``<cwd>/.noeta/plugins``), memory (``~/.noeta/memory``), prompt overrides
    (``~/.noeta/prompts``), hooks and MCP servers (``~/.noeta/settings.json``,
    and the ``.mcp.json`` servers it consents to), agent files
    (``~/.noeta/agents``, then ``<cwd>/.noeta/agents``) and the plugin
    settings of ``config.toml``; ``plugins`` (the front end's own) goes over
    those. Front ends (``-p``, ``acp``, the TUI) pass ``own_tasks_only`` so
    their worker never picks up another process's task from the shared
    store; ``worker`` and ``serve`` serve every task."""
    from noeta.core.agent_files import read_agent_dir
    from noeta.host.local import LocalApp
    from noeta.presets import default_agents

    notices = load_plugin_files(cwd)
    for notice in notices:
        log.warning("%s", notice)
    files = read_agent_dir(HOME / "agents")
    if cwd:
        files += read_agent_dir(Path(cwd) / ".noeta" / "agents")
    table = merge_plugin_settings(
        {"memory": {"root": str(HOME / "memory")},
         "hooks": {"settings_files": [str(HOME / "settings.json")]},
         "mcp": {"settings_files": [str(HOME / "settings.json")], "project": True}},
        settings.plugins, plugins or {})
    agents = default_agents(settings.model, plugins=table, add=settings.add_plugins,
                            extra_files=files)
    runtime_kw.setdefault("prompt_dirs", [str(HOME / "prompts")])
    app = LocalApp(open_store(settings.store_url), agents,
                   settings.make_provider(), own_tasks_only=own_tasks_only,
                   task_config={"prompts": settings.prompts} if settings.prompts else None,
                   **runtime_kw)
    app.notices = notices
    return app
