"""engine_room — the app's in-process noeta.sdk engine.

The product backend drives
agents through the **public** ``noeta.sdk`` client surface and nothing else —
this module imports only ``noeta.sdk``. A static check (and, from T8, an
import-linter contract) forbids any ``noeta.core`` / ``noeta.execution`` /
``noeta.policies`` / … import here. The runtime engine is a transitive
dependency the backend never names.

:class:`EngineRoom` wraps one noeta.sdk :class:`~noeta.sdk.Client` over a compiled
agent registry (the official presets by default) and exposes:

* the eight conversation **verbs** (start / send_goal / approve / deny / answer
  / cancel / close / reopen) the HTTP command endpoints (T5) translate into; and
* the canonical **EventEnvelope stream** (:meth:`events`) plus the human view
  (:meth:`messages`) the SSE layer (T5) multiplexes and the resource services
  (T6) reference.

``session`` is only ever a runner name — the backend builds no independent
session entity; a multi-turn conversation **is** a Task driven through these
verbs (the hard rule from D6 / T4).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, List, Optional, Sequence

from noeta.sdk import Client, ContentRef, HostConfig, LLMProvider, Options, presets


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
    ) -> None:
        self._workspace_dir = Path(workspace_dir)
        # T5 async contract ("commands return 202 + an ack only; every visible
        # change is observed through the stream"): when enabled, the turn-driving
        # verbs (start / send_goal / approve / deny / answer) SEED synchronously —
        # every durable, validated step, so typed 4xx rejections still raise on
        # the request thread — and run the blocking drive on a daemon thread.
        # The served product enables it (``BackendConfig.background_drive``);
        # the default False keeps in-process/embedded use synchronous.
        self._background_drive = background_drive
        # In-flight background drives (task_id → thread): joined by shutdown so
        # a durable store is not closed under a mid-flight turn, and by tests.
        self._drives: dict[str, threading.Thread] = {}
        self._drives_lock = threading.Lock()
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
        self._client = Client(
            options,
            provider=provider,
            workspace_dir=self._workspace_dir,
            model=model,
            multi_turn=True,
            host_config=host_config,
            allowed_models=tuple(allowed) or None,
        )

    @property
    def workspace_dir(self) -> Path:
        """The host-fixed default sandbox root (the single-workspace path)."""
        return self._workspace_dir

    def workspace_dir_for(self, task_id: Optional[str]) -> Path:
        """The workspace root the file resource service serves for ``task_id``.

        Returns the per-session workspace a non-default task was created under,
        else the host-fixed default (no task given, an unknown task, or a task
        created without an explicit workspace). Keeps ``/files`` / ``/file`` in
        step with the project the agent actually edits."""
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
        """
        try:
            return list(self._client.registry.names())
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
    ) -> "EngineRoom":
        """Build the room over the official preset registry (main + subagents).

        ``host_config`` threads durable storage + the host runtime injections
        (preview gateway, live-MCP resolver) through to the noeta.sdk Client;
        ``None`` ⇒ the in-memory, no-preview, no-MCP default. ``models`` is the
        configured selectable model list (empty ⇒ single-model path).
        """
        return cls(
            presets.main_options(),
            provider=provider,
            workspace_dir=workspace_dir,
            model=model,
            host_config=host_config,
            models=models,
            background_drive=background_drive,
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

    def get_content(self, content_hash: str) -> Optional[bytes]:
        """Deref a ContentRef's bytes by hash (T6 ``/content/{hash}``)."""
        return self._client.get_content(content_hash)

    def put_content(self, body: bytes, *, media_type: str) -> ContentRef:
        """Store ``body`` and return its ``ContentRef`` (image-input write side)."""
        return self._client.put_content(body, media_type=media_type)

    def messages(self, task_id: str) -> list[Any]:
        """The folded human-readable message view for ``task_id``."""
        return self._client.messages(task_id)

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
        command endpoint forwards from the request body (approval mode + the MCP
        aliases enabled for this conversation). ``workspace_dir`` is the chosen
        project's absolute path (welded into durable ``TaskHostBound`` — passed
        once here, fold-resolved on every later turn); ``model_selector`` /
        ``effort`` are the per-turn model + reasoning-effort selectors. All
        default to ``None`` ⇒ the host-fixed workspace / model / effort,
        byte-identical to the single-workspace path.

        With ``background_drive`` the durable seed (task creation, goal
        append, selector validation, lease) still runs on this thread — the
        typed 4xx contract is unchanged — and the blocking turn drives on a
        daemon thread; the ``task_id`` returns immediately and progress rides
        the SSE stream.
        """
        if self._background_drive:
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
            self._spawn_drive(seeded)
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
            self._spawn_drive(
                self._client.seed_send_goal(
                    task_id,
                    goal=goal,
                    images=images,
                    permission_mode=permission_mode,
                    enabled_mcp=enabled_mcp,
                    model_selector=model_selector,
                    effort=effort,
                )
            )
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
            self._spawn_drive(
                self._client.seed_approve(task_id, call_id=call_id, reason=reason)
            )
            return
        self._client.approve(task_id, call_id=call_id, reason=reason)

    def deny(
        self, task_id: str, *, call_id: str, reason: Optional[str] = None
    ) -> None:
        if self._background_drive:
            self._spawn_drive(
                self._client.seed_deny(task_id, call_id=call_id, reason=reason)
            )
            return
        self._client.deny(task_id, call_id=call_id, reason=reason)

    def answer(
        self, task_id: str, *, question_id: str, answers: dict[str, Any]
    ) -> None:
        if self._background_drive:
            self._spawn_drive(
                self._client.seed_answer(
                    task_id, question_id=question_id, answers=answers
                )
            )
            return
        self._client.answer(task_id, question_id=question_id, answers=answers)

    # -- background drive machinery -----------------------------------------

    def _spawn_drive(self, seeded: Any) -> None:
        """Run ``Client.drive_seeded`` on a daemon thread (the SdkHost
        background-exit idiom). Faults are self-contained: the driver fails
        the lease / converges a lost-lease terminal, and the durable fault is
        visible on the stream — here we only log."""

        def _run() -> None:
            try:
                self._client.drive_seeded(seeded)
            except Exception:  # noqa: BLE001 — the stream carries the fault
                _log.exception(
                    "background drive failed for task %s", seeded.task_id
                )
            finally:
                # ``_drives`` is keyed by task_id, so a fast follow-up command
                # for the SAME task can register its own thread in the tiny
                # window before this ``finally`` runs. Evict only OUR entry —
                # a blind ``pop(task_id)`` would drop the successor's thread and
                # ``join_drives`` (shutdown) would close the store under it.
                with self._drives_lock:
                    if self._drives.get(seeded.task_id) is thread:
                        del self._drives[seeded.task_id]

        thread = threading.Thread(
            target=_run,
            name=f"noeta-agent-drive-{seeded.task_id}",
            daemon=True,
        )
        with self._drives_lock:
            self._drives[seeded.task_id] = thread
        thread.start()

    def join_drives(self, timeout: Optional[float] = None) -> bool:
        """Wait for in-flight background drives (tests / graceful shutdown).

        Returns True when none remain within ``timeout`` (``None`` = wait
        indefinitely)."""
        import time as _time

        deadline = None if timeout is None else _time.monotonic() + timeout
        while True:
            with self._drives_lock:
                threads = list(self._drives.values())
            if not threads:
                return True
            for thread in threads:
                remaining = (
                    None if deadline is None else deadline - _time.monotonic()
                )
                if remaining is not None and remaining <= 0:
                    return False
                thread.join(timeout=remaining)

    def cancel(
        self, task_id: str, *, reason: str = "cancelled", cascade: bool = False
    ) -> None:
        self._client.cancel(task_id, reason=reason, cascade=cascade)

    def close(self, task_id: str, *, reason: Optional[str] = None) -> None:
        self._client.close(task_id, reason=reason)

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

    # -- shutdown ----------------------------------------------------------

    def shutdown(self) -> None:
        # Give in-flight background drives a bounded window to settle before
        # the client (and any injected durable storage) closes under them.
        if not self.join_drives(timeout=10.0):
            _log.warning(
                "engine_room shutdown: background drives still in flight "
                "after grace; closing anyway (recovery via requeue_stale)"
            )
        self._client.shutdown()
