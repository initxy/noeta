"""``HostConfig`` — the SDK's process-level wiring surface.

Everything here is decided once per process and is deliberately outside every
agent identity: the durable storage backend, the sandbox execution path, and
the host runtime injections. ``compile_options`` never sees a HostConfig, so
two clients differing only in theirs compile byte-identical AgentSpecs.
Every field defaults to "absent" — a bare ``HostConfig()`` is in-memory
storage, a local exec env, and no injections at all.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Tuple

from noeta.client.sandbox_provider import SandboxProvider, SandboxSpec
from noeta.client.storage_resolve import open_storage_stack
from noeta.execution.background_subagent import (
    DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_ROOT_TASK,
)
from noeta.client.otlp import OtlpHttpPost, OtlpTraceConfig
from noeta.runtime.background_shell import DEFAULT_MAX_BACKGROUND_JOBS_PER_ROOT_TASK

if TYPE_CHECKING:
    # Annotation-only: ``noeta.client.sandbox`` imports this module, so a
    # runtime import back would be circular.
    from noeta.client.sandbox import BackendFactory, BrowserBackendFactory
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.messages import StreamDelta
from noeta.protocols.step_context import StepContext
from noeta.runtime.mcp import HttpPostFn, McpAnyServerSpec

#: The preview gateway as SDK core sees it: an OPAQUE object. Its real shape is
#: the app plugin's ``AppPreviewGateway`` Protocol (``noeta.builtins.app.impl``)
#: — the host drops the object into the kernel builder's ``backends`` bag under
#: the ``"app_preview"`` name and only the app pack ever calls it.
AppPreviewGateway = Any


__all__ = ["HostConfig", "SandboxExecEnvConfig", "WRITE_MODES"]


#: Legal values for :attr:`HostConfig.write_mode`. ``"dry_run"`` stages a
#: proposed diff without touching disk (the safe default); ``"apply"`` performs
#: real writes. Validated in :meth:`HostConfig.__post_init__`.
WRITE_MODES = frozenset({"dry_run", "apply"})


@dataclass(frozen=True)
class SandboxExecEnvConfig:
    """Config for ATTACHING the fs / shell tools to one sandbox container.

    A pure, serialisable config value — it carries only *addressing*, never a
    live client or a secret. The host turns it into a live ``AioSandboxExecEnv``
    (reading the key from the environment, connecting to the container) and
    threads that into ``build_session_inputs``; the config alone is
    import-linter-safe for a backend to build.

    **Attach-only.** This config addresses one already-running container that
    every session shares, and it never owns it (release is a no-op, so a stop
    here cannot break a peer that reconnected to the same address). Per-session
    *provisioning* — a fresh container allocated when a session opens and torn
    down when it ends — is a different seam entirely:
    :class:`~noeta.client.sandbox_provider.SandboxProvider`.

    * ``base_url`` — the container's API root (e.g. ``http://host:8080``).
    * ``api_key_env`` — the environment variable holding the container's static
      ``SANDBOX_API_KEY``. The key rides only on the wire, never in a log, an
      event, or this config. ``None`` env value ⇒ no auth header.
    * ``workdir`` — the container's working directory. In sandbox mode this
      *is* the fs-tools' workspace root (a lexical containment fence): the host
      path a local session would use is meaningless inside the container, so
      the host substitutes this container path. Must be absolute.
    """

    base_url: str
    api_key_env: str = "SANDBOX_API_KEY"
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
    """Host-level wiring for a :class:`~noeta.client.client.Client`.

    Storage triple
    --------------
    ``event_log`` / ``content_store`` / ``dispatcher`` inject an external,
    typically durable storage backend. Supply **all three or none**; omitting
    them makes the Client build its own in-memory triple. The three are
    constructed together by the caller so the event log already holds the
    dispatcher as its ``lease_validator``.

    Runtime injections
    ------------------
    ``app_gateway`` is the live HTML-app preview gateway the ``open_app`` tool
    mounts against; ``None`` ⇒ no ``open_app`` tool at all. ``mcp_server_resolver``
    resolves an enabled MCP alias to its full connectable spec each turn;
    ``None`` ⇒ no live MCP is connected. ``mcp_http_post`` is an injectable HTTP
    transport for the remote-MCP client (tests pass a fake; ``None`` uses stdlib
    urllib). These are runtime objects, never part of the agent identity.

    ``workflow_allowed`` is the host kill-switch for the ``run_workflow`` control
    tool. ``write_mode`` is the process-level fs write policy (``"dry_run"``
    stages a proposed diff without touching disk — the safe default; ``"apply"``
    performs real writes); the Client maps it to the edit tools' ``FsWriteMode``.

    ``write_roots`` answers "may this task write HERE, outside its workspace?"
    — ``task_id -> extra writable directories``, consulted per call by ``edit``
    / ``write`` / ``apply_patch``. ``None`` keeps the single-root wall: an
    out-of-workspace write simply fails, which is the only honest answer for a
    host with nobody to ask. A host that *can* ask — suspending the call for an
    owner's ruling and remembering it as a durable grant — wires this so the
    approved directory is open when the paused call resumes. Reads are never
    fenced and never consult it.
    """

    # -- durable storage (all-or-none) -------------------------------------
    event_log: Optional[EventLogFull] = None
    content_store: Optional[ContentStore] = None
    dispatcher: Optional[Dispatcher] = None
    #: One-string durable storage: a sqlite file path, a ``postgresql://`` DSN,
    #: or ``":memory:"``. Resolved through
    #: :func:`noeta.sdk.storage.open_storage_stack`, which builds the whole
    #: triple **in the right order** (the event log needs the dispatcher as its
    #: ``lease_validator``). Mutually exclusive with the explicit triple above:
    #: supplying both is a loud error, because the two would disagree about
    #: which store the session actually writes to.
    storage_path: Optional[str] = None

    # -- host runtime injections -------------------------------------------
    app_gateway: Optional[AppPreviewGateway] = None
    #: ``task_id -> the directories this task may write outside its workspace``
    #: (see the class docstring). A wiring concern, never part of identity.
    write_roots: Optional[Callable[[str], Sequence[str]]] = None
    mcp_server_resolver: Optional[
        Callable[[str], Optional[McpAnyServerSpec]]
    ] = None
    mcp_http_post: Optional[HttpPostFn] = None
    #: Token-streaming sink: ``(ctx, call_id, delta)`` receives ephemeral
    #: ``StreamDelta``s while a streaming-capable provider call is in flight
    #: (a product backend wires its delta hub here). ``None`` ⇒ no sink; deltas
    #: are never persisted either way.
    delta_sink: Optional[
        Callable[[StepContext, str, StreamDelta], None]
    ] = None
    #: OTLP trace export: when set, the Client wires a
    #: :class:`noeta.observers.trace_export.TraceExportObserver` with an
    #: OTLP/HTTP JSON sink at the configured endpoint and stops it on
    #: ``shutdown``. ``None`` ⇒ no trace export. A host runtime injection like
    #: the preview gateway — never part of agent identity.
    otlp_traces: Optional[OtlpTraceConfig] = None
    #: Injectable HTTP transport for the OTLP exporter (tests pass a fake;
    #: ``None`` uses httpx) — the ``mcp_http_post`` pattern.
    otlp_http_post: Optional[OtlpHttpPost] = None
    #: Per-request provider header factory: ``(ctx) -> {header: value}`` called
    #: once per LLM round-trip, merged over the provider client's static
    #: headers. The product wires a stable per-task ``root_task_id`` here so a
    #: gateway that pins prompt-cache to a single backend account (via a
    #: per-request account-stickiness field the gateway keys on) keeps a long
    #: task on one account and actually reuses its KV cache. ``None`` ⇒ no per-request
    #: headers — a host runtime injection, never agent identity.
    provider_headers: Optional[Callable[[StepContext], Mapping[str, str]]] = None

    #: **Attach path.** ONE pre-existing sandbox container named by
    #: ``base_url``; every session on the host attaches it, and the Client wraps
    #: it into an attach ``SandboxProvider``. ``None`` ⇒ the local host
    #: (``LocalExecEnv``). Routing fs / shell side effects into a container
    #: leaves the tool schemas — and thus the stable prefix — untouched, which
    #: is why this can be a host runtime injection rather than agent identity.
    exec_env: Optional[SandboxExecEnvConfig] = None
    #: **Per-session path.** A ``SandboxProvider`` that provisions a FRESH
    #: container per root-task tree. ``None`` ⇒ no provisioning. Takes
    #: precedence over ``exec_env``. Paired with ``sandbox_spec`` (image /
    #: resource caps / the built-in + global skills mounts); the manager adds
    #: the per-session workspace mount at allocate time. A host runtime
    #: injection, never part of any agent identity.
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
    #: ⇒ no preamble. A host runtime injection, never LLM-controlled and never
    #: recorded; the callback must be total (return ``""`` on its own failure).
    #: Ignored when no sandbox is configured.
    sandbox_exec_preamble: Optional[Callable[[str, Sequence[str]], str]] = None
    #: Optional per-session backend factories threaded into the
    #: ``SandboxExecEnvManager``. ``None`` ⇒ the SDK's own ``AioSandboxExecEnv``
    #: / ``AioBrowserBackend``. A product injects these to swap the sandbox wire
    #: without touching the seam; the substitutes keep the same ``ExecEnv`` /
    #: ``BrowserBackend`` surface, so the tool schemas — and the stable prefix —
    #: are unaffected. Ignored when no sandbox is configured; a host runtime
    #: injection, never part of any agent identity.
    sandbox_backend_factory: Optional["BackendFactory"] = None
    sandbox_browser_factory: Optional["BrowserBackendFactory"] = None
    #: Per-session sandbox opt-out for **execution tiers**: given
    #: ``(root_task_id, workspace_dir)`` return whether to provision a
    #: container for THAT session. ``False`` ⇒ no container: the driver records
    #: no ``exec_env_ref`` and the build falls back to ``LocalExecEnv`` + the
    #: host ``WorkspaceRoot`` fence (the ``local`` tier), reachable even while a
    #: ``sandbox_provider`` is configured for other sessions. ``None`` ⇒ a
    #: configured provider provisions every session. Consulted at the top of
    #: ``NoetaHost.allocate_exec_env``; a host runtime injection, never part of
    #: any agent identity. Must be cheap, total, and deterministic for a given
    #: session — a resumed or reclaimed session must resolve the same answer.
    sandbox_policy: Optional[Callable[[str, Optional[str]], bool]] = None

    # -- memory store addressing --------------------------------------------
    #: Explicit memory-dir override forwarded to the SdkHost; ``None`` falls
    #: through to ``global_memory_dir`` / the SDK global default
    #: (``~/.noeta/memories``). One precedence chain (``memory_dir`` >
    #: ``global_memory_dir`` > default) serves the memory tool pack, the
    #: resident index, recall, and the consolidation marker alike.
    memory_dir: Optional[Path] = None
    #: Deployment-level global memory root; ``None`` keeps the SDK global
    #: default. Beaten by an explicit ``memory_dir`` override.
    global_memory_dir: Optional[Path] = None
    #: Per-task memory-root resolution seam for multi-tenant hosts: given a
    #: task id, return that task's memory root, or ``None`` to fall back to the
    #: precedence chain above. The SDK stays tenancy-agnostic — it knows tasks,
    #: not users; the embedding product owns the task→tenant mapping. When set,
    #: every consumer of the chain (memory tool pack + resident index at engine
    #: build, recall at the goal seam, ``Client.memory_root``) resolves through
    #: it first. The callable must be cheap and total (it runs on the engine
    #: build and goal paths) and deterministic for a given task id — a resumed
    #: task must resolve the same store. ``None`` ⇒ the host-level chain above.
    memory_root_resolver: Optional[Callable[[str], Optional[Path]]] = None

    # -- host kill-switches ------------------------------------------------
    workflow_allowed: bool = False
    #: Per-session background-job concurrency cap, forwarded to
    #: ``SdkHost.max_background_jobs_per_root_task``. Over the cap a
    #: ``shell_run(run_in_background=True)`` spawn is **rejected** (not queued)
    #: with a "kill one first" refusal the model can act on; no event is
    #: recorded, so the cap is invisible to resume. Default 8.
    max_background_jobs_per_root_task: int = DEFAULT_MAX_BACKGROUND_JOBS_PER_ROOT_TASK
    #: Per-session background SUB-AGENT concurrency cap, forwarded to
    #: ``SdkHost.max_background_subagents_per_root_task``
    #: (docs/adr/background-subagent.md). Over the cap a
    #: ``spawn_subagent(background=True)`` is rejected before any durable write.
    #: Default 8.
    max_background_subagents_per_root_task: int = (
        DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_ROOT_TASK
    )
    #: Project-instructions-file switch, forwarded verbatim to
    #: ``SdkHost.instructions_enabled``. When on, the session's workspace root
    #: is searched for ``NOETA.md`` → ``AGENTS.md`` (in that order) and the file
    #: is rendered into the stable head.
    instructions_enabled: bool = False
    #: Explicit path override for the instructions file; reads ONLY that path
    #: instead of the ``NOETA.md`` → ``AGENTS.md`` search. Requires
    #: ``instructions_enabled``; ``None`` (default) keeps the search.
    instructions_file: Optional[Path] = None
    #: ``read``-triggered discovery of subdirectory ``NOETA.md`` / ``AGENTS.md``
    #: files (docs/adr/anchored-content-placement.md). Forwarded verbatim to
    #: ``SdkHost.instructions_discovery``. When on, a successful ``read`` inside
    #: the session workspace activates every not-yet-active instruction file
    #: between the read file's directory and the workspace root; each renders
    #: anchored at its point of discovery, so a mid-task activation appends
    #: instead of rewriting the stable head. Independent of
    #: ``instructions_enabled``: that switch governs the workspace-ROOT file at
    #: session start, this one governs subdirectory files found while reading.
    instructions_discovery: bool = False
    write_mode: str = "dry_run"

    def __post_init__(self) -> None:
        # ``write_mode`` is consumed as ``FsWriteMode.APPLY if == "apply" else
        # DRY_RUN``, so a typo (``"Apply"`` / ``"apply "`` / ``"applied"``) would
        # silently fall back to dry-run and a user who meant real writes would get
        # none, with no error. Validate at construction the way ``Options``
        # validates ``permission_mode`` / ``thinking`` / ``effort``.
        if self.write_mode not in WRITE_MODES:
            legal = ", ".join(sorted(WRITE_MODES))
            raise ValueError(
                f"HostConfig.write_mode must be one of {{{legal}}}; "
                f"got {self.write_mode!r}"
            )

    def storage_triple(
        self,
    ) -> Optional[Tuple[EventLogFull, ContentStore, Dispatcher]]:
        """The injected ``(event_log, content_store, dispatcher)``, or ``None``.

        Two ways to supply it: :attr:`storage_path` (one string, resolved
        through :func:`noeta.sdk.storage.open_storage_stack`) or the explicit
        triple. ``None`` ⇒ neither was given, so the Client builds its
        in-memory triple.

        Raises :class:`ValueError` when both forms are supplied (they would
        name different stores) or when only some of the explicit triple is set
        — the three must be constructed and supplied together, since the event
        log takes the dispatcher as its ``lease_validator``.
        """
        event_log, content_store, dispatcher = (
            self.event_log,
            self.content_store,
            self.dispatcher,
        )
        explicit = (event_log, content_store, dispatcher)
        if self.storage_path is not None:
            if any(p is not None for p in explicit):
                raise ValueError(
                    "HostConfig: pass EITHER storage_path (the one-string "
                    "form) OR an explicit event_log/content_store/dispatcher "
                    "triple — not both"
                )
            return open_storage_stack(self.storage_path)
        if all(p is None for p in explicit):
            return None
        if event_log is None or content_store is None or dispatcher is None:
            raise ValueError(
                "HostConfig storage is all-or-none: supply event_log, "
                "content_store and dispatcher together, or none of them"
            )
        # The per-name None checks above exist because mypy cannot narrow a
        # tuple through ``all()``/``any()``; they keep this return cast-free.
        return (event_log, content_store, dispatcher)
