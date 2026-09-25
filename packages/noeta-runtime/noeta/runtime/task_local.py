"""Task-local runtime state: what a task accumulates in this process.

The Engine is a per-turn value (``noeta.execution.resolver``), so nothing on
it outlives the turn. What a task needs across turns and still never records
— the files the model has read (the edit tools' read-first precondition),
the ReAct compaction-trigger calibration, the last MCP provenance the host
emitted, the skill roster the task last saw — lives here instead, keyed by
task id, in ONE host-owned registry with one lock, one cap and one
``forget``: the driver's conversation-end verbs (``cancel`` / ``close``)
call it through ``forget_turn_carriers``, and an LRU bounds a process that
never closes anything. A runtime accelerator like
:class:`~noeta.runtime.file_checkpoint.FileCheckpointRegistry`: never
written to the event log, never resumed from, so a restart simply starts
every task's local state empty.

Two kinds of entry:

* the **read registry** — the runtime's own
  :class:`~noeta.runtime.tool.InMemoryFileReadRegistry`, typed because the
  runtime owns both ends (the host threads it into the turn's
  ``ToolRuntime``);
* **named slots** — opaque objects a built-in keeps for the task
  (``slot(task_id, name, factory)`` get-or-creates one; ``peek`` reads
  without creating). A pack reaches its slot through the
  ``task_slot`` the host binds into its ``plugin_config`` entry — a
  :data:`TaskSlot` callable already bound to the build's task — so the
  runtime never names a built-in's type.

The registry also holds the task's **turn Engine**: ``resolve_engine`` keeps
the Engine it built for the turn here and hands it back to every resume of
that turn (approval, answer, sub-agent return); the driver drops it when a
new user goal opens a turn, the worker when the turn settles (terminal, or
parked on the next-goal handle), and ``forget`` with everything else.

Keyed by ``task_id``, not the root task: a sub-agent's reads are not its
parent's, and its calibration is its own conversation's.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable, Optional, TypeVar

from noeta.runtime.tool import InMemoryFileReadRegistry


__all__ = [
    "DEFAULT_MAX_TASK_LOCALS",
    "TaskLocalRegistry",
    "TaskSlot",
]

T = TypeVar("T")

#: A get-or-create over one task's named slots, already bound to the task:
#: ``task_slot(name, factory) -> the slot's object``. The shape a pack
#: receives as ``plugin_config[<plugin>]["task_slot"]``.
TaskSlot = Callable[[str, Callable[[], Any]], Any]

#: Tasks tracked before the least recently touched is forgotten. A live
#: conversation is touched every turn, so eviction reaches only tasks the
#: host never closed.
DEFAULT_MAX_TASK_LOCALS: int = 4096


class _TaskLocal:
    __slots__ = ("reads", "slots", "engine")

    def __init__(self) -> None:
        self.reads = InMemoryFileReadRegistry()
        self.slots: dict[str, Any] = {}
        self.engine: Any = None


class TaskLocalRegistry:
    """See the module docstring. Thread-safe on its table; a task's own
    objects are touched by that task's one driving thread at a time (turns
    are serial under the dispatcher lease), so they carry no lock of their
    own unless a built-in needs one."""

    def __init__(self, *, max_tasks: int = DEFAULT_MAX_TASK_LOCALS) -> None:
        self._max = max_tasks
        self._tasks: OrderedDict[str, _TaskLocal] = OrderedDict()
        self._lock = threading.Lock()

    # -- table ----------------------------------------------------------

    def _touch(self, task_id: str) -> _TaskLocal:
        """Get-or-create ``task_id``'s record, freshest; evict past the cap.
        Caller holds the lock."""
        key = str(task_id)
        local = self._tasks.get(key)
        if local is None:
            local = _TaskLocal()
            self._tasks[key] = local
        else:
            self._tasks.move_to_end(key)
        while len(self._tasks) > self._max:
            self._tasks.popitem(last=False)
        return local

    def forget(self, task_id: str) -> None:
        """Drop everything kept for ``task_id`` (conversation end). Idempotent."""
        with self._lock:
            self._tasks.pop(str(task_id), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)

    def __contains__(self, task_id: object) -> bool:
        with self._lock:
            return str(task_id) in self._tasks

    # -- the read registry ----------------------------------------------

    def read_registry(self, task_id: str) -> InMemoryFileReadRegistry:
        """The files ``task_id`` has read this process — the edit tools'
        read-first record, threaded into each turn's ``ToolRuntime``."""
        with self._lock:
            return self._touch(task_id).reads

    # -- named slots ----------------------------------------------------

    def slot(self, task_id: str, name: str, factory: Callable[[], T]) -> T:
        """``task_id``'s ``name`` slot, created by ``factory`` on first use."""
        with self._lock:
            slots = self._touch(task_id).slots
            found = slots.get(name)
            if found is None:
                found = factory()
                slots[name] = found
            return found

    def peek(self, task_id: str, name: str) -> Optional[Any]:
        """``task_id``'s ``name`` slot, or ``None`` when never created —
        never creates and never refreshes the LRU."""
        with self._lock:
            local = self._tasks.get(str(task_id))
            if local is None:
                return None
            return local.slots.get(name)

    def bind_slot(self, task_id: str) -> TaskSlot:
        """:meth:`slot` bound to ``task_id`` — the callable a build hands
        its packs as ``plugin_config[<plugin>]["task_slot"]``."""
        tid = str(task_id)

        def task_slot(name: str, factory: Callable[[], Any]) -> Any:
            return self.slot(tid, name, factory)

        return task_slot

    # -- the turn Engine ------------------------------------------------

    def held_engine(self, task_id: str) -> Optional[Any]:
        """The Engine kept for ``task_id``'s current turn, or ``None``."""
        with self._lock:
            local = self._tasks.get(str(task_id))
            return None if local is None else local.engine

    def hold_engine(self, task_id: str, engine: Any) -> None:
        """Keep ``engine`` for ``task_id``'s current turn."""
        with self._lock:
            self._touch(task_id).engine = engine

    def drop_engine(self, task_id: str) -> None:
        """Let ``task_id``'s turn Engine go (turn settled / a new turn
        opens); the rest of the task's local state stays. Idempotent."""
        with self._lock:
            local = self._tasks.get(str(task_id))
            if local is not None:
                local.engine = None
