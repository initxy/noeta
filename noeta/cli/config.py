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

More providers than the default one (``model`` / ``provider`` / ``base_url`` /
``api_key``), each model sent to the one that serves it
(:mod:`noeta.backends.providers.routing`)::

    [providers.deepseek]
    family = "openai_compat"
    base_url = "https://api.deepseek.com/v1"
    api_key = "{env:DEEPSEEK_API_KEY}"
    headers = {}
    models = ["deepseek-chat", {id = "deepseek-reasoner", context_window = 128000,
                                max_output_tokens = 64000}]
    list_models = true         # read its /models for more models and their windows
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
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
from noeta.cli.claude_plugins import (
    ClaudePlugin,
    find_claude_plugins,
    plugin_settings,
    read_plugin_dir,
)
from noeta.cli.plugin_policy import plugin_policy
from noeta.cli.settings_files import (
    FileSettings,
    apply_env,
    managed_dir,
    permission_arguments,
    plan_mode_arguments,
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
from noeta.core.execenv import ExecEnv
from noeta.core.loop.state import MODEL_ROUTE
from noeta.core.provider import Provider
from noeta.toolkit.local_env import withhold_env_refs
from noeta.toolkit.sandbox import SandboxSettings

if TYPE_CHECKING:
    from noeta.backends.providers.routing import Endpoint, ProviderRouter
    from noeta.core.agent import Agent
    from noeta.core.channel import Channel
    from noeta.core.events import TaskCreated
    from noeta.core.loop.state import TaskState
    from noeta.host.local import LocalApp

log = logging.getLogger("noeta.cli")


#: The built-in providers and the variable each reads a key from when none
#: is given (``None``: none).
_KEY_ENV: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY", "openai_compat": "OPENAI_API_KEY",
    "openai_responses": None,
}


class MissingAPIKey(ValueError):
    """No API key for the configured provider; the message names where to set one."""


#: The name the default provider goes by when no ``providers`` table is it.
DEFAULT_PROVIDER = "default"


def _adapter(family: str, base_url: str | None, api_key: str | None,
             headers: Mapping[str, str]) -> Provider:
    kw: dict[str, Any] = {}
    if headers:
        kw["extra_headers"] = dict(headers)
    if family == "anthropic":
        kw["device_id"] = home_dir.device_id()
    return registry.provider(family, base_url=base_url, api_key=api_key, **kw)


#: The keys of a ``[providers.<name>]`` table.
_PROVIDER_KEYS = frozenset({"family", "base_url", "api_key", "headers", "models", "list_models"})


