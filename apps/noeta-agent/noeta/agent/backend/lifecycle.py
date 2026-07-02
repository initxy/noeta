"""lifecycle — new backend process boot / serve / drain.

Assembles the host-level (D5
"I. process-level") configuration — host / port / workspace / provider / model — into a
running :class:`~noeta.agent.backend.app` server over an
:class:`~noeta.agent.backend.engine_room.EngineRoom`, and returns a clean
shutdown handle.

This is the assembly layer: unlike :mod:`noeta.agent.backend.engine_room` (which
imports only ``noeta.sdk``), lifecycle may wire concrete host material — the
offline :class:`~noeta.agent.observe._stub_provider.CodeStubProvider` default,
env-driven config — transitionally, until the host-config story (durable
storage, real providers) is consolidated.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from noeta.sdk import HostConfig

from noeta.agent.backend.app import Router, make_http_server
from noeta.agent.backend.engine_room import EngineRoom
from noeta.agent.backend.mcp_service import register_mcp_routes
from noeta.agent.backend.read_views import register_read_view_routes
from noeta.agent.backend.resource_services import register_resource_routes
from noeta.agent.backend.static_assets import locate_web_assets
from noeta.agent.backend.task_protocol import register_task_routes
from noeta.agent.backend.workspace_service import register_workspace_routes


_log = logging.getLogger(__name__)

#: Default MCP connector config store (mirrors the legacy runner's
#: ``~/.noeta/mcp_servers.json``); operators override via ``NOETA_AGENT_MCP_FILE``.
_DEFAULT_MCP_FILE: Path = Path("~/.noeta/mcp_servers.json").expanduser()


@dataclass(frozen=True)
class BackendConfig:
    """Host-level (process) config for the new backend.

    Mirrors the env knobs (and the ``NOETA_AGENT_CONFIG`` JSON file) the legacy
    runner reads so ``python -m noeta.agent`` can target either backend with the
    same environment / config file. The provider fields
    (``provider_id`` / ``api_key`` / ``base_url`` / ``api_version`` /
    ``default_headers`` / ``max_tokens``) select a real LLM; the default
    ``"stub"`` keeps a bare boot offline + credential-free.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    workspace_dir: Path = Path.cwd()
    model: Optional[str] = None
    #: The
    #: selectable model list (the composer's model dropdown), all served by the
    #: single configured provider. Doubles as the per-turn model-selector
    #: allowlist (⊤ local principal: config = deployment permission). Empty ⇒ the
    #: single ``model`` only (no per-turn switching). Env ``NOETA_AGENT_MODELS`` is
    #: comma-separated; the config file key ``models`` is a JSON list.
    models: tuple[str, ...] = ()
    #: The workspace
    #: (project) registry JSON store; the default workspace is ``workspace_dir``.
    workspaces_path: Path = field(
        default_factory=lambda: Path("~/.noeta/workspaces.json").expanduser()
    )
    mcp_servers_path: Path = _DEFAULT_MCP_FILE
    #: A sqlite file path enabling durable
    #: storage (the ``HostConfig`` triple) so conversations + the session list
    #: survive restarts. ``None`` ⇒ the SDK's in-memory default (single-run,
    #: which is enough for the single-port preview / MCP — no cross-process
    #: need; history retention is the only thing that needs durability).
    sqlite_path: Optional[str] = None
    #: Provider wiring (mirrors the legacy ``RunnerConfig`` single-provider
    #: path). ``"stub"`` ⇒ the offline two-turn provider; ``openai`` /
    #: ``openai-responses`` / ``anthropic`` reach a real ``noeta.sdk.providers``
    #: adapter (needs ``api_key`` + network). See :func:`build_provider`.
    provider_id: str = "stub"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    api_version: Optional[str] = None
    #: Output-token cap forwarded to a request carrying none (only the
    #: openai-responses adapter consumes it — keeps the gateway's small default
    #: from truncating long turns).
    max_tokens: Optional[int] = None
    #: Extra HTTP headers merged into the provider client (gateway headers like
    #: ``X-TT-LOGID``); config-file only (dict shape).
    default_headers: Mapping[str, str] = field(default_factory=dict)
    #: Process fs write policy: ``"dry_run"`` stages a diff (safe default),
    #: ``"apply"`` performs real writes. Threaded to the SDK via HostConfig.
    write_mode: str = "dry_run"
    #: Host kill-switch for the ``run_workflow`` control tool (off by default).
    workflow_enabled: bool = False
    #: T5 async contract: the turn-driving command endpoints (start /
    #: send_goal / approve / deny / answer) seed synchronously (typed 4xx
    #: unchanged) and drive the turn on a background thread, acking 202
    #: immediately — progress rides the SSE stream. On by default for the
    #: served product; ``NOETA_AGENT_BACKGROUND_DRIVE=0`` (or the config
    #: file key) restores the fully synchronous commands.
    background_drive: bool = True

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "BackendConfig":
        """Build a config from env + an optional ``NOETA_AGENT_CONFIG`` JSON file.

        Precedence (low → high): dataclass defaults < ``NOETA_AGENT_CONFIG`` file
        < ``NOETA_AGENT_*`` env vars — identical to the legacy runner so the same
        ``noeta.config.json`` works against either backend.
        """
        e = dict(os.environ if env is None else env)
        # 1) config file (if any) — the lower-precedence layer.
        file_vals: dict[str, Any] = {}
        config_path = e.get("NOETA_AGENT_CONFIG")
        if config_path:
            loaded = json.loads(Path(config_path).read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(
                    f"NOETA_AGENT_CONFIG {config_path!r} must hold a JSON object"
                )
            file_vals = loaded

        def pick(env_suffix: str, file_key: str, default: Any = None) -> Any:
            raw = e.get(f"NOETA_AGENT_{env_suffix}")
            if raw is not None:
                return raw
            if file_vals.get(file_key) is not None:
                return file_vals[file_key]
            return default

        workspace = pick("WORKSPACE", "workspace_dir")
        workspaces_file = pick("WORKSPACES_FILE", "workspaces_registry_path")
        mcp_file = pick("MCP_FILE", "mcp_servers_registry_path")
        sqlite = pick("SQLITE", "sqlite_path")
        models = _normalize_models(pick("MODELS", "models"))
        headers = file_vals.get("default_headers") or {}
        max_tokens = pick("MAX_TOKENS", "max_tokens")
        workflow_raw = pick("WORKFLOW_ENABLED", "workflow_enabled", False)
        workflow_enabled = (
            workflow_raw
            if isinstance(workflow_raw, bool)
            else str(workflow_raw).strip().lower() in ("1", "true", "yes", "on")
        )
        background_raw = pick("BACKGROUND_DRIVE", "background_drive", True)
        background_drive = (
            background_raw
            if isinstance(background_raw, bool)
            else str(background_raw).strip().lower() in ("1", "true", "yes", "on")
        )
        return cls(
            host=pick("HOST", "host", "127.0.0.1"),
            port=int(pick("PORT", "port", 8765)),
            workspace_dir=Path(workspace) if workspace else Path.cwd(),
            model=pick("MODEL", "model") or None,
            models=models,
            workspaces_path=Path(workspaces_file).expanduser()
            if workspaces_file
            else Path("~/.noeta/workspaces.json").expanduser(),
            mcp_servers_path=Path(mcp_file).expanduser()
            if mcp_file
            else _DEFAULT_MCP_FILE,
            sqlite_path=(str(Path(sqlite).expanduser()) if sqlite else None),
            provider_id=pick("PROVIDER", "provider_id", "stub"),
            api_key=pick("API_KEY", "api_key"),
            base_url=pick("BASE_URL", "base_url"),
            api_version=pick("API_VERSION", "api_version"),
            max_tokens=int(max_tokens) if max_tokens is not None else None,
            default_headers=dict(headers),
            write_mode=pick("WRITE_MODE", "write_mode", "dry_run"),
            workflow_enabled=workflow_enabled,
            background_drive=background_drive,
        )


def _normalize_models(raw: Any) -> tuple[str, ...]:
    """Normalize the configured model list → a tuple of names.

    Accepts a JSON list (config file ``models``) or a comma-separated string
    (env ``NOETA_AGENT_MODELS``); ``None`` / empty ⇒ ``()`` (single-model path).
    """
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(m).strip() for m in raw if str(m).strip())
    return tuple(m.strip() for m in str(raw).split(",") if m.strip())


