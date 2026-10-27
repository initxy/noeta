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

from noeta.events import Event, InboxItem, InboxKind, Recorded, TaskCreated
from noeta.types import Struct

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
    ) -> int:
        """Append ``events`` after ``expected_seq`` and apply ``patch``,
        ``posts`` and ``creates`` atomically. Returns the new last seq. Raises
        ``noeta.errors.Conflict`` when the claim's epoch or the expected seq
        no longer holds, or the task is not running."""
        ...

    async def read(self, task_id: str, after: int = 0) -> list[Recorded]: ...

    async def post(self, post: Post) -> tuple[int, bool]:
        """Add to the task's inbox; returns ``(inbox_seq, created)``. A known
        ``dedupe_key`` returns the first post's seq and ``False``. Raises
        ``noeta.errors.UnknownTask``."""
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
        self, worker: str, queues: Sequence[str], lease_seconds: float
    ) -> Claim | None:
        """Take one claimable task: ``ready`` (and due), ``waiting`` with a due
        ``ready_at``, or ``running`` with an expired lease. Bumps the epoch."""
        ...

    async def renew(self, claim: Claim, lease_seconds: float) -> Claim | None:
        """Extend the lease; ``None`` when the claim was lost."""
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