@dataclass
class Settings:
    model: str = ""
    #: The default provider: a family (``anthropic`` | ``openai_compat`` |
    #: ``openai_responses`` | a third-party one) set up by ``base_url`` /
    #: ``api_key`` / ``extra_headers``, or the name of a ``providers`` table.
    provider: str = "anthropic"
    base_url: str | None = None
    api_key: str | None = None
    #: ``sqlite:///path`` or ``postgres://...``.
    store: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    effort: str | None = None
    #: A JSON file of extra models: ``{"models": [{"id", "context_window",
    #: "max_output_tokens", "supports_vision"?, "family"?, "provider"?}]}``.
    models_file: str | None = None
    #: More providers, by name (``[providers.<name>]``): see the module's doc.
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Model id -> the provider that serves it (a table's ``models``, a
    #: models file row's ``provider``).
    model_providers: dict[str, str] = field(default_factory=dict)
    #: Read the default provider's model listing (windows of models the
    #: catalog does not know).
    list_models: bool = True
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

    def make_provider(self) -> Provider:
        """The default provider. Raises :class:`MissingAPIKey` when no key
        is set for a built-in provider."""
        if self.provider in self.providers:
            return self._table_provider(self.provider)
        if not self.api_key and self.provider in _KEY_ENV:
            fallback = _KEY_ENV[self.provider]
            if not (fallback and os.environ.get(fallback)):
                also = f" ({fallback} is read too)" if fallback else ""
                raise MissingAPIKey(
                    f"no API key for the {self.provider} provider: set NOETA_API_KEY{also}, "
                    f"or api_key in {home_dir.HOME / 'config.toml'}")
        return _adapter(self.provider, self.base_url, self.api_key, self.extra_headers)

    def _table_secrets(self, name: str) -> tuple[str | None, dict[str, str]]:
        """A provider table's key and headers, their secret references read."""
        from noeta.agents.agent_files import resolve_secrets

        table, where = self.providers[name], f"config.toml [providers.{name}]"
        key = resolve_secrets(table.get("api_key"), where)
        return key or None, dict(resolve_secrets(table.get("headers") or {}, where))

    def _table_provider(self, name: str) -> Provider:
        table = self.providers[name]
        family = table["family"]
        key, headers = self._table_secrets(name)
        fallback = _KEY_ENV.get(family)
        if not key and family in _KEY_ENV and not (fallback and os.environ.get(fallback)):
            raise MissingAPIKey(f"no API key for provider {name!r}: set api_key in its "
                                f"[providers.{name}] table of {home_dir.HOME / 'config.toml'}")
        return _adapter(family, table.get("base_url"), key, headers)

    def make_router(self) -> ProviderRouter:
        """Every configured provider behind one ``provider_for(model)``, the
        default first; each adapter is built on first use."""
        from noeta.backends.providers import listing
        from noeta.backends.providers.routing import Endpoint, ProviderRouter

        def lister(name: str, family: str, base_url: str | None
                   ) -> Callable[[], list[listing.ListedModel]]:
            def read() -> list[listing.ListedModel]:
                if name in self.providers:
                    key, headers = self._table_secrets(name)
                else:
                    key, headers = self.api_key, dict(self.extra_headers)
                return listing.fetch_listing(family, base_url, key, headers)

            return read

        def endpoint(name: str) -> Endpoint:
            if name == DEFAULT_PROVIDER:
                family, base_url, listed = self.provider, self.base_url, self.list_models
                make: Callable[[], Provider] = self.make_provider
            else:
                table = self.providers[name]
                family, base_url = table["family"], table.get("base_url")
                listed = bool(table.get("list_models", True))
                make = functools.partial(self._table_provider, name)
            known = listing.listing_url(family, base_url) is not None
            return Endpoint(name=name, family=family, make=make, base_url=base_url,
                            listing=lister(name, family, base_url) if listed and known else None)

        default = self.provider if self.provider in self.providers else DEFAULT_PROVIDER
        names = [default, *(n for n in self.providers if n != default)]
        return ProviderRouter([endpoint(n) for n in names], explicit=self.model_providers,
                              cache_dir=home_dir.HOME / "cache" / "models")

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
    data["providers"] = _provider_tables(data.get("providers", {}))
    # The secrets this config hands to noeta's plugins, channels and
    # providers are its own.
    withhold_env_refs([data["plugins"], data["channels"], data["add_plugins"],
                       data["providers"]])
    data.pop("model_providers", None)
    known = {f for f in Settings.__dataclass_fields__}
    settings = Settings(**{k: v for k, v in data.items() if k in known})
    named: dict[str, str] = {}  # model id -> where its provider is named

    def name_provider(model: str, provider: str, where: str) -> None:
        if model in settings.model_providers and settings.model_providers[model] != provider:
            raise ValueError(f"model {model!r} is named for two providers: "
                             f"{settings.model_providers[model]!r} ({named[model]}) and "
                             f"{provider!r} ({where})")
        settings.model_providers[model] = provider
        named[model] = where

    if settings.models_file:
        for model, provider in register_models_file(settings.models_file).items():
            name_provider(model, provider, settings.models_file)
    for name, table in settings.providers.items():
        rows = [r if isinstance(r, dict) else {"id": r} for r in table.get("models", ())]
        _register_rows(rows)
        for row in rows:
            name_provider(row["id"], name, f"[providers.{name}]")
    names = {*settings.providers} | (
        set() if settings.provider in settings.providers else {DEFAULT_PROVIDER})
    stray = sorted({p for p in settings.model_providers.values() if p not in names})
    if stray:
        raise ValueError(f"{settings.models_file}: rows name providers that are not "
                         f"configured: {stray}")
    return settings


def _provider_tables(value: Any) -> dict[str, dict[str, Any]]:
    """The ``[providers.<name>]`` tables, checked."""
    tables = _plugin_tables(value, "providers")
    for name, table in tables.items():
        where = f"config.toml [providers.{name}]"
        if name == DEFAULT_PROVIDER:
            raise ValueError(f"{where}: {DEFAULT_PROVIDER!r} names the top-level provider; "
                             "pick another name")
        if unknown := set(table) - _PROVIDER_KEYS:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}; it takes "
                             f"{sorted(_PROVIDER_KEYS)}")
        if not isinstance(table.get("family"), str) or not table["family"]:
            raise ValueError(f"{where}: family is required (anthropic, openai_compat, "
                             "openai_responses or a provider package's)")
        if not isinstance(table.get("headers", {}), dict):
            raise ValueError(f"{where}: headers is a table of strings")
        rows = table.get("models", [])
        if not isinstance(rows, list) or not all(
                (isinstance(r, str) and r) or (isinstance(r, dict) and isinstance(r.get("id"), str))
                for r in rows):
            raise ValueError(f"{where}: models is a list of model ids or {{id = ..., "
                             "context_window = ..., max_output_tokens = ...}} rows")
    return tables


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


