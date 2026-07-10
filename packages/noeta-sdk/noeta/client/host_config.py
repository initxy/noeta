"""``HostConfig`` — the SDK's host-level (process) wiring surface (D3).

This splits the SDK's extension face in two:

* **Options** carries *agent identity + per-agent extension points* (Tool /
  Provider / Policy / Guard / Observer / Content Channel) — see
  :class:`~noeta.client.options.Options`.
* **HostConfig** carries *host-level wiring* that is NOT part of any agent
  identity and is decided once per process: the durable **storage** backend
  (EventLog / ContentStore / Dispatcher) and the host **runtime injections**
  (the HTML-app preview gateway, the live-MCP alias resolver). ``compile_options``
  never sees any of this, so two clients differing only in their HostConfig
  produce byte-identical AgentSpec identities.

Every field defaults to "absent", so ``HostConfig()`` reproduces today's
behaviour exactly: in-memory storage, no ``open_app`` tool, no live MCP. A
product backend (``noeta.agent.backend``) passes a populated HostConfig to opt
into durable storage / preview / MCP while still driving the engine only through
``noeta.sdk``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Optional, Tuple

from noeta.client.sandbox_provider import SandboxProvider, SandboxSpec
from noeta.observers.otlp import OtlpHttpPost, OtlpTraceConfig

if TYPE_CHECKING:
    # Only for annotations (``from __future__ import annotations`` keeps these
    # out of the runtime import graph — ``noeta.client.sandbox`` imports this
    # module, so a runtime import here would be circular).
    from noeta.client.sandbox import BackendFactory, BrowserBackendFactory
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.messages import StreamDelta
from noeta.protocols.step_context import StepContext
from noeta.tools.app import AppPreviewGateway
from noeta.tools.mcp import HttpPostFn, McpAnyServerSpec


__all__ = ["HostConfig", "SandboxExecEnvConfig"]


@dataclass(frozen=True)
class SandboxExecEnvConfig:
    """Config for routing the fs / shell tools to an AIO Sandbox container.

    A pure, serialisable config value — it carries only *addressing*, never a
    live client or a secret. The product host turns it into a live
    ``AioSandboxExecEnv`` (reading the key from the environment, provisioning /
    attaching a container) and threads that into ``build_session_inputs``; the
    config alone is import-linter-safe for the backend to build (D2: the
    backend fills config, the runtime instantiates the adapter).

    * ``base_url`` — the container's API root (e.g. ``http://host:8080``).
    * ``api_key_env`` — the environment variable holding the container's static
      ``SANDBOX_API_KEY``. The key rides only on the wire, never in a log /
      event / this config (D5). ``None`` env value ⇒ no auth header.
    * ``provision`` — ``"eager"`` provisions a fresh container when a root task
      starts; ``"attach"`` connects to an already-running ``base_url`` (the
      default — the reconnect path a resumed / reclaimed task also takes).
    * ``workdir`` — the container's working directory. In sandbox mode this
      *is* the fs-tools' workspace root (a lexical containment fence, D7): the
      host path a local session would use is meaningless inside the container,
      so the host substitutes this container path. Must be absolute.
    """

    base_url: str
    api_key_env: str = "SANDBOX_API_KEY"
    provision: str = "attach"
    workdir: str = "/workspace"

    def resolve_api_key(self) -> Optional[str]:
        """Read the container key from ``api_key_env`` (``None`` if unset).

        Kept here so the addressing (this config) and the secret (the env
        lookup) stay separated: the config is safe to record / pass around; the
        key is fetched only at connect time.
        """
        return os.environ.get(self.api_key_env)


@dataclass(frozen=True)
class HostConfig:
    """Host-level wiring for a :class:`~noeta.client.client.Client` (D3).

    Storage triple
    --------------
    ``event_log`` / ``content_store`` / ``dispatcher`` inject an external,
    typically durable (sqlite) storage backend. Supply **all three or none**;
    omitting them (the default) makes the Client build its own in-memory triple,
    byte-identical to the historical single-session behaviour. The three are
    constructed together by the caller so the event log already holds the
    dispatcher as its ``lease_validator``.

    Runtime injections
    ------------------
    ``app_gateway`` is the live HTML-app preview gateway the ``open_app`` tool
    mounts against; ``None`` ⇒ no ``open_app`` tool (the prompt's tool list is
    unchanged). ``mcp_server_resolver`` resolves an enabled MCP alias to its full
    connectable spec each turn; ``None`` ⇒ no live MCP is connected.
    ``mcp_http_post`` is an injectable HTTP transport for the remote-MCP client
    (tests pass a fake; production leaves it ``None`` to use stdlib urllib).
    These are runtime objects, never part of the agent identity.

    ``workflow_allowed`` is the host kill-switch for the ``run_workflow`` control
    tool (off by default, matching the runtime default). ``write_mode`` is the
    process-level fs write policy (``"dry_run"`` stages a proposed diff without
    touching disk — the safe default; ``"apply"`` performs real writes); the
    Client maps it to the edit tools' ``FsWriteMode``.
    """

    # -- durable storage (all-or-none) -------------------------------------
    event_log: Optional[EventLogFull] = None
    content_store: Optional[ContentStore] = None
    dispatcher: Optional[Dispatcher] = None

    # -- host runtime injections -------------------------------------------
    app_gateway: Optional[AppPreviewGateway] = None
    mcp_server_resolver: Optional[
        Callable[[str], Optional[McpAnyServerSpec]]
    ] = None
    mcp_http_post: Optional[HttpPostFn] = None
    #: Token-streaming sink: ``(ctx, call_id, delta)`` receives ephemeral
    #: ``StreamDelta``s while a streaming-capable provider call is in flight
    #: (the product backend wires its delta hub here). ``None`` (default) ⇒
    #: providers are called exactly as today; deltas are never persisted.
    delta_sink: Optional[
        Callable[[StepContext, str, StreamDelta], None]
    ] = None
    #: OTLP trace export: when set, the Client wires a
    #: :class:`noeta.observers.trace_export.TraceExportObserver` with an
    #: OTLP/HTTP JSON sink at the configured endpoint and stops it on
    #: ``shutdown``. ``None`` (default) ⇒ no trace export. A host runtime
    #: injection like the preview gateway — never part of agent identity.
    otlp_traces: Optional[OtlpTraceConfig] = None
    #: Injectable HTTP transport for the OTLP exporter (tests pass a fake;
    #: production leaves it ``None`` to use httpx) — the ``mcp_http_post``
    #: pattern.
    otlp_http_post: Optional[OtlpHttpPost] = None
    #: Per-request provider header factory: ``(ctx) -> {header: value}`` called
    #: once per LLM round-trip, merged over the provider client's static
    #: headers. The product wires a stable per-task ``session_id`` here so a
    #: gateway that pins prompt-cache to a single backend account (ModelHub's
    #: ``extra.session_id`` account-stickiness) keeps a long task on one
    #: account and actually reuses its KV cache. ``None`` (default) ⇒ no
    #: per-request headers — a host runtime injection, never agent identity.
    provider_headers: Optional[Callable[[StepContext], Mapping[str, str]]] = None

    #: Sandbox execution backend for the fs / shell tools. ``None`` (default) ⇒
    #: the local host (``LocalExecEnv``, today's behaviour). When set, the
    #: product host provisions / attaches an AIO Sandbox container per root task
    #: and routes fs / shell side effects into it (the tool schemas — and thus
    #: the stable prefix — are unchanged). A host runtime injection, never part
    #: of any agent identity.
    #:
    #: **v1 attach path.** ``exec_env`` names ONE pre-existing container by
    #: ``base_url``; every session on the host attaches it (byte-identical to the
    #: shipped v1 behaviour). The Client wraps it into an attach ``SandboxProvider``.
    exec_env: Optional[SandboxExecEnvConfig] = None
    #: **v2 per-session path (D2/D4).** A ``SandboxProvider`` that provisions a
    #: FRESH container per root-task tree (``LocalDockerSandboxProvider`` and
    #: friends). ``None`` (default) ⇒ no provisioning. Takes precedence over
    #: ``exec_env``. Paired with ``sandbox_spec`` (image / resource caps / the
    #: built-in + global skills mounts); the manager adds the per-session
    #: workspace mount at allocate time. A host runtime injection, never part of
    #: any agent identity.
    sandbox_provider: Optional[SandboxProvider] = None
    #: The deployment-fixed half of the per-session :class:`SandboxSpec` passed
    #: to ``sandbox_provider.allocate`` — image, resource caps, and the base
    #: mount list (built-in / global skills). ``None`` with a ``sandbox_provider``
    #: set ⇒ a bare spec (no base mounts); the workspace mount is always added
    #: per session. Ignored on the ``exec_env`` attach path.
    sandbox_spec: Optional[SandboxSpec] = None
    #: Per-session shell preamble source for the sandbox exec path:
    #: ``(exec_env_ref, argv) -> prefix``. The manager curries the session's
    #: durable ``exec_env_ref`` and invokes it FRESH for every container command,
    #: prepending the returned prefix (which must carry its own separator, e.g.
    #: ``export X=Y && ``) ahead of the command — the process twin of
    #: ``SandboxAuth.connect_headers`` for HTTP. Lets a product inject per-user
    #: credentials that expire mid-session (fetched fresh each exec). ``None``
    #: (default) ⇒ no preamble, byte-identical wire. A host runtime injection,
    #: never LLM-controlled and never recorded (D5); the callback must be total
    #: (return ``""`` on its own failure). Ignored when no sandbox is configured.
    sandbox_exec_preamble: Optional[Callable[[str, Sequence[str]], str]] = None
    #: Optional per-session backend factories threaded into the
    #: ``SandboxExecEnvManager``. ``None`` (default) ⇒ the SDK's hand-written
    #: ``AioSandboxExecEnv`` / ``AioBrowserBackend``. A product injects these to
    #: swap the sandbox wire without touching the seam — e.g. the official
    #: ``agent-sandbox`` SDK adapters in ``noeta.agent.host``. The adapters keep
    #: the same ``ExecEnv`` / ``BrowserBackend`` surface, so the tool schemas —
    #: and the stable prefix — are unchanged. Ignored when no sandbox is
    #: configured; a host runtime injection, never part of any agent identity.
    sandbox_backend_factory: Optional["BackendFactory"] = None
    sandbox_browser_factory: Optional["BrowserBackendFactory"] = None

    # -- memory store addressing --------------------------------------------
    #: Explicit memory-dir override forwarded to the SdkHost; ``None`` (default)
    #: falls through to ``global_memory_dir`` / the SDK global default
    #: (``~/.noeta/memories``). Same precedence chain as the host fields it
    #: forwards to (``memory_dir`` > ``global_memory_dir`` > default) — one
    #: chain for the memory tool pack, the resident index, recall, and the
    #: consolidation marker.
    memory_dir: Optional[Path] = None
    #: Deployment-level global memory root; ``None`` (default) keeps the SDK
    #: global default. Beaten by an explicit ``memory_dir`` override.
    global_memory_dir: Optional[Path] = None
    #: Per-task memory-root resolution seam for multi-tenant hosts: given a
    #: task id, return that task's memory root, or ``None`` to fall back to the
    #: precedence chain above. The SDK stays tenancy-agnostic — it knows tasks,
    #: not users; the embedding product owns the task→tenant mapping. When set,
    #: every consumer of the chain (memory tool pack + resident index at engine
    #: build, recall at the goal seam, ``Client.memory_root``) resolves through
    #: it first. The callable must be cheap and total (it runs on the engine
    #: build and goal paths) and deterministic for a given task id — a resumed
    #: task must resolve the same store. ``None`` (default) ⇒ today's
    #: host-level chain, byte-identical for single-tenant hosts.
    memory_root_resolver: Optional[Callable[[str], Optional[Path]]] = None

    # -- host kill-switches ------------------------------------------------
    workflow_allowed: bool = False
    #: process fs write policy — "dry_run" (stage a diff, safe default) or
    #: "apply" (real writes). Mapped to FsWriteMode by the Client.
    write_mode: str = "dry_run"

    def storage_triple(
        self,
    ) -> Optional[Tuple[EventLogFull, ContentStore, Dispatcher]]:
        """The injected ``(event_log, content_store, dispatcher)``, or ``None``.

        ``None`` ⇒ no external storage supplied (the Client builds in-memory).
        Raises :class:`ValueError` if only some of the triple is set — the three
        must be constructed and supplied together.
        """
        parts = (self.event_log, self.content_store, self.dispatcher)
        if all(p is None for p in parts):
            return None
        if any(p is None for p in parts):
            raise ValueError(
                "HostConfig storage is all-or-none: supply event_log, "
                "content_store and dispatcher together, or none of them"
            )
        # mypy: narrowed by the all/any guards above.
        return (  # type: ignore[return-value]
            self.event_log,
            self.content_store,
            self.dispatcher,
        )
