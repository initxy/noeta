"""The scoped session recorder — the one write verb a plugin's ``init`` hook
gets (spec §4.4/§4.5).

:class:`SeedRecorder` is the kernel's concrete :class:`SessionRecorder`: it
routes a resident-content activation through the host-owned ``event_log`` +
``content_store`` in the pre-loop seed window, using the identical recipe the
retired per-feature seed recorders used (no-op guard → first-only gate against
``TaskState.active_content`` → one ``ContextContentRecorded`` → ``apply_event``)
— so the emitted envelope shape (``actor="engine"`` / ``origin="engine"``) and
its bytes are unchanged. The single physical writer stays the kernel: a plugin
never touches a raw ``EventLog``, only this verb (single-writer-invariant).

:func:`run_content_init` runs every contributed ``init`` hook against one
recorder and returns the folded task. Each hook is idempotent per drive: the
first-only gate drops a re-record of an already-active ``(kind, name)``, so
running the hooks again (resume) appends nothing when nothing changed.
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

    Threads the functionally-returned task internally so a caller reads the
    folded result back through :attr:`task` after every hook has run.
    """

    def __init__(
        self,
        event_log: EventLogWriter,
        content_store: ContentStore,
        task: Task,
        *,
        lease_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._task = task
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

        No-ops on an empty triple or an already-active ``(kind, name)`` (the
        first-only gate — the hash-last-write-wins refresh is a later slice);
        otherwise emits one ``ContextContentRecorded`` and folds it in.
        """
        if not kind or not name or not ref.hash:
            return
        if name in self._task.state.active_content.get(kind, ()):
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
            lease_id=self._lease_id,
            trace_id=self._trace_id,
        )
        apply_event(self._task, env, self._content_store)


def run_content_init(
    event_log: EventLogWriter,
    content_store: ContentStore,
    task: Task,
    *,
    init_hooks: Iterable[InitHook],
    lease_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Task:
    """Run each contributed ``init`` hook against one recorder; return the task.

    The generic successor of the three hand-rolled seed-recorder calls: the
    driver hands the folded, priority-ordered hook tuple and the hooks record
    their residents in that order. An empty tuple leaves the ledger untouched.
    """
    recorder = SeedRecorder(
        event_log, content_store, task, lease_id=lease_id, trace_id=trace_id
    )
    for hook in init_hooks:
        hook(recorder)
    return recorder.task
