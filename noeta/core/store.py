"""The store interface: task logs, inboxes, the task table, and claims.

Correctness rests on two rules every implementation enforces in one
transaction:

* ``append`` succeeds only for the claim's epoch and the expected last seq, so
  a task's log has one writer however many runners think they own it.
* ``post`` is idempotent on ``dedupe_key`` and wakes a waiting task, and a
  runner that goes to wait while unread inbox items exist is made ready
  instead — so no message is ever left unseen.

Leases (``lease_until``) only decide when a silent runner's task may be
claimed by someone else. A wrong guess costs a retry, never a double write.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, Protocol

from msgspec.structs import replace

from noeta.core.events import Event, InboxItem, InboxKind, Recorded, TaskCreated, TaskEnded, Waiting
from noeta.core.types import Struct

Status = Literal["ready", "running", "waiting", "ended"]


class TaskRow(Struct):
    task_id: str
    root_id: str
    agent: str
    queue: str
    status: Status
    last_seq: int
    created_at: float
    updated_at: float
    parent_id: str | None = None
    waiting_on: str | None = None
    ready_at: float | None = None
    owner: str | None = None
    owner_epoch: int = 0
    lease_until: float | None = None
    inbox_seq: int = 0
    inbox_read_seq: int = 0
    end_status: str | None = None


class Claim(Struct):
    task_id: str
    worker: str
    epoch: int
    lease_until: float


class Patch(Struct):
    """How the task row changes with an append. ``running`` keeps the claim;
    ``waiting`` / ``ended`` release it in the same transaction."""

    status: Literal["running", "waiting", "ended"] = "running"
    waiting_on: str | None = None
    #: For ``waiting``: when the task becomes claimable on its own (a timer).
    ready_at: float | None = None
    inbox_read_seq: int | None = None
    end_status: str | None = None


def patch_for(events: Sequence[Event], inbox_read: int) -> Patch:
    """The row change ``events`` imply: a ``Waiting`` makes the task wait on
    what it names until its ``until``, a ``TaskEnded`` ends it, anything else
    keeps it running. A releasing change records ``inbox_read`` as read."""
    for event in reversed(events):
        if isinstance(event, TaskEnded):
            return Patch(status="ended", end_status=event.status, inbox_read_seq=inbox_read)
        if isinstance(event, Waiting):
            return Patch(status="waiting", waiting_on=event.on, ready_at=event.until,
                         inbox_read_seq=inbox_read)
    return Patch()


def next_row_state(patch: Patch, inbox_seq: int,
                   read_seq: int) -> tuple[Status, str | None, float | None, int]:
    """The row's ``(status, waiting_on, ready_at, inbox_read_seq)`` after an
    append with ``patch``, given its current ``inbox_seq`` and
    ``inbox_read_seq``: a task going to wait while unread inbox items exist
    is made ready instead, so no message is left unseen."""
    read = patch.inbox_read_seq if patch.inbox_read_seq is not None else read_seq
    status: Status = patch.status
    ready_at = patch.ready_at
    if status == "waiting" and inbox_seq > read:
        status, ready_at = "ready", None
    return status, patch.waiting_on if status == "waiting" else None, ready_at, read


class Appended(Struct):
    """What an append wrote: the log's new last seq and the time the store
    stamped on its events (``Recorded.at``)."""

    seq: int
    at: float


class Post(Struct):
    task_id: str
    body: InboxKind
    dedupe_key: str | None = None


class NewTask(Struct):
    task_id: str
    created: TaskCreated
    queue: str = "default"
    #: A second ``create`` with the same key returns the first task's id.
    dedupe_key: str | None = None
    #: Items to put in the new task's inbox in the same transaction.
    inbox: list[InboxKind] = []


ChangeKind = Literal["appended", "posted", "created"]
OnChange = Callable[[str, ChangeKind], None]


class Store(Protocol):
    async def create(self, task: NewTask) -> tuple[str, bool]:
        """Insert the task (status ``ready``) with ``TaskCreated`` at seq 1.
        Returns ``(task_id, created)``; with a known ``dedupe_key`` it returns
        the existing task's id and ``False``."""
        ...

    async def append(
        self,
        claim: Claim,
        events: Sequence[Event],
        *,
        expected_seq: int,
        patch: Patch = Patch(),
        posts: Sequence[Post] = (),
        creates: Sequence[NewTask] = (),
    ) -> Appended:
        """Append ``events`` after ``expected_seq`` and apply ``patch``,
        ``posts`` and ``creates`` atomically. Returns the new last seq and
        the time stamped on the events (what :meth:`read` returns). Raises
        ``noeta.core.errors.Conflict`` when the claim's epoch or the expected seq
        no longer holds, or the task is not running. A post to a task that
        does not exist is skipped (the rest still lands)."""
        ...

    async def read(self, task_id: str, after: int = 0) -> list[Recorded]: ...

    async def post(self, post: Post) -> tuple[int, bool]:
        """Add to the task's inbox; returns ``(inbox_seq, created)``. A known
        ``dedupe_key`` returns the first post's seq and ``False``. Raises
        ``noeta.core.errors.UnknownTask``."""
        ...

    async def inbox(self, task_id: str, after: int = 0) -> list[InboxItem]: ...

    async def get(self, task_id: str) -> TaskRow | None: ...

    async def tasks(
        self,
        *,
        parent_id: str | None = None,
        status: Status | None = None,
        limit: int = 100,
    ) -> list[TaskRow]: ...

    async def claim(
        self, worker: str, queues: Sequence[str], lease_seconds: float,
        *, roots: Sequence[str] | None = None,
    ) -> Claim | None:
        """Take one claimable task: ``ready`` (and due), ``waiting`` with a due
        ``ready_at``, or ``running`` with an expired lease, of a tree no other
        worker holds (:meth:`hold`). Bumps the epoch.
        With ``roots``, only tasks whose ``root_id`` is one of them (a front
        end serving its own task trees); ``None`` takes any task."""
        ...

    async def renew(self, claim: Claim, lease_seconds: float) -> Claim | None:
        """Extend the lease; ``None`` when the claim was lost."""
        ...

    async def hold(self, root_id: str, worker: str, seconds: float) -> bool:
        """Keep the task tree of ``root_id`` with ``worker`` for ``seconds``:
        until then ``claim`` by any other worker skips every task of the
        tree. Renewing is holding again. ``False`` (nothing changed) while
        another worker's hold on the tree has not run out."""
        ...

    async def unhold(self, root_id: str, worker: str) -> None:
        """Drop ``worker``'s hold on the tree; a no-op without one."""
        ...

    async def release(self, claim: Claim) -> None:
        """Give a running task back (``ready``) without ending it, e.g. on
        shutdown. A no-op when the claim was already lost."""
        ...

    def on_change(self, callback: OnChange) -> Callable[[], None]:
        """Call ``callback(task_id, kind)`` after each committed change this
        process makes (and, for shared backends, changes others make). Returns
        an unsubscribe function."""
        ...

    async def close(self) -> None: ...


async def pin_history(store: Store, created: TaskCreated) -> TaskCreated:
    """``created`` with its continuation pinned: a ``history_from`` without
    ``history_through`` ends at its source's last event as of now, so the
    source growing later never changes what the new task's log folds to.
    An unknown source is left as given (it folds to no history)."""
    if created.history_from is None or created.history_through is not None:
        return created
    row = await store.get(created.history_from)
    return created if row is None else replace(created, history_through=row.last_seq)