class _LateImageResolver:
    """A ``ContentRef → bytes`` resolver bound after the engine room exists.

    The responses adapter needs its ``image_resolver`` at construction time, but
    the content store lives inside the noeta.sdk host that is built *after* the
    provider (the provider is an argument to ``EngineRoom.official``). This
    holder is handed to the provider up front and :meth:`bind` once the engine
    room — hence the content store — exists. Calling it before binding, or on a
    ref with no stored bytes, raises rather than silently dropping an image
    (mirrors the adapter's own "refusing to silently drop the image" stance).
    """

    def __init__(self) -> None:
        self._get: Optional[Callable[[str], Optional[bytes]]] = None

    def bind(self, get_content: Callable[[str], Optional[bytes]]) -> None:
        self._get = get_content

    def __call__(self, ref: Any) -> bytes:
        if self._get is None:
            raise RuntimeError(
                "image_resolver used before the engine room was bound"
            )
        body = self._get(ref.hash)
        if body is None:
            raise LookupError(f"image content not found for ref {ref.hash!r}")
        return body


def build_provider(
    config: BackendConfig, *, image_resolver: Optional[Any] = None
) -> Optional[Any]:
    """Construct the real LLM provider for ``config``, or ``None`` for the stub.

    ``provider_id == "stub"`` ⇒ ``None`` (``serve_backend`` then builds the
    offline :class:`CodeStubProvider`). The real adapters come **through
    noeta.sdk** (``noeta.sdk.providers``) so the backend never imports
    ``noeta.providers`` directly — the encapsulation weld
    (D2). ``openai-responses`` wants the
    COMPLETE responses endpoint as ``base_url`` (it POSTs there verbatim,
    only adding ``?api-version``).

    ``image_resolver`` (a ``ContentRef → bytes`` callable) is injected only into
    the vision-capable ``openai-responses`` adapter so it can deref an
    ``ImageBlock`` and base64-inline it at request time; the non-vision adapters
    reject ``ImageBlock`` by design and take no resolver.
    """
    adapter = (config.provider_id or "stub").strip().lower()
    if adapter == "stub":
        return None
    if not config.api_key:
        raise SystemExit(f"provider {adapter!r} needs an api_key (config/env)")
    headers = dict(config.default_headers) or None
    if adapter == "openai":
        from noeta.sdk.providers import OpenAICompatProvider

        if not config.base_url:
            raise SystemExit("provider 'openai' needs a base_url")
        return OpenAICompatProvider(
            base_url=config.base_url, api_key=config.api_key, extra_headers=headers
        )
    if adapter == "openai-responses":
        from noeta.sdk.providers import OpenAIResponsesProvider

        if not config.base_url:
            raise SystemExit(
                "provider 'openai-responses' needs a base_url "
                "(the COMPLETE responses endpoint)"
            )
        return OpenAIResponsesProvider(
            base_url=config.base_url,
            api_key=config.api_key,
            api_version=config.api_version,
            default_max_tokens=config.max_tokens,
            extra_headers=headers,
            image_resolver=image_resolver,
        )
    if adapter == "anthropic":
        from noeta.sdk.providers import AnthropicProvider

        kwargs: dict[str, Any] = {"api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if headers:
            kwargs["extra_headers"] = headers
        if config.max_tokens is not None:
            kwargs["default_max_tokens"] = config.max_tokens
        kwargs["image_resolver"] = image_resolver
        return AnthropicProvider(**kwargs)
    raise SystemExit(
        f"unknown provider {adapter!r} "
        "(expected 'stub' / 'openai' / 'openai-responses' / 'anthropic')"
    )


def serve_backend(
    config: BackendConfig,
    *,
    provider: Optional[Any] = None,
    engine_room: Optional[EngineRoom] = None,
    app_gateway: Optional[Any] = None,
    mcp_registry: Optional[Any] = None,
    workspace_registry: Optional[Any] = None,
    mcp_http_post: Optional[Any] = None,
    web_assets: Optional[Any] = None,
) -> tuple[Any, str, Callable[[], None]]:
    """Boot the new backend; return ``(server, url, shutdown)``.

    * Loads the official preset registry into an :class:`EngineRoom` (unless one
      is injected — tests pass a pre-built room).
    * Builds the T6 ancillary services — the HTML-app preview gateway (``open_app``)
      and the MCP connector store — and threads them through ``noeta.sdk``'s
      :class:`HostConfig` so the engine gains the matching tools, while the same
      instances back the ``/preview`` + ``/mcp`` routes. Both default to absent
      (``None``) when an ``engine_room`` is injected without them, keeping the
      core protocol usable on its own.
    * Defaults to the offline :class:`CodeStubProvider` so a bare boot works
      with no credentials, matching the legacy runner's offline default.
    * Serves on a daemon thread; ``shutdown`` stops the server and drains the
      engine room.
    """
    def storage_close() -> None:
        """Close any durable storage opened below (rebound when sqlite is on)."""

    if engine_room is None:
        # Vision adapters need a ``ContentRef → bytes`` resolver at construction,
        # but the content store only exists once the engine room is built below;
        # hand the provider this late-bound holder now, bind it after.
        image_resolver = _LateImageResolver()
        if provider is None:
            # A configured provider_id (openai / openai-responses / anthropic)
            # builds a real adapter through noeta.sdk; the offline stub default
            # keeps a bare boot credential-free.
            provider = build_provider(config, image_resolver=image_resolver)
        if provider is None:
            # Imported lazily so engine_room/app stay free of this app-private
            # provider; the offline default keeps a bare boot credential-free.
            from noeta.agent.observe._stub_provider import CodeStubProvider

            provider = CodeStubProvider()
        # Build the ancillary services (product material reused from the legacy host)
        # and bind them into the engine via the noeta.sdk host-config so the
        # agent gets open_app + live MCP; the same instances back the routes.
        if app_gateway is None:
            from noeta.agent.host.preview_gateway import PreviewGateway

            app_gateway = PreviewGateway()
        if mcp_registry is None:
            from noeta.agent.host.mcp_registry import McpServerRegistry

            mcp_registry = McpServerRegistry(
                config.mcp_servers_path, http_post=mcp_http_post
            )
            mcp_registry.load()
        # Workspace (project) registry — the default workspace is the host-fixed
        # ``workspace_dir``; user-added projects persist to the JSON store.
        if workspace_registry is None:
            from noeta.agent.host.workspace_registry import WorkspaceRegistry

            workspace_registry = WorkspaceRegistry(
                config.workspaces_path, default_dir=config.workspace_dir
            )
            workspace_registry.load()
        # Durable storage (D3): a configured sqlite path supplies the HostConfig
        # triple so conversations + the session list survive restarts; otherwise
        # the SDK builds its in-memory default (single-run).
        event_log = content_store = dispatcher = None
        if config.sqlite_path:
            from noeta.agent.host.storage import open_sqlite_storage

            (event_log, content_store, dispatcher), storage_close = (
                open_sqlite_storage(config.sqlite_path)
            )
        host_config = HostConfig(
            app_gateway=app_gateway,
            mcp_server_resolver=mcp_registry.resolve_spec,
            mcp_http_post=mcp_http_post,
            event_log=event_log,
            content_store=content_store,
            dispatcher=dispatcher,
            write_mode=config.write_mode,
            workflow_allowed=config.workflow_enabled,
        )
        engine_room = EngineRoom.official(
            provider=provider,
            workspace_dir=config.workspace_dir,
            model=config.model,
            host_config=host_config,
            models=config.models,
            background_drive=config.background_drive,
        )
        # Now the content store exists (inside the noeta.sdk host): a vision
        # provider can deref ``ImageBlock(ContentRef)`` bytes at request time.
        image_resolver.bind(engine_room.get_content)

    router = Router()
    register_task_routes(router)  # T5: SSE stream + command endpoints
    register_resource_routes(router)  # T6 core: content / files / file
    register_mcp_routes(router)  # T6: MCP connector management
    register_workspace_routes(router)  # workspace (project) management
    register_read_view_routes(router)  # capabilities + session-list index
    # Preview (/preview/<token>/...) + the SPA (/chat, /trace, /assets) are
    # prefix-routed in the handler dispatch.
    server = make_http_server(
        engine_room,
        host=config.host,
        port=config.port,
        router=router,
        app_gateway=app_gateway,
        mcp_registry=mcp_registry,
        workspace_registry=workspace_registry,
        web_assets=web_assets if web_assets is not None else locate_web_assets(),
    )
    bound_host, bound_port = server.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"

    thread = threading.Thread(
        target=server.serve_forever, name="noeta-agent-backend", daemon=True
    )
    thread.start()
    _log.info("noeta-agent new backend serving at %s", url)

    def shutdown() -> None:
        try:
            server.shutdown()
            server.server_close()
        finally:
            try:
                engine_room.shutdown()
            finally:
                # Close the durable storage we opened (the SDK never closes an
                # injected store); a no-op for the in-memory default.
                storage_close()

    return server, url, shutdown
