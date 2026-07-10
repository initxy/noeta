"""engine_room — the app's in-process noeta.sdk engine.

The product backend drives
agents through the **public** ``noeta.sdk`` client surface and nothing else —
this module imports only ``noeta.sdk``. A static check (and, from T8, an
import-linter contract) forbids any ``noeta.core`` / ``noeta.execution`` /
``noeta.policies`` / … import here. The runtime engine is a transitive
dependency the backend never names.

:class:`EngineRoom` wraps one noeta.sdk :class:`~noeta.sdk.Client` over a compiled
agent registry (the official presets by default) and exposes:

* the conversation **verbs** (start / send_goal / approve / deny / answer /
  deliver_event / cancel / close / reopen) the HTTP command endpoints (T5)
  translate into; and
* the canonical **EventEnvelope stream** (:meth:`events`) plus the human view
  (:meth:`messages`) the SSE layer (T5) multiplexes and the resource services
  (T6) reference.

``session`` is only ever a runner name — the backend builds no independent
session entity; a multi-turn conversation **is** a Task driven through these
verbs (the hard rule from D6 / T4).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from noeta.sdk import (
    NEXT_GOAL_WAKE_HANDLE,
    Client,
    ContentRef,
    HostConfig,
    LLMProvider,
    Options,
    consolidation_due,
    presets,
    run_consolidation,
)

from noeta.agent.backend.delta_hub import DeltaHub


_log = logging.getLogger("noeta.agent.backend")


class EngineRoom:
    """In-process noeta.sdk engine: conversation verbs + the envelope stream."""

    def __init__(
        self,
        options: Options,
        *,
        provider: LLMProvider,
        workspace_dir: Path,
        model: Optional[str] = None,
        host_config: Optional[HostConfig] = None,
        models: Sequence[str] = (),
        background_drive: bool = False,
        num_workers: int = 1,
        sandbox_enabled: bool = False,
        memory_consolidation: bool = False,
        memory_consolidation_debounce_hours: float = 24.0,
    ) -> None:
        # Memory consolidation (memory v2 T7): when on, register the internal
        # ``__consolidation__`` curator into the recipe BEFORE compiling the
        # Client, so the trigger's ``seed_start(agent=...)`` resolves. The
        # reserved name never enters main's spawnable roster (compile filters
        # ``__``-prefixed names from the auto-union), so main's compiled spec
        # and stable prefix are byte-identical either way; off (the default)
        # registers nothing — zero behavior, zero registry change.
        if (
            memory_consolidation
            and presets.CONSOLIDATION_AGENT_NAME not in options.agents
        ):
            options = presets.with_consolidation_agent(options)
        self._workspace_dir = Path(workspace_dir)
        # Whether the sandbox browser subsystem is active (sandbox_browser_options
        # were used at construction). Drives /capabilities → frontend visibility.
        self._sandbox_enabled = sandbox_enabled
        # T5 async contract ("commands return 202 + an ack only; every visible
        # change is observed through the stream"): when enabled, the turn-driving
        # verbs (start / send_goal / approve / deny / answer) SEED synchronously —
        # every durable, validated step, so typed 4xx rejections still raise on
        # the request thread — and hand the seed's lease back to the ready
        # queue. A resident worker pool (started lazily on first verb) drives
        # tasks to their trailing suspend / terminal.
        # The served product enables it (``BackendConfig.background_drive``);
        # the default False keeps in-process/embedded use synchronous.
        self._background_drive = background_drive
        self._num_workers = max(1, int(num_workers)) if background_drive else 0
        # Per-session workspace paths (task_id → absolute path), recorded when a
        # task is created with a non-default ``workspace_dir`` so the file
        # resource service (``/files`` / ``/file``) serves the tree the agent
        # actually edits, not the host-fixed default. Process-local (a restart
        # falls back to the host default until the session is re-driven — the
        # durable binding lives in the event log). One short path per task.
        self._task_workspaces: dict[str, Path] = {}
        self._model = model
        # The
        # configured model list doubles as the per-turn model-selector allowlist
        # (noeta-agent is ⊤ LOCAL_PRINCIPAL ⇒ config = deployment permission), so
        # real model ids pass the driver's selector check. Empty ⇒ the Client
        # keeps its STUB default (byte-identical single-model path).
        self._models: tuple[str, ...] = tuple(models)
        # The per-turn model-selector allowlist = the configured list PLUS the
        # host default model. Including the default ensures a turn that selects
        # the already-bound model (e.g. the composer echoing the current model)
        # is never rejected, and that a single-model deployment (empty ``models``)
        # can still select its one model. Empty (no list, no default) ⇒ None ⇒ the
        # Client keeps its STUB default, byte-identical to the pre-codex path.
        allowed = list(self._models)
        if model and model not in allowed:
            allowed.append(model)
        # Token streaming (delta hub): the room owns one DeltaHub and injects
        # its sink into the host config (host wiring, never AgentSpec identity —
        # the same column as storage / preview / MCP). A caller that already
        # supplied a ``delta_sink`` keeps it (the hub then simply never fires);
        # otherwise the hub becomes the sink so the SSE layer can subscribe via
        # :meth:`subscribe_deltas`. Deltas stay ephemeral — this changes no
        # durable behaviour.
        self._delta_hub = DeltaHub()
        if host_config is None:
            host_config = HostConfig(delta_sink=self._delta_hub.sink)
        elif host_config.delta_sink is None:
            host_config = dataclasses.replace(
                host_config, delta_sink=self._delta_hub.sink
            )
        self._client = Client(
            options,
            provider=provider,
            workspace_dir=self._workspace_dir,
            model=model,
            multi_turn=True,
            host_config=host_config,
            allowed_models=tuple(allowed) or None,
        )
        # Resident worker pool (background_drive only): started lazily on first
        # verb so the constructor stays side-effect free and tests that never
        # drive a verb don't need to shut threads down. ``_workers_started``
        # guards against double-start.
        self._workers_started = False
        self._workers_lock = threading.Lock()
        # Memory consolidation trigger (memory v2 T7 — the session-stop seams
        # of docs/adr/memory-consolidation.md). Two seams funnel into ONE
        # guard (:meth:`_maybe_consolidate_memory`): the explicit close verb,
        # and the turn boundary — observed here as the trailing next-goal
        # ``TaskSuspended`` on the post-commit envelope stream (a drive
        # completing back to suspended; approval / subtask / timer suspends
        # carry other wake shapes and never match). The tap itself only
        # filters and hands off to a daemon thread — it can never fail, block,
        # or slow the commit path.
        self._memory_consolidation = bool(memory_consolidation)
        self._memory_consolidation_debounce_hours = float(
            memory_consolidation_debounce_hours
        )
        # Serializes concurrent passes: the enqueue-time marker only lands
        # AFTER the digest build, so two near-simultaneous session stops
        # (parallel workers committing suspends, or close next to a turn
        # boundary) would both read a stale marker and double-enqueue. The
        # lock is taken non-blocking — the loser drops its attempt, the
        # winner's marker debounces every later boundary.
        self._consolidation_pass_lock = threading.Lock()
        self._consolidation_unsubscribe: Optional[Callable[[], None]] = None
        if self._memory_consolidation:
            self._consolidation_unsubscribe = self._client.subscribe(
                self._observe_consolidation_boundary
            )

    @property
    def workspace_dir(self) -> Path:
        """The host-fixed default sandbox root (the single-workspace path)."""
        return self._workspace_dir

    @property
    def sandbox_enabled(self) -> bool:
        """True when the sandbox browser subsystem was activated at construction.

        Drives ``GET /capabilities`` → frontend conditionally renders sandbox
        affordances (indicator dot, browser screenshot inline display).
        """
        return self._sandbox_enabled

    def workspace_dir_for(self, task_id: Optional[str]) -> Path:
        """The workspace root the file resource service serves for ``task_id``.

        Returns the per-session workspace a non-default task was created under,
        else the host-fixed default (no task given, an unknown task, or a task
        created without an explicit workspace). Keeps ``/files`` / ``/file``
        in step with the project the agent actually edits."""
        if task_id is None:
            return self._workspace_dir
        return self._task_workspaces.get(task_id, self._workspace_dir)

    @property
    def model(self) -> Optional[str]:
        """The host-bound default model selector (``None`` ⇒ provider default).

        The model bound at construction (bypasses the selector allowlist); a
        per-turn ``model_selector`` switch (must be in :attr:`models`) drives the
        next turn.
        """
        return self._model

    @property
    def models(self) -> List[str]:
        """The configured selectable model list (the composer's model dropdown).

        Empty ⇒ only the host default :attr:`model` is bound (no per-turn
        switching). Doubles as the per-turn selector allowlist on the ⊤ local
        principal.
        """
        return list(self._models)

    def agent_names(self) -> list[str]:
        """The compiled agent registry's names (main + subagents).

        The capabilities projection's ``agents`` dropdown. Read off the public
        ``Client.registry`` so the backend never names the identity layer.
        Reserved ``__``-prefixed names (the internal ``__consolidation__``
        curator) are host-driven identities, never user-selectable, so they
        are filtered here — ``/capabilities`` never advertises them.
        """
        try:
            return [
                n for n in self._client.registry.names() if not n.startswith("__")
            ]
        except Exception:
            return []

    @classmethod
    def official(
        cls,
        *,
        provider: LLMProvider,
        workspace_dir: Path,
        model: Optional[str] = None,
        host_config: Optional[HostConfig] = None,
        models: Sequence[str] = (),
        background_drive: bool = False,
        num_workers: int = 1,
        sandbox_browser: bool = False,
        memory_consolidation: bool = False,
        memory_consolidation_debounce_hours: float = 24.0,
    ) -> "EngineRoom":
        """Build the room over the official preset registry (main + subagents).

        ``host_config`` threads durable storage + the host runtime injections
        (preview gateway, live-MCP resolver) through to the noeta.sdk Client;
        ``None`` ⇒ the in-memory, no-preview, no-MCP default. ``models`` is the
        configured selectable model list (empty ⇒ single-model path).

        ``sandbox_browser`` activates the sandbox browser subsystem (spec layer
        4): when True, the ``web`` browsing subagent — the sole identity that
        opens ``browser`` — is registered into main's delegation roster. Main
        itself stays browser-free (no ``browser_*`` tools); every page
        interaction is delegated to ``web``, whose browser pack is merged
        per-session (gated on a live sandbox backend). Off by default so a
        non-sandbox deployment keeps the pre-browser roster + stable prefix
        byte-identical. A product sets this from its ``sandbox_enabled`` config.

        ``memory_consolidation`` activates the background memory-curation
        trigger (memory v2 T7): the internal ``__consolidation__`` agent is
        registered (constructor concern — see ``__init__``) and the
        session-stop seams (close + turn boundary) start funnelling into the
        debounced ``run_consolidation`` guard. A product sets this from its
        ``memory_consolidation`` config; the ``False`` default keeps embedded
        / test rooms at zero behavior.
        """
        options = (
            presets.sandbox_browser_options() if sandbox_browser else presets.main_options()
        )
        return cls(
            options,
            provider=provider,
            workspace_dir=workspace_dir,
            model=model,
            host_config=host_config,
            models=models,
            background_drive=background_drive,
            num_workers=num_workers,
            sandbox_enabled=sandbox_browser,
            memory_consolidation=memory_consolidation,
            memory_consolidation_debounce_hours=memory_consolidation_debounce_hours,
        )

    # -- introspection -----------------------------------------------------

    @property
    def main_agent_name(self) -> str:
        return self._client.main_agent_name

    def events(self, task_id: str) -> list[Any]:
        """The canonical EventEnvelope stream for ``task_id`` (D6: wire it raw)."""
        return self._client.events(task_id)

    def events_after(self, task_id: str, after_seq: Optional[int] = None) -> list[Any]:
        """``task_id``'s envelope stream strictly past ``after_seq`` (cursor catch-up)."""
        return self._client.events_after(task_id, after_seq)

    def task_streams(self) -> list[Any]:
        """Enumerate every task stream (``task_id`` + ``last_seq``) for tree discovery."""
        return self._client.task_streams()

    def subscribe(self, callback: Any) -> Any:
        """Subscribe to the live, post-commit envelope stream (all tasks)."""
        return self._client.subscribe(callback)

    def subscribe_deltas(
        self, callback: Callable[[str, str, Any], None]
    ) -> Callable[[], None]:
        """Subscribe to the ephemeral token-delta stream (all tasks).

        ``callback(task_id, call_id, delta)`` fires on the LLM drive thread
        while a streaming provider call is in flight; returns an unsubscribe.
        Deltas are never persisted or replayed — the SSE layer projects them
        as ``event: delta`` frames without an ``id:`` (cursor untouched).
        """
        return self._delta_hub.subscribe(callback)

    def get_content(self, content_hash: str) -> Optional[bytes]:
        """Deref a ContentRef's bytes by hash (T6 ``/content/{hash}``)."""
        return self._client.get_content(content_hash)

    def put_content(self, body: bytes, *, media_type: str) -> ContentRef:
        """Store ``body`` and return its ``ContentRef`` (image-input write side)."""
        return self._client.put_content(body, media_type=media_type)

    # -- sandbox lifecycle wiring (product side) ----------------------------

    def add_sandbox_lifecycle_listener(
        self, on_allocate: Any, on_release: Any
    ) -> None:
        """Register container allocate/release listeners on the SDK host.

        Delegates to :meth:`Client.add_sandbox_lifecycle_listener` →
        :meth:`SdkHost.add_sandbox_lifecycle_listener` →
        :meth:`SandboxExecEnvManager.add_lifecycle_listener`. Used by the
        product backend to wire the sandbox preview gateway to the container
        lifecycle (mount on allocate, unmount on release).
        """
        self._client.add_sandbox_lifecycle_listener(on_allocate, on_release)

    def messages(self, task_id: str) -> list[Any]:
        """The folded human-readable message view for ``task_id``."""
        return self._client.messages(task_id)

    # -- worker pool (lifecycle) ------------------------------------------

    def _ensure_workers(self) -> None:
        """Start the resident worker pool on first use (idempotent).

        Only meaningful when ``background_drive=True``; a no-op otherwise.
        Lazy start keeps the constructor side-effect free and prevents
        test-only EngineRooms that never drive a verb from leaking threads.
        """
        if not self._background_drive:
            return
        with self._workers_lock:
            if self._workers_started:
                return
            self._client.start_workers(num_workers=self._num_workers)
            self._workers_started = True

    # -- conversation verbs (T5 maps HTTP commands → these) ----------------

    def start(
        self,
        *,
        goal: str,
        agent: Optional[str] = None,
        images: Sequence[Any] = (),
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        workspace_dir: Optional[str] = None,
        model_selector: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> str:
        """Create a Task, drive its first turn, return the new ``task_id``.

        ``permission_mode`` / ``enabled_mcp`` are the per-turn host knobs the
        command endpoint forwards from the request body (approval mode + the
        MCP aliases enabled for this conversation). ``workspace_dir`` is the
        chosen project's absolute path (welded into durable ``TaskHostBound`` —
        passed once here, fold-resolved on every later turn); ``model_selector`` /
        ``effort`` are the per-turn model + reasoning-effort selectors. All
        default to ``None`` ⇒ the host-fixed workspace / model / effort,
        byte-identical to the single-workspace path.

        With ``background_drive`` the durable seed (task creation, goal
        append, selector validation, lease) still runs on this thread — the
        typed 4xx contract is unchanged — and the seed's lease is yielded
        back to the ready queue; a resident worker drives the turn. The
        ``task_id`` returns immediately and progress rides the SSE stream.
        """
        if self._background_drive:
            self._ensure_workers()
            seeded = self._client.seed_start(
                goal=goal,
                agent=agent,
                images=images,
                permission_mode=permission_mode,
                enabled_mcp=enabled_mcp,
                workspace_dir=workspace_dir,
                model_selector=model_selector,
                effort=effort,
            )
            task_id = seeded.task_id
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001 — SDK surface
        else:
            outcome = self._client.start(
                goal=goal,
                agent=agent,
                images=images,
                permission_mode=permission_mode,
                enabled_mcp=enabled_mcp,
                workspace_dir=workspace_dir,
                model_selector=model_selector,
                effort=effort,
            )
            task_id = outcome.task_id
        if workspace_dir:
            # Remember the chosen project so /files + /file serve THIS session's
            # tree (the host-fixed default otherwise diverges from where the
            # agent works).
            self._task_workspaces[task_id] = Path(workspace_dir)
        return task_id

    def send_goal(
        self,
        task_id: str,
        *,
        goal: str,
        images: Sequence[Any] = (),
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        model_selector: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> None:
        """Append a new user turn (no ``workspace_dir``: a follow-up turn
        fold-resolves the workspace the session was created with)."""
        if self._background_drive:
            self._ensure_workers()
            seeded = self._client.seed_send_goal(
                task_id,
                goal=goal,
                images=images,
                permission_mode=permission_mode,
                enabled_mcp=enabled_mcp,
                model_selector=model_selector,
                effort=effort,
            )
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001
            return
        self._client.send_goal(
            task_id,
            goal=goal,
            images=images,
            permission_mode=permission_mode,
            enabled_mcp=enabled_mcp,
            model_selector=model_selector,
            effort=effort,
        )

    def approve(
        self, task_id: str, *, call_id: str, reason: Optional[str] = None
    ) -> None:
        if self._background_drive:
            self._ensure_workers()
            seeded = self._client.seed_approve(task_id, call_id=call_id, reason=reason)
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001
            return
        self._client.approve(task_id, call_id=call_id, reason=reason)

    def deny(self, task_id: str, *, call_id: str, reason: Optional[str] = None) -> None:
        if self._background_drive:
            self._ensure_workers()
            seeded = self._client.seed_deny(task_id, call_id=call_id, reason=reason)
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001
            return
        self._client.deny(task_id, call_id=call_id, reason=reason)

    def answer(
        self, task_id: str, *, question_id: str, answers: dict[str, Any]
    ) -> None:
        if self._background_drive:
            self._ensure_workers()
            seeded = self._client.seed_answer(
                task_id, question_id=question_id, answers=answers
            )
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001
            return
        self._client.answer(task_id, question_id=question_id, answers=answers)

    def deliver_event(
        self, task_id: str, *, event_kind: str, payload: Any = None
    ) -> None:
        """Deliver an external event to a ``wait_external``-suspended task."""
        if self._background_drive:
            self._ensure_workers()
            seeded = self._client.seed_deliver_event(
                task_id, event_kind=event_kind, payload=payload
            )
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001
            return
        self._client.deliver_event(task_id, event_kind=event_kind, payload=payload)

    # -- graceful shutdown / idle wait ------------------------------------

    def join_drives(self, timeout: Optional[float] = None) -> bool:
        """Wait until the ready queue is empty and no worker holds a lease.

        This is the background_drive equivalent of "wait for in-flight
        drive threads to finish" used by tests and graceful shutdown.
        Returns True when the dispatcher reports no ready and no leased
        tasks within ``timeout`` (``None`` = wait indefinitely).

        Implementation note: we poll the host's dispatcher (a backend-
        private seam that stays an implementation detail) and count rows
        whose ``status`` is ``'ready'`` (waiting for a worker) or
        ``'leased'`` (a worker is actively driving). A suspended task is
        correctly NOT counted (it is idle, waiting on an external wake);
        a terminal task is NOT counted. We require **three** consecutive
        empty polls with a short gap — two was insufficient because a
        wake delivered between a release(suspended) and the next poll
        could leave the streak at 2 if both polls fell in the window
        where the suspended→ready transition hadn't been observed yet.
        Three polls (two gaps ≈ 100 ms by default) is wide enough to
        span one full worker poll cycle, ruling out false idle.
        """
        import time as _time

        if not self._background_drive or not self._workers_started:
            # Synchronous mode: nothing is in flight by definition.
            return True
        deadline = None if timeout is None else _time.monotonic() + timeout
        # Poll dispatcher state through the client's diagnostic seam. We
        # reach for the host's dispatcher (injected Client) and count rows
        # that are 'ready' or 'leased'. Three consecutive empty polls ⇒ idle.
        host = self._client._host  # noqa: SLF001
        dispatcher = host.dispatcher
        idle_streak = 0
        gap = 0.05
        required_streak = 3
        while True:
            busy = _count_busy_tasks(dispatcher)
            if busy == 0:
                idle_streak += 1
                if idle_streak >= required_streak:
                    return True
            else:
                idle_streak = 0
            if deadline is not None and _time.monotonic() >= deadline:
                return False
            _time.sleep(gap)

    def cancel(
        self, task_id: str, *, reason: str = "cancelled", cascade: bool = False
    ) -> None:
        self._client.cancel(task_id, reason=reason, cascade=cascade)

    def close(self, task_id: str, *, reason: Optional[str] = None) -> None:
        self._client.close(task_id, reason=reason)
        # Session-stop seam (a): the explicit close cascade. Fire-and-forget —
        # the guard hands off to a daemon thread, so the HTTP 202 never waits
        # on (and can never be failed by) the consolidation pass.
        self._maybe_consolidate_memory(task_id)

    def reopen(self, task_id: str, *, reason: Optional[str] = None) -> None:
        self._client.reopen(task_id, reason=reason)

    # -- session management ------------------------------------------------

    def delete_task(self, task_id: str) -> dict[str, Any]:
        """Hard-delete a session (task + subtask tree) via the noeta.sdk Client.

        The thin backend has no independent session entity — a conversation IS a
        Task — so deletion purges the task's persisted stream. Returns the
        Client's typed result (``ok`` / ``reason`` ∈ {not_found, running}) the
        ``DELETE /tasks/{id}`` handler maps onto a status code.
        """
        result = self._client.delete_task(task_id)
        if result.get("ok"):
            for deleted in result.get("deleted", []):
                self._task_workspaces.pop(deleted, None)
        return result

    # -- memory consolidation (memory v2 T7) --------------------------------

    def memory_root(self) -> Path:
        """The SDK host's resolved memory-store root (marker + store home).

        ``Client.memory_root`` → ``SdkHost.memory_root`` — one precedence
        chain (``memory_dir`` > ``global_memory_dir`` > ``~/.noeta/memories``)
        shared with the memory tools, so the consolidation marker always sits
        next to the store the curation run mutates.
        """
        return self._client.memory_root()

    def _observe_consolidation_boundary(self, env: Any) -> None:
        """Session-stop seam (b): the turn boundary, off the envelope tap.

        Fires post-commit for EVERY envelope; matches only the trailing
        next-goal ``TaskSuspended`` (a drive completing back to suspended —
        the chat turn boundary). Runs on the committing drive thread, so it
        must stay cheap and must never raise into the event log.
        """
        try:
            if getattr(env, "type", None) != "TaskSuspended":
                return
            wake_on = getattr(getattr(env, "payload", None), "wake_on", None)
            if getattr(wake_on, "handle", None) != NEXT_GOAL_WAKE_HANDLE:
                return
            self._maybe_consolidate_memory(env.task_id)
        except Exception:
            _log.exception("memory-consolidation turn-boundary tap failed")

    def _maybe_consolidate_memory(self, task_id: Optional[str]) -> None:
        """The one consolidation guard both session-stop seams funnel into.

        Config off ⇒ no-op. The actual pass (debounce read, digest build,
        enqueue) runs on a fire-and-forget daemon thread — it can NEVER fail
        or block the user path; every failure is logged and swallowed.
        Requires the resident pool (``background_drive``): the run is seeded
        onto the ready queue for a worker to drive, so a synchronous embedded
        room skips the trigger (such hosts call ``noeta.sdk.run_consolidation``
        themselves — the memory-v2 decision-#11 layering).
        """
        try:
            if not self._memory_consolidation or not self._background_drive:
                return
            threading.Thread(
                target=self._consolidation_pass,
                args=(task_id,),
                name="noeta-memory-consolidation",
                daemon=True,
            ).start()
        except Exception:
            _log.exception("memory-consolidation trigger failed")

    def _consolidation_pass(self, task_id: Optional[str]) -> None:
        """One debounced consolidation attempt (daemon-thread body).

        At most one pass runs at a time (``_consolidation_pass_lock``,
        non-blocking): between the debounce read and the enqueue-time marker
        write sits the whole digest build, and without the lock two
        near-simultaneous session stops would both find the marker stale and
        seed two curation runs. Within the winning pass, order matters for
        cost: the marker read (one small file) gates first — on the
        overwhelmingly common not-due boundary nothing else runs; only then
        the triggering task's genesis is peeked to skip a reserved
        (``__consolidation__``) session's own boundary (no self-retrigger; the
        enqueue-time marker already protects when the debounce is nonzero).
        ``run_consolidation`` re-checks the debounce, builds the digest,
        writes the marker at enqueue time, and seeds the curation root task.
        """
        if not self._consolidation_pass_lock.acquire(blocking=False):
            return
        try:
            memory_root = self.memory_root()
            now = datetime.now(timezone.utc)
            if not consolidation_due(
                memory_root,
                now=now,
                debounce_hours=self._memory_consolidation_debounce_hours,
            ):
                return
            if task_id is not None and self._agent_name_of(task_id).startswith(
                "__"
            ):
                return
            self._ensure_workers()
            run_consolidation(
                self._client,
                memory_root=memory_root,
                now=now,
                debounce=True,
                debounce_hours=self._memory_consolidation_debounce_hours,
            )
        except Exception:
            _log.exception("memory-consolidation pass failed")
        finally:
            self._consolidation_pass_lock.release()

    def _agent_name_of(self, task_id: str) -> str:
        """The task's genesis ``TaskCreated.agent_name`` (``""`` if unknown)."""
        try:
            for env in self._client.events(task_id):
                if getattr(env, "type", None) == "TaskCreated":
                    return str(getattr(env.payload, "agent_name", "") or "")
        except Exception:
            pass
        return ""

    # -- shutdown ----------------------------------------------------------

    def shutdown(self) -> None:
        # Unhook the consolidation tap first so a suspend committed during
        # teardown cannot spawn a new pass against a closing client.
        if self._consolidation_unsubscribe is not None:
            try:
                self._consolidation_unsubscribe()
            except Exception:
                pass
            self._consolidation_unsubscribe = None
        # Stop resident workers first (bounded grace) so no step is in flight
        # when the client (and any injected durable storage) closes under them.
        if self._workers_started:
            try:
                if not self._client.stop_workers(timeout=10.0):
                    _log.warning(
                        "engine_room shutdown: workers still in flight "
                        "after grace; closing anyway (recovery via requeue_stale)"
                    )
            except Exception:
                _log.exception("engine_room shutdown: error stopping workers")
        self._client.shutdown()


def _count_busy_tasks(dispatcher: Any) -> int:
    """Count dispatcher rows in 'ready' or 'leased' status.

    Uses the concrete dispatcher's introspection surface; sqlite/postgres
    adaptors expose this via their connection, InMemoryDispatcher via its
    ``_tasks`` dict.

    Error policy: if we cannot introspect (unknown dispatcher shape or a
    transient SQL error), we re-raise to the caller rather than silently
    returning 0 — returning 0 would make ``join_drives`` declare idle
    mid-flight and let ``shutdown`` close durable storage under a worker
    step, which is exactly the loss mode we built step-attempt recovery
    for but would still be user-visible. A test double that exposes
    neither ``_conn`` nor ``_tasks`` causes ``join_drives`` to time out
    rather than falsely succeed.
    """
    # Sqlite / Postgres dispatcher — query through its _conn.
    conn = getattr(dispatcher, "_conn", None)
    if conn is not None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM dispatcher_tasks "
            "WHERE status IN ('ready','leased')"
        ).fetchone()
        return int(row[0] if isinstance(row, (tuple, list)) else row["n"])
    # InMemoryDispatcher.
    tasks = getattr(dispatcher, "_tasks", None)
    if isinstance(tasks, dict):
        return sum(
            1
            for t in tasks.values()
            if getattr(t, "status", None) in ("ready", "leased")
        )
    raise RuntimeError(
        "_count_busy_tasks: dispatcher %r exposes neither _conn nor _tasks; "
        "cannot detect idleness" % (type(dispatcher).__name__,)
    )
