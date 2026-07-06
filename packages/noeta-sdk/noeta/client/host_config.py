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

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.messages import StreamDelta
from noeta.protocols.step_context import StepContext
from noeta.tools.app import AppPreviewGateway
from noeta.tools.mcp import HttpPostFn, McpAnyServerSpec


__all__ = ["HostConfig"]


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
