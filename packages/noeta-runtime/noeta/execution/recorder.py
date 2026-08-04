"""The kernel's concrete session recorder: it routes a plugin's resident-content
activation through the host-owned ``event_log`` + ``content_store`` in the
pre-loop seed window.

The envelope carries ``actor="plugin:<name>"`` purely as a provenance label —
the recorder remains the sole component calling ``event_log.emit``, so the
single-writer invariant holds and ``fold(events) → state`` cannot fork (fold
never reads ``actor``). The active-content gate is what makes rerunning the
hooks on resume append nothing when nothing changed.
"""

from __future__ import annotations

from typing import Iterable, Optional

from noeta.core.fold import apply_event
from noeta.execution.session_pack import InitHook
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import ContextContentRecordedPayload
from noeta.protocols.task import Task
from noeta.protocols.values import ContentRef


__all__ = ["SeedRecorder", "run_content_init"]


class SeedRecorder:
    """Concrete :class:`~noeta.execution.session_pack.SessionRecorder` bound to
    one task's seed window.

    Threads the functionally-returned task internally, so a caller reads the
    folded result back through :attr:`task` after every hook has run.
    """

    def __init__(
        self,
        event_log: EventLogWriter,
        content_store: ContentStore,
        task: Task,
        *,
        actor: str = "engine",
        lease_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._task = task
        self._actor = actor
        self._lease_id = lease_id
        self._trace_id = trace_id

    @property
    def task(self) -> Task:
        """The task after every recording this recorder has applied."""
        return self._task

    def record_content(
        self,
        *,
        kind: str,
        name: str,
        version: str,
        ref: ContentRef,
        policy: str,
    ) -> None:
        """Activate the ``(kind, name)`` resident at ``ref``'s bytes.

        No-ops on an empty triple or when ``(kind, name)`` is already active
        with this exact hash; a new hash records a refresh (hash
        last-write-wins). This gate is what lets the callers rerun ``init``
        freely: it runs at task seed, at a subtask drain, and again whenever a
        new goal is seeded onto a resumed task, so an unchanged source appends
        nothing and a changed one records exactly one refresh.
        """
        if not kind or not name or not ref.hash:
            return
        if self._task.state.active_content.get(kind, {}).get(name) == ref.hash:
            return
        env = self._event_log.emit(
            task_id=self._task.task_id,
            type="ContextContentRecorded",
            payload=ContextContentRecordedPayload(
                kind=kind,
                name=name,
                version=version,
                content_hash=ref.hash,
                policy=policy,
            ),
            actor=self._actor,
            lease_id=self._lease_id,
            trace_id=self._trace_id,
        )
        apply_event(self._task, env, self._content_store)


def run_content_init(
    event_log: EventLogWriter,
    content_store: ContentStore,
    task: Task,
    *,
    init_hooks: Iterable[tuple[str, InitHook]],
    lease_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Task:
    """Run each contributed ``init`` hook against a plugin-bound recorder.

    ``init_hooks`` arrives priority-ordered and each hook records its residents
    in that order, so the resulting event order is a function of the pack bands
    alone. An empty tuple leaves the ledger untouched.
    """
    for name, hook in init_hooks:
        recorder = SeedRecorder(
            event_log,
            content_store,
            task,
            actor=f"plugin:{name}",
            lease_id=lease_id,
            trace_id=trace_id,
        )
        hook(recorder)
        task = recorder.task
    return task
