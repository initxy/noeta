"""The scoped session recorder — the one write verb a plugin's ``init`` hook
gets (spec §4.4/§4.5).

:class:`SeedRecorder` is the kernel's concrete :class:`SessionRecorder`: it
routes a resident-content activation through the host-owned ``event_log`` +
``content_store`` in the pre-loop seed window (no-op guard → first-only gate
against ``TaskState.active_content`` → one ``ContextContentRecorded`` →
``apply_event``). The kernel stamps the envelope with ``actor="plugin:<name>"``
(spec §4.4) so audit attributes the resident to the plugin that produced it —
the same provenance-label use the ``mcp`` / ``llm`` / ``compaction`` actors
already make of the field. This is NOT a second physical writer: the kernel's
recorder is the sole component calling ``event_log.emit`` (a plugin only calls
``record_content``), so ``fold(events) → state`` still cannot fork
(single-writer-invariant — which is about the physical writer, never the
provenance label; fold does not read ``actor``).

:func:`run_content_init` runs each contributed ``init`` hook against a recorder
bound to that hook's plugin name and returns the folded task. Each hook is
idempotent per drive: the first-only gate drops a re-record of an already-active
``(kind, name)``, so running the hooks again (resume) appends nothing when
nothing changed.
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
        with this exact hash (spec §4.4); a new hash records a refresh (hash
        last-write-wins, spec §3). ``init`` reruns every drive, so an unchanged
        store appends nothing and a changed store records exactly one refresh.
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

    The generic successor of the three hand-rolled seed-recorder calls: the
    driver hands the folded, priority-ordered ``(plugin, hook)`` tuple and each
    hook records its residents in that order through a recorder that stamps
    ``actor="plugin:<name>"`` (spec §4.4). An empty tuple leaves the ledger
    untouched.
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