def local_env_for(sandbox: SandboxSettings | None) -> Any:
    """The runtime's ``env_for``: each task's local env on its ``cwd``, with
    the settings' sandbox (when enabled)."""
    def env_for(state: TaskState) -> ExecEnv:
        cwd = str(state.config.get("cwd") or os.getcwd())
        kw: dict[str, Any] = {"sandbox": sandbox} if sandbox is not None else {}
        return registry.execenv("local", cwd, **kw)

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


def register_models_file(path: str) -> dict[str, str]:
    """Register the models a JSON file describes (gateway models the shipped
    catalog does not know), so they run at their real limits. Returns the
    rows' ``provider``: model id -> the provider that serves it."""
    import json

    rows = json.loads(Path(path).expanduser().read_text("utf-8"))
    rows = rows.get("models", rows) if isinstance(rows, dict) else rows
    _register_rows(rows)
    return {row["id"]: row["provider"] for row in rows if isinstance(row.get("provider"), str)}


def _register_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Catalog rows for the models file rows (or ``providers`` table rows)
    that give both limits and that the catalog does not know yet."""
    from noeta.core.models import ModelSpec, find_spec, register_models

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


def load_plugin_files(cwd: str | None, plugin_dirs: Sequence[str | Path] = ()) -> list[str]:
    """Load the plugin files of ``~/.noeta/plugins``, for a trusted project
    ``<cwd>/.noeta/plugins``, then ``plugin_dirs`` (``--plugin-dir``: plugin
    files, plugin directories or directories of them, for this process only).
    Returns notices for the person: a project's plugin directory left alone
    because it is not trusted."""
    dirs = [home_dir.HOME / "plugins"]
    notice = untrusted_notice(cwd, ".noeta/plugins")
    if cwd and notice is None and (Path(cwd).expanduser() / ".noeta" / "plugins").is_dir():
        dirs.append(Path(cwd).expanduser().resolve() / ".noeta" / "plugins")
    registry.load_plugin_files(dirs, admit=plugin_policy(managed_dir()).admits,
                               given=[Path(d).expanduser().resolve() for d in plugin_dirs])
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
    # Each task's own working directory, not the server's: a task may run in
    # an --allow-root directory.
    permission_args.setdefault("trusted_workspace", project_trusted)
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
         "skills": _skills_settings(found, write_roots),
         "instructions": {"user_dir": str(home_dir.HOME), "managed_dir": str(managed_dir())},
         "permissions": permission_args, "plan_mode": plan_mode_arguments(found),
         "fs": {"write_roots": write_roots},
         "environment": environment_arguments(found)})


def environment_arguments(found: FileSettings) -> dict[str, Any]:
    """The environment plugin's arguments the settings files give:
    ``language`` and the ``attribution`` lines."""
    out: dict[str, Any] = {}
    if found.language:
        out["language"] = found.language
    for key in ("commit", "pr"):
        if key in found.attribution:
            out[f"attribution_{key}"] = found.attribution[key]
    return out


#: The skill listing's budget in characters for every task (Claude Code's
#: ``SLASH_COMMAND_TOOL_CHAR_BUDGET``, under noeta's name).
SKILL_BUDGET_ENV = "NOETA_SLASH_COMMAND_TOOL_CHAR_BUDGET"


def _skills_settings(found: FileSettings, additional: Sequence[str]) -> dict[str, Any]:
    """The skills plugin's arguments: noeta's places (``~/.noeta/skills``
    and ``commands``, the managed directory's), the additional directories,
    and the listing settings."""
    out: dict[str, Any] = {"trusted": project_trusted,
                           "global_dir": str(home_dir.HOME / "skills"),
                           "global_commands_dir": str(home_dir.HOME / "commands"),
                           "managed_dir": str(managed_dir()),
                           "additional_dirs": list(additional)}
    fraction = found.skill_listing.get("skillListingBudgetFraction")
    if fraction is not None:
        out["budget_fraction"] = float(fraction)
    max_chars = found.skill_listing.get("skillListingMaxDescChars")
    if max_chars is not None:
        out["max_desc_chars"] = int(max_chars)
    raw = os.environ.get(SKILL_BUDGET_ENV, "").strip()
    if raw.isdigit() and int(raw) > 0:
        out["budget_chars"] = int(raw)
    return out


def _apply_sandbox(sandbox: SandboxSettings, cwd: str | None,
                   runtime_kw: dict[str, Any]) -> list[str]:
    """Run tasks' commands in the settings' ``sandbox`` (``runtime_kw``'s
    ``env_for``) when it is enabled; a notice when the sandbox cannot start
    here."""
    runtime_kw.setdefault("env_for", local_env_for(sandbox if sandbox.enabled else None))
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
                  plugin_dirs: Sequence[str | Path] = (),
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
    and win a name. ``plugin_dirs`` (``--plugin-dir``) are plugin places for
    this process only, read after the others."""
    from noeta.agents import default_agents
    from noeta.agents.agent_files import read_agent_dir
    from noeta.core.client import Client
    from noeta.host.local import LocalApp

    home_dir.ensure_home()
    notices = home_dir.loose_permissions(settings.store_url)
    notices += load_plugin_files(cwd, plugin_dirs)
    notices += [n for sub in PROJECT_GATED_DIRS if sub != ".noeta/plugins"
                if (n := untrusted_notice(cwd, sub)) is not None]
    router = settings.make_router()
    # Only a start model the catalog does not know needs a listing now (its
    # limits, and whether to say so); its route is settled when a task is
    # given it.
    if settings.model and models.find_spec(settings.model) is None:
        router.prepare(settings.model)
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
    sandbox = SandboxSettings.from_settings(found.sandbox, found.sandbox_permissions,
                                            base_dir=str(cwd or os.getcwd()))
    base = _base_plugin_settings(found, cwd, permissions, project=source("project"))
    fs = base.get("fs")
    if own_tasks_only and isinstance(fs, Mapping):
        # A front end runs on the person's machine: an @-mention of a path
        # under ~ is their home (never on a server, whose home is not theirs).
        base["fs"] = {**fs, "home": str(Path.home())}
    store = open_store(settings.store_url)
    # Claude Code ranks the skill listing by use across its sessions; here
    # the store's tasks are the sessions (scanned in the background).
    skills = base.get("skills")
    if isinstance(skills, Mapping):
        base["skills"] = {**skills, "usage_from": Client(store)}
    made: dict[Any, Any] = {}
    model, add_plugins = settings.model, settings.add_plugins
    plugin_agent: dict[str, str] = {}

    def make_agents() -> list[Agent]:
        """The agents from the agent files and plugins found now; a plugin
        whose code and arguments did not change is the one made before."""
        claude, found_notices = claude_plugins(cwd, plugin_dirs)
        for notice in found_notices:
            if notice not in notices:
                notices.append(notice)
                log.warning("%s", notice)  # a worker has no front end to show it
        plugin_agent.clear()
        plugin_agent.update(plugins_agent(claude))
        # Named <plugin>:<agent>, a plugin's agent never takes a built-in one's name.
        files = [a for p in claude for a in p.agents]
        own: list[AgentFile] = []
        if source("user"):
            own += read_agent_dir(home_dir.HOME / "agents")
        if cwd and project_trusted(cwd) and source("project"):
            own += read_agent_dir(Path(cwd) / ".noeta" / "agents")
        own += agent_files
        # Secrets noeta's own agent files hand to plugins are noeta's too.
        withhold_env_refs([f.plugins for f in own])
        files += own
        from_plugins, plugin_notices = plugin_settings(claude)
        notices.extend(n for n in plugin_notices if n not in notices)
        table = merge_plugin_settings(base, from_plugins, settings.plugins, plugins or {})
        policy = plugin_policy(managed_dir())
        table.update({name: False for name in registry.available(registry.PLUGINS)
                      if not policy.admits(name, shipped=_shipped(name))})
        add = {agent: tuple(entries) for agent, entries in add_plugins.items()}
        for agent, names in registry.added_by_files().items():
            add[agent] = (*add.get(agent, ()), *((name, {}) for name in names))
        # Every agent offers the typed commands (``/init``, ``/review`` and
        # the Claude Code plugins').
        add["*"] = (*add.get("*", ()), ("markdown_commands", {}))
        agents = default_agents(model, plugins=table, add=add, extra_files=files, made=made)
        unjoined[:] = _unjoined_notices(agents, table)
        notices.extend(n for n in unjoined if n not in notices)
        return agents

    unjoined: list[str] = []
    agents = make_agents()
    for notice in unjoined:
        log.warning("%s", notice)
    notices += _apply_sandbox(sandbox, cwd, runtime_kw)
    runtime_kw.setdefault("prompt_dirs", [str(home_dir.HOME / "prompts")])
    runtime_kw.setdefault("workspace_trusted", project_trusted)
    # The default provider is built now, so a missing key stops the start.
    router.adopt(router.default.name, settings.make_provider())
    if router.several:
        # Routes are recorded and followed only with several providers: with
        # one, every route is that one and the log stays as it always was.
        runtime_kw.setdefault("task_provider", router.task_provider)
    app = LocalApp(store, agents,
                   router.provider_for, own_tasks_only=own_tasks_only,
                   task_config={"prompts": settings.prompts} if settings.prompts else None,
                   channels=make_channels(settings.channels) if with_channels else (),
                   **runtime_kw)
    app.notices = notices

    def prepare_model(model: str) -> str | None:
        router.prepare(model)
        return models.unknown_model_notice(model)

    app.list_models = router.available
    app.prepare_model = prepare_model
    if router.several:
        app.client.route_model = router.route_record
        app.runtime.prepare_child = _route_child(app.runtime, router)

    def pick_main_agent() -> None:
        """The agent front ends start with: the settings' ``agent``, else
        the one a Claude Code plugin's settings name, else ``main``."""
        wanted = found.agent or plugin_agent.get("agent")
        # A plugin's agent by its bare name, as settings written before
        # plugin agents were named <plugin>:<agent> give it.
        named = app.runtime.agent(wanted) if wanted else None
        app.main_agent = named.name if named is not None else "main"
        if wanted and named is None:
            notice = (f"agent {wanted!r} (from the settings of "
                      f"{plugin_agent.get('source') if not found.agent else 'noeta'}) "
                      "is not an agent here; main is used")
            if notice not in notices:
                notices.append(notice)
                log.warning("%s", notice)

    pick_main_agent()

    def reload_plugins() -> list[str]:
        """What changed (one line per plugin), then the files that failed
        and the notices of loading them."""
        from noeta.cli.main import broken_plugin_lines

        before = dict(app.runtime.agents)
        registry.forget_plugin_files()
        found = load_plugin_files(cwd, plugin_dirs)
        app.replace_agents(make_agents())
        pick_main_agent()
        changes = plugin_changes(before, app.runtime.agents) or ["no plugin changed"]
        return [*changes, *(f"failed: {line}" for line in broken_plugin_lines()),
                *found, *unjoined]

    app.reload_plugins = reload_plugins
    return app


def _route_child(runtime: Any, router: ProviderRouter) -> Any:
    """``Runtime.prepare_child``: a child task records the route of the
    model it runs (its config's, else its agent's), like a task a front end
    starts, unless it carries that model's route already."""
    import asyncio

    import msgspec

    async def prepare(created: TaskCreated) -> TaskCreated:
        agent = runtime.agent(created.agent)
        model = models.resolve_alias(str(created.config.get("model")
                                         or (agent.model if agent is not None else "")))
        route = created.config.get(MODEL_ROUTE)
        if not model or (isinstance(route, Mapping) and route.get("model") == model):
            return created
        try:
            record = await asyncio.to_thread(router.route_record, model)
        except Exception:
            log.warning("recording the route of %r for a child task failed; it resolves "
                        "on the worker that runs it", model, exc_info=True)
            return created
        if record is None:
            return created
        return msgspec.structs.replace(created, config={**created.config, MODEL_ROUTE: record})

    return prepare


def _unjoined_notices(agents: Sequence[Agent],
                      table: Mapping[str, PluginSetting]) -> list[str]:
    """One notice per plugin file whose plugin no agent runs (no ``add_to``,
    not in any ``add_plugins`` or agent file), unless it is switched off."""
    joined = {e.name for a in agents for e in a.extensions}
    out = []
    for name, path in registry.plugin_files().items():
        setting = table.get(name)
        if name in joined or setting is False or (
                isinstance(setting, Mapping) and setting.get("enabled") is False):
            continue
        out.append(f"plugin file {path}: {name!r} joins no agent; set add_to = (\"main\",) "
                   f"on its class, or [agents.main] add_plugins = [\"{name}\"] in config.toml")
    return out


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


def claude_plugins(cwd: str | None, plugin_dirs: Sequence[str | Path] = ()
                   ) -> tuple[list[ClaudePlugin], list[str]]:
    """The Claude Code plugin directories of ``~/.noeta/plugins``, for a
    trusted project ``<cwd>/.noeta/plugins``, and ``plugin_dirs`` (each one
    such directory or a directory of them; :mod:`noeta.cli.claude_plugins`),
    with notices for the person."""
    places = [(home_dir.HOME / "plugins", False)]
    if cwd and project_trusted(cwd):
        places.append((Path(cwd).expanduser().resolve() / ".noeta" / "plugins", True))
    data_home = home_dir.HOME / "plugin-data"
    found = find_claude_plugins(places, data_home)
    for given in (Path(d).expanduser().resolve() for d in plugin_dirs):
        one = read_plugin_dir(given, data_home)
        found += [one] if one is not None else [] if (given / "__init__.py").is_file() \
            else find_claude_plugins([(given, False)], data_home)
    policy = plugin_policy(managed_dir())
    found = [p for p in found if policy.admits(p.name)]
    return found, [n for p in found for n in p.notices]


def plugins_agent(plugins: Sequence[ClaudePlugin]) -> dict[str, str]:
    """The ``agent`` Claude Code plugins' settings name (a later plugin
    wins, as in Claude Code) and which plugin names it (``source``)."""
    out: dict[str, str] = {}
    for p in plugins:
        agent = p.settings.get("agent")
        if not isinstance(agent, str):
            continue
        if out:
            log.info("plugin %s overrides setting 'agent' (set by %s)", p.name, out["source"])
        out = {"agent": agent, "source": f"plugin {p.name}"}
    return out


def settings_rows(settings: Settings, cwd: str) -> list[tuple[str, str]]:
    """``(name, value)`` rows of what a front end started in ``cwd`` runs
    with: provider, store, the settings files and their permission rules."""
    found = file_settings(cwd)
    rows = [("provider", settings.provider), ("base URL", settings.base_url or "(default)"),
            ("store", settings.store_url), ("effort", settings.effort or "(default)"),
            ("settings files", ", ".join(str(f) for f in found.files) or "(none)")]
    for key in ("allow", "deny", "ask"):
        if found.permissions.get(key):
            rows.append((f"permissions.{key}", ", ".join(found.permissions[key])))
    return rows


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
