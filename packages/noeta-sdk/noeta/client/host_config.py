"""``HostConfig`` — the SDK's process-level wiring surface.

Everything here is decided once per process and is deliberately outside every
agent identity: the durable storage backend, the sandbox execution path, and
the host runtime injections. ``compile_options`` never sees a HostConfig, so
two clients differing only in theirs compile byte-identical AgentSpecs.
Every field defaults to "absent" — a bare ``HostConfig()`` is in-memory
storage, a local exec env, and no injections at all.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Collection,
    Mapping,
    Optional,
    Tuple,
)

from noeta.client.sandbox_provider import SandboxProvider, SandboxSpec
from noeta.client.storage_resolve import open_storage_stack
from noeta.execution.background_subagent import (
    DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_ROOT_TASK,
)
from noeta.client.otlp import OtlpHttpPost, OtlpTraceConfig
from noeta.client.webfetch_policy import normalize_allowed_hosts
from noeta.runtime.background_shell import DEFAULT_MAX_BACKGROUND_JOBS_PER_ROOT_TASK

if TYPE_CHECKING:
    # Annotation-only: ``noeta.client.sandbox`` imports this module, so a
    # runtime import back would be circular.
    from noeta.client.sandbox import BackendFactory, BrowserBackendFactory
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import DEFAULT_QUEUE, Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.messages import StreamDelta
from noeta.protocols.step_context import StepContext
from noeta.runtime.governance import (
    DEFAULT_HOOK_COMMAND_TIMEOUT_S,
    DEFAULT_HOOK_QUEUE_MAX,
    MatchArg,
    NotificationRule,
    PostToolUseRule,
    PreToolUseRule,
)
from noeta.runtime.mcp import HttpPostFn, McpAnyServerSpec
from noeta.runtime.worker import ReliabilitySink

#: The preview gateway as SDK core sees it: an OPAQUE object. Its real shape is
#: the app plugin's ``AppPreviewGateway`` Protocol (``noeta.builtins.app.impl``)
#: — the host drops the object into the kernel builder's ``backends`` bag under
#: the ``"app_preview"`` name and only the app pack ever calls it.
AppPreviewGateway = Any


__all__ = ["HooksConfig", "HostConfig", "SandboxExecEnvConfig", "WRITE_MODES"]


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


_HOOK_ACTIONS = frozenset({"allow", "deny", "require_approval"})
_MATCH_OPS = frozenset({"equals", "contains", "regex"})


def _check_command(where: str, command: Any, log: Any) -> None:
    if command is not None and (
        not isinstance(command, tuple)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ValueError(
            f"HooksConfig.{where}.command must be a non-empty tuple of "
            f"non-empty str (an argv, never run through a shell) or None; "
            f"got {command!r}"
        )
    if command is None and not log:
        raise ValueError(
            f"HooksConfig.{where} does nothing: set command, log=True, or both"
        )


def _check_pre_rule(where: str, rule: PreToolUseRule) -> PreToolUseRule:
    """Validate one pre-tool-use rule; compile a regex given as a string."""
    if rule.action not in _HOOK_ACTIONS:
        legal = ", ".join(sorted(_HOOK_ACTIONS))
        raise ValueError(
            f"HooksConfig.{where}.action must be one of {{{legal}}}; "
            f"got {rule.action!r}"
        )
    ma = rule.match_arg
    if ma is None:
        return rule
    if not isinstance(ma, MatchArg):
        raise ValueError(
            f"HooksConfig.{where}.match_arg must be a MatchArg; got {ma!r}"
        )
    if (
        not isinstance(ma.path, tuple)
        or not ma.path
        or not all(isinstance(key, str) and key for key in ma.path)
    ):
        raise ValueError(
            f"HooksConfig.{where}.match_arg.path must be a non-empty tuple of "
            f"argument keys (str); got {ma.path!r}"
        )
    if ma.op not in _MATCH_OPS:
        legal = ", ".join(sorted(_MATCH_OPS))
        raise ValueError(
            f"HooksConfig.{where}.match_arg.op must be one of {{{legal}}}; "
            f"got {ma.op!r}"
        )
    if ma.op == "contains" and not isinstance(ma.value, str):
        raise ValueError(
            f"HooksConfig.{where}.match_arg: op 'contains' needs a str value; "
            f"got {ma.value!r}"
        )
    if ma.op == "regex" and ma.pattern is None:
        # The guard reads only ``pattern``; a regex given as ``value`` is
        # compiled here, so a bad one fails now rather than never matching.
        if not isinstance(ma.value, str):
            raise ValueError(
                f"HooksConfig.{where}.match_arg: op 'regex' needs a pattern "
                f"(re.Pattern) or a str value; got {ma.value!r}"
            )
        try:
            pattern = re.compile(ma.value)
        except re.error as exc:
            raise ValueError(
                f"HooksConfig.{where}.match_arg: invalid regex "
                f"{ma.value!r}: {exc}"
            ) from exc
        return dataclasses.replace(
            rule, match_arg=dataclasses.replace(ma, pattern=pattern)
        )
    return rule


@dataclass(frozen=True)
class HooksConfig:
    """User hooks, set once per Client through :attr:`HostConfig.hooks`.

    * ``pre_tool_use`` — :class:`PreToolUseRule` s the ``HookGuard`` checks
      before each tool call (first match decides: allow / deny /
      require approval). They are part of the guard stack, so a resuming host
      must pass the same rules the original run used.
    * ``post_tool_use`` — :class:`PostToolUseRule` s: a command run after a
      matching tool call finishes.
    * ``notification`` — :class:`NotificationRule` s: a command run when a
      tool call starts waiting for approval.

    Post-tool-use and notification commands are side-effects only: one
    ``HookObserver`` per Client runs them in the background, as argv (never
    a shell) in the Client's workspace directory, with a minimal
    environment. Each is killed after ``command_timeout_s``; at most
    ``max_queue`` wait, and one past that is dropped with a warning rather
    than slowing the agent. They never fire on replay or resume.
    """

    pre_tool_use: tuple[PreToolUseRule, ...] = ()
    post_tool_use: tuple[PostToolUseRule, ...] = ()
    notification: tuple[NotificationRule, ...] = ()
    command_timeout_s: float = DEFAULT_HOOK_COMMAND_TIMEOUT_S
    max_queue: int = DEFAULT_HOOK_QUEUE_MAX

    def __post_init__(self) -> None:
        checked: list[PreToolUseRule] = []
        for group, kind in (
            ("pre_tool_use", PreToolUseRule),
            ("post_tool_use", PostToolUseRule),
            ("notification", NotificationRule),
        ):
            rules = getattr(self, group)
            if not isinstance(rules, tuple):
                raise ValueError(
                    f"HooksConfig.{group} must be a tuple of "
                    f"{kind.__name__}; got {type(rules).__name__}"
                )
            for i, rule in enumerate(rules):
                where = f"{group}[{i}]"
                if not isinstance(rule, kind):
                    raise ValueError(
                        f"HooksConfig.{where} must be a {kind.__name__}; "
                        f"got {rule!r}"
                    )
                if isinstance(rule, NotificationRule):
                    if rule.on != "approval":
                        raise ValueError(
                            f"HooksConfig.{where}.on must be 'approval' (the "
                            f"only notification moment); got {rule.on!r}"
                        )
                elif not isinstance(rule.match_tool, str) or not rule.match_tool:
                    raise ValueError(
                        f"HooksConfig.{where}.match_tool must be a non-empty "
                        f"tool-name pattern; got {rule.match_tool!r}"
                    )
                if isinstance(rule, PreToolUseRule):
                    checked.append(_check_pre_rule(where, rule))
                else:
                    _check_command(where, rule.command, rule.log)
        object.__setattr__(self, "pre_tool_use", tuple(checked))
        timeout = self.command_timeout_s
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not timeout > 0
        ):
            raise ValueError(
                "HooksConfig.command_timeout_s must be a positive number of "
                f"seconds; got {timeout!r}"
            )
        if (
            isinstance(self.max_queue, bool)
            or not isinstance(self.max_queue, int)
            or self.max_queue < 1
        ):
            raise ValueError(
                f"HooksConfig.max_queue must be a positive int; "
                f"got {self.max_queue!r}"
            )


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
    urllib). ``mcp_idle_ttl`` bounds how long an unused pooled connection
    stays open; ``mcp_scope_resolver`` partitions the pool per task (tenant /
    workspace). These are runtime objects, never part of the agent identity.

    ``workflow_allowed`` is the host kill-switch for the ``run_workflow`` control
    tool. ``write_mode`` is the process-level fs write policy (``"dry_run"``
    stages a proposed diff without touching disk — the safe default; ``"apply"``
    performs real writes); the Client maps it to the edit tools' ``FsWriteMode``.

    ``write_roots`` answers "may this task write HERE, outside its workspace?"
    — ``task_id -> extra writable directories``, consulted per call by ``edit``
    / ``Write``. ``None`` keeps the single-root wall: an
    out-of-workspace write simply fails, which is the only honest answer for a
    host with nobody to ask. A host that *can* ask — suspending the call for an
    owner's ruling and remembering it as a durable grant — wires this so the
    approved directory is open when the paused call resumes. Reads are never
    fenced and never consult it.

    Loop and tool-output bounds
    ---------------------------
    ``repetition_threshold`` registers the built-in ``RepetitionGuard`` (a
    model wedged on the identical ``(tool, arguments)`` call), and
    ``tool_output_inline_limit`` caps a tool result's inline characters before
    it is appended to the history. Both are ``None`` = off by default, which is
    what a host that never sets them has always run; both are positive ints
    when set. They are the two generic bounds a host with custom or MCP tools
    wants on — nothing else bounds an arg-identical loop below ReAct's
    1,000,000-step backstop, and nothing else bounds a third-party tool's
    inline payload.

    Web egress
    ----------
    ``webfetch_allowed_hosts`` names the hosts ``WebFetch`` may reach without
    human approval; every other host — intranet or public — routes the call
    through approval unless the session bypasses permissions. It is an approval
    knob only; ``WebFetch`` refuses no address of its own.
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
    #: This client's worker queue over the (possibly shared) store: root tasks
    #: it seeds are born on this queue, children inherit it, and its resident
    #: worker pool claims ONLY it — so differently-configured clients sharing
    #: one storage triple can never drive each other's work (ADR
    #: ``worker-queue-routing``). A wiring concern, never part of identity;
    #: single-store single-client setups keep the default and never see it.
    queue: str = DEFAULT_QUEUE

    # -- host runtime injections -------------------------------------------
    app_gateway: Optional[AppPreviewGateway] = None
    #: ``task_id -> the directories this task may write outside its workspace``
    #: (see the class docstring). A wiring concern, never part of identity.
    write_roots: Optional[Callable[[str], Sequence[str]]] = None
    mcp_server_resolver: Optional[
        Callable[[str], Optional[McpAnyServerSpec]]
    ] = None
    mcp_http_post: Optional[HttpPostFn] = None
    #: Idle expiry, in seconds, of a pooled MCP connection no turn holds;
    #: ``None`` ⇒ never. The Engine is built per turn, and its MCP
    #: connections come from one host-owned pool keyed by server identity
    #: (shared across tasks, released when the turn's Engine goes, closed on
    #: ``Client.shutdown()``); ``Client.reconnect_mcp()`` retires them early.
    mcp_idle_ttl: Optional[float] = 1800.0
    #: Per-task MCP pool partition: ``task_id -> scope name`` (a tenant id, a
    #: workspace) or ``None`` for the shared scope. Two tasks share a pooled
    #: connection only when the server identity AND the scope match, so a
    #: stateful stdio server (a browser, a login) never carries one tenant's
    #: state into another's turn. The same tenancy seam as
    #: ``memory_root_resolver`` — cheap, total, deterministic per task id.
    #: ``None`` (single-tenant) shares every connection.
    mcp_scope_resolver: Optional[Callable[[str], Optional[str]]] = None
    #: Token-streaming sink: ``(ctx, call_id, delta)`` receives ephemeral
    #: ``StreamDelta``s while a streaming-capable provider call is in flight
    #: (a product backend wires its delta hub here). ``None`` ⇒ no sink; deltas
    #: are never persisted either way.
    delta_sink: Optional[
        Callable[[StepContext, str, StreamDelta], None]
    ] = None
    #: Where the resident worker pool (``Client.start_workers``) reports its
    #: process-local reliability signals — a lease reclaimed under a running
    #: step, a heartbeat that lost its lease, a shutdown that abandoned a step,
    #: a dispatcher outage (``noeta.sdk.ReliabilityEvent``). Never recorded
    #: in the event log. ``None`` ⇒ each signal is logged as a warning.
    reliability_sink: Optional[ReliabilitySink] = None
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
    #: Memory names auto-recall never surfaces — not as a body, a pointer, a
    #: ``related`` neighbour, or a recall-judge candidate. For a page the host
    #: already rides into context by its own means (a resident of its own
    #: kind), which recall cannot see and would otherwise inject a second
    #: time. The index still lists the page and ``memory_read`` still reads
    #: it. Empty ⇒ every page is recallable.
    recall_exclude: Collection[str] = ()
    #: Cap on a page ``memory_write`` stores, in UTF-8 bytes — the body plus
    #: the frontmatter fields the tool writes with it, which is what recall
    #: and ``memory_read`` load. A larger write is refused before anything is written,
    #: with both numbers in the message, so the model can tighten or split the
    #: page while it still has the context. Worth setting under auto-recall's
    #: 4096-byte inline limit: a page past that limit is recalled as a
    #: one-line pointer, never whole. ``None`` ⇒ no cap.
    memory_max_bytes: Optional[int] = None
    #: Offer the agent ``memory_read`` and ``memory_search`` only: the store is
    #: someone else's to write — a curation pass, another agent, the operator.
    #: ``memory_write`` and ``memory_archive`` are absent from the tool list
    #: rather than refused, so the model never plans around a call it cannot
    #: make. The index resident and auto-recall are untouched, and the reserved
    #: ``__consolidation__`` curator keeps all four. ``False`` ⇒ all four tools.
    memory_read_only: bool = False
    #: Total budget for the rendered memory index, in estimated tokens. The
    #: index sits in the cached head of every request, so an unbounded one
    #: charges the store's page count to every turn. Over budget, entries
    #: degrade whole — full line, then name only, then a closing count
    #: pointing at ``memory_search`` — with the most recently written pages
    #: keeping the most. ``None`` ⇒ 1 % of the bound model's context window,
    #: the same share the ``skill`` roster takes.
    memory_index_budget_tokens: Optional[int] = None

    # -- skill menu ranking --------------------------------------------------
    #: Per-task keep order for the ``skill`` control tool's roster: given a
    #: task id, return ``{skill name: score}`` (higher keeps its summary
    #: longer when the roster is over budget) or ``None`` for no ranking.
    #: The same tenancy seam as ``memory_root_resolver`` — the SDK hands over
    #: task ids, the product maps them to tenants — and the same contract:
    #: cheap, total, and deterministic for a given task id, because the
    #: roster is composed once per build and a resumed task must compose the
    #: same bytes. Fold a tenant's ledger with ``skill_usage_from_events`` /
    #: ``rank_skills_by_usage`` to derive one. Reaches the ``skills`` pack as
    #: ``plugin_config["skills"]["menu_rank"]``; a static host-wide ranking
    #: can be set there directly instead. ``None`` ⇒ the default usage rank
    #: below, else tier + ``priority`` order only.
    skill_menu_rank_resolver: Optional[
        Callable[[str], Optional[Mapping[str, float]]]
    ] = None
    #: The default keep order when neither a resolver nor a static
    #: ``menu_rank`` is set: the host folds skill usage from the most recently
    #: updated task streams of the whole store (at most 200, refolded at most
    #: every 10 minutes), and each task keeps the first rank it composed with.
    #: Store-wide, so it is a single-tenant default: it stays off when
    #: ``memory_root_resolver`` or ``mcp_scope_resolver`` is bound (logged
    #: once) — that host passes ``skill_menu_rank_resolver`` instead. Best
    #: effort across processes: a task resumed in another process may compose
    #: a different roster once. ``False`` ⇒ tier + ``priority`` order only.
    skill_usage_ranking: bool = True

    # -- plugin operator config ---------------------------------------------
    #: Operator config per plugin: ``plugin name -> {key: value}``, reaching a
    #: ``session_pack`` factory as ``SessionBuildContext.config("<plugin
    #: name>")``. This is the channel a manifest's ``config-schema`` describes,
    #: and the one thing a third-party pack cannot obtain any other way — the
    #: SDK host derives entries only for the built-ins it knows by name.
    #:
    #: Merge rule, per plugin name: a name the host alone supplies passes
    #: through verbatim; a name the SDK also derives (``fs`` / ``skills`` /
    #: ``workspace`` / ``memory``) is a **shallow per-key overlay** — the
    #: host's keys win, the derived keys it does not mention survive. Wiring,
    #: never identity: two clients differing only here compile the same
    #: ``AgentSpec``.
    plugin_config: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    # -- model catalog extensions --------------------------------------------
    #: Operator model rows joining the shipped catalog: ``model id →
    #: ModelSpec`` (from ``noeta.sdk.providers``; typed ``Any`` here because
    #: the class lives in the providers built-in, reached only dynamically).
    #: For deployments the public table cannot know — internal gateway routing
    #: names, self-hosted models. Registered at Client construction; a name
    #: colliding with a shipped row/alias fails the build, and a spec must be
    #: registered identically on every run (the catalog feeds compaction
    #: derivation, which feeds composed bytes). Wiring, never identity.
    #: Registration lands when the Client is CONSTRUCTED: a host that consults
    #: catalog-derived surfaces earlier (``model_capabilities``,
    #: ``catalog_models`` at config-validation time) should call
    #: ``noeta.sdk.providers.register_models`` at process start instead.
    extra_models: Mapping[str, Any] = field(default_factory=dict)

    # -- loop and tool-output bounds -----------------------------------------
    #: How many times the model may repeat the SAME ``(tool, arguments)`` call
    #: inside the detection window before the built-in ``RepetitionGuard``
    #: intervenes (it asks for approval, the guard's own default action). The
    #: one loop nothing else bounds: ``Options.budget`` caps cost and tool
    #: calls only when the host sets them, and ReAct's ``max_steps`` backstop
    #: is 1,000,000 — so a tool that keeps returning the same error the model
    #: keeps retrying verbatim runs until the budget or the operator stops it.
    #: 3 is the guard's own threshold default. ``None`` ⇒ the guard is not
    #: registered at all (today's behavior).
    repetition_threshold: Optional[int] = None
    #: Inline character cap on a tool result BEFORE it is appended to the
    #: history (``docs/adr/unified-context-supply.md``): over the cap the model
    #: sees the first N characters plus a deterministic ``[tool output
    #: truncated: …]`` marker, while the full bytes stay in
    #: ``ToolResultRecorded.output_ref`` so audit loses nothing. The built-in
    #: tools cap themselves (``Read`` by lines, ``Bash`` at 30k, MCP at 1 MiB),
    #: so this is the generic backstop for host-supplied and MCP tools, which
    #: may return a multi-megabyte payload inline. A resumed task must reuse
    #: the value the original run used, or it re-derives different tool-output
    #: bytes. ``None`` ⇒ no truncation (today's behavior).
    tool_output_inline_limit: Optional[int] = None
    #: User hooks (:class:`HooksConfig`): pre-tool-use rules join the guard
    #: stack as the ``HookGuard``; post-tool-use and notification commands
    #: run from one background ``HookObserver`` the Client starts at
    #: construction and stops in ``shutdown``. ``None`` ⇒ no hooks.
    hooks: Optional[HooksConfig] = None

    # -- web egress ----------------------------------------------------------
    #: The hosts ``WebFetch`` may reach without asking a human. Operator
    #: configuration, trusted like ``shell_allowlist`` — never model input.
    #: Two forms: ``"example.com"`` (that host exactly) and
    #: ``"*.example.com"`` (its subdomains at any depth, but not the apex —
    #: list both for both). A malformed entry raises here rather than silently
    #: matching nothing.
    #:
    #: Under a gating permission mode (``default`` / ``acceptEdits``) a fetch
    #: of any other host — intranet or public — routes through HITL approval,
    #: per call, the way an unlisted ``Bash`` command does, so ``WebFetch``
    #: keeps ``risk_level="low"`` and a listed host stays prompt-free.
    #: ``bypassPermissions`` gates nothing, here as everywhere. Empty (the
    #: default) ⇒ every fetch is gated unless the session bypasses permissions.
    #:
    #: It is an **approval** knob and nothing more: ``WebFetch`` refuses no
    #: address, intranet or loopback, because an agent holding ``Bash`` reaches
    #: the same target with one ``curl``. A host that needs an egress boundary
    #: enforces it at the network or the sandbox.
    webfetch_allowed_hosts: Sequence[str] = ()

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
    #: The ``<workspace-environment>`` block switch, forwarded verbatim to
    #: ``SdkHost.environment_enabled``. On by default: every session records the
    #: workspace facts (directory, git repo flag, platform, task-start git
    #: branch / status / date) once at task start and renders them as the first
    #: semi-stable message. Off, the block is neither recorded nor rendered —
    #: for a host that supplies its own working-directory and clock context and
    #: does not want a second, task-start-frozen copy in front of the model.
    environment_enabled: bool = True
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
        # Both bounds spell "off" as ``None``. A 0 or a negative would be read
        # as a *disabled* guard by one consumer and an impossible cap by the
        # other, so refuse the ambiguity at construction rather than silently
        # running unguarded.
        for name, value in (
            ("repetition_threshold", self.repetition_threshold),
            ("tool_output_inline_limit", self.tool_output_inline_limit),
        ):
            if value is not None and value <= 0:
                raise ValueError(
                    f"HostConfig.{name} must be a positive int or None "
                    f"(None = off); got {value!r}"
                )
        # Values that type-check but mean nothing: each fails here, at
        # construction, instead of as a silent no-op or a raw driver error
        # on the first turn.
        if isinstance(self.recall_exclude, (str, bytes)) or not all(
            isinstance(name, str) for name in self.recall_exclude
        ):
            raise ValueError(
                "HostConfig.recall_exclude must be a collection of memory "
                f"names (e.g. a tuple of str), got {self.recall_exclude!r}"
            )
        for name, value in (
            ("memory_max_bytes", self.memory_max_bytes),
            ("memory_index_budget_tokens", self.memory_index_budget_tokens),
            (
                "max_background_jobs_per_root_task",
                self.max_background_jobs_per_root_task,
            ),
            (
                "max_background_subagents_per_root_task",
                self.max_background_subagents_per_root_task,
            ),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"HostConfig.{name} must be a positive int; got {value!r}"
                )
        if self.mcp_idle_ttl is not None and self.mcp_idle_ttl < 0:
            raise ValueError(
                "HostConfig.mcp_idle_ttl must be >= 0 seconds or None "
                f"(None = never expire); got {self.mcp_idle_ttl!r}"
            )
        if self.instructions_file is not None and not self.instructions_enabled:
            raise ValueError(
                "HostConfig.instructions_file is read only when "
                "instructions_enabled=True; set both, or drop instructions_file"
            )
        if self.storage_path is not None and not self.storage_path.strip():
            raise ValueError(
                "HostConfig.storage_path must be a sqlite file path, a "
                "postgresql:// DSN or ':memory:' — got an empty string"
            )
        if self.hooks is not None and not isinstance(self.hooks, HooksConfig):
            raise ValueError(
                "HostConfig.hooks must be a HooksConfig or None; "
                f"got {type(self.hooks).__name__}"
            )
        # The egress allowlist is a security knob, so a typo has to be loud: an
        # entry that quietly matches nothing would gate every fetch the
        # operator meant to open, and one that quietly matched too much would
        # open a host they never named.
        normalize_allowed_hosts(self.webfetch_allowed_hosts)

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
