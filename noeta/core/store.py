"""The store interface: task logs, inboxes, the task table, and claims.

Correctness rests on two rules every implementation enforces in one
transaction:

* ``append`` succeeds only for the claim's epoch and the expected last seq, so
  a task's log has one writer however many runners think they own it.
* ``post`` is idempotent on ``dedupe_key`` and wakes a waiting task (unless
  ``Post.wake`` is off: then it is read when something else wakes it), and
  a runner that goes to wait while unread inbox items exist is made ready
  instead, and one that ends over them is refused (``InboxUnread``) — so no
  message is ever left unseen.

Leases (``lease_until``) only decide when a silent runner's task may be
claimed by someone else. A wrong guess costs a retry, never a double write.

Holds (:meth:`Store.hold`) keep a task tree with the worker that runs
something only it has (a background job). A tree has at most one holding
worker, and a hold is refused while another worker runs a task of the tree
(an unexpired lease), checked in the hold's transaction; a claim and a hold
of one tree serialize. So a held tree's tasks run only on its holder, which
has every job of the tree. A hold carries an epoch: renewing
needs it, so a hold that was reaped stays lost to its old holder. A hold's
time running out only makes it reapable; :meth:`Store.reap_holds` releases
it and tells the tree's tasks (``hold_lost``) in one transaction, so no task
of the tree is claimed by another worker before that notice is in its inbox.

Optional members. Tenant budgets: ``async add_tenant_usage(tenant, period,
tokens, cost_usd)`` adds to a tenant's count for a period (``YYYY-MM``) and
``async tenant_usage(tenant, period) -> (tokens, cost_usd)`` reads it. For a
store shared by processes on many machines:
``async publish_deltas(deltas)`` sends live model output (``(task_id,
Delta)`` pairs) to the other processes, and ``on_delta(callback)`` hears it
(returning the unsubscribe function). Best effort and never stored; a store
without them keeps live output in its process.

Snapshots (optional members ``snapshot`` / ``put_snapshot``, see
:class:`Snapshot`) keep a folded state per task and fold version so a claim
need not fold the whole log; a store without them folds from the start.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Protocol

from msgspec.structs import replace

from noeta.core.errors import InboxUnread
from noeta.core.events import (
    DEFAULT_TENANT,
    Event,
    External,
    InboxItem,
    InboxKind,
    Recorded,
    TaskCreated,
    TaskEnded,
    Waiting,
)
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
    #: Whose task it is (``TaskCreated.tenant``).
    tenant: str = DEFAULT_TENANT


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
    is made ready instead, and one ending over them raises ``InboxUnread``
    (posts in the same append are not counted), so no message is left
    unseen."""
    read = patch.inbox_read_seq if patch.inbox_read_seq is not None else read_seq
    status: Status = patch.status
    if status == "ended" and inbox_seq > read:
        raise InboxUnread(f"inbox at {inbox_seq}, read through {read}")
    ready_at = patch.ready_at
    if status == "waiting" and inbox_seq > read:
        status, ready_at = "ready", None
    return status, patch.waiting_on if status == "waiting" else None, ready_at, read


class Appended(Struct):
    """What an append wrote: the log's new last seq and the time the store
    stamped on its events (``Recorded.at``)."""

    seq: int
    at: float


#: Snapshots a store keeps per task, each of another fold version.
SNAPSHOT_VERSIONS = 2


class Snapshot(Struct):
    """A task's folded state as of log seq ``seq`` (``noeta.core.snapshots``),
    used only by a runner whose fold ``version`` is the same.

    Optional store members, read with ``getattr``:

    * ``async snapshot(task_id, version=None) -> Snapshot | None`` — the
      task's snapshot of ``version``; without one, its newest of any.
    * ``async put_snapshot(task_id, snapshot) -> None`` — keep it unless the
      task's stored one of the same version is at a later seq, or the task
      has ended. One is kept per version, so workers of two fold versions
      (a rolling upgrade) do not replace each other's; past
      :data:`SNAPSHOT_VERSIONS` the one at the lowest seq goes. Needs no
      claim: a snapshot is a fold of a log prefix, which never changes.

    The append that ends a task deletes its snapshots in the same
    transaction: an ended task is never claimed again.
    """

    seq: int
    version: str
    data: bytes


class Post(Struct):
    task_id: str
    body: InboxKind
    dedupe_key: str | None = None
    #: Make a waiting task ready. Off: the item waits in the inbox for the
    #: task's next wake (news that needs no turn of its own).
    wake: bool = True
    #: When the task has ended (so it reads nothing more), post to its root
    #: instead, decided under the task's row lock: no end can slip between
    #: the check and the post, and the item reaches whoever settles for an
    #: ended task (a background job's end, for one). Same dedupe key.
    forward_if_ended: bool = False


def hold_lost(task_id: str, worker: str, epoch: int, *, wake: bool = True) -> Post:
    """The post that tells a task of the tree that hold ``epoch`` of
    ``worker`` is gone (``wake``: see :class:`Post`)."""
    return Post(task_id=task_id, body=External(name=HOLD_LOST,
                                               data={"worker": worker, "epoch": epoch}),
                dedupe_key=f"{HOLD_LOST}:{epoch}", wake=wake)


class NewTask(Struct):
    task_id: str
    created: TaskCreated
    queue: str = "default"
    #: A second ``create`` with the same key returns the first task's id.
    dedupe_key: str | None = None
    #: Items to put in the new task's inbox in the same transaction.
    inbox: list[InboxKind] = []


#: The ``External`` a reaped (or abandoned) hold posts to each unended task of
#: its tree: ``data = {"worker": ..., "epoch": ...}``, the hold that is gone
#: and with it what that worker ran for the tree.
HOLD_LOST = "hold_lost"

#: ``released``: a claim or hold was given back, so the task (tree) may be
#: claimable again.
ChangeKind = Literal["appended", "posted", "created", "released"]
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
        no longer holds, or the task is not running; ``InboxUnread`` (a
        ``Conflict``) when the append ends the task and its inbox holds items
        past ``patch.inbox_read_seq`` (see :func:`next_row_state`). A post to a task that
        does not exist is skipped (the rest still lands)."""
        ...

    async def read(self, task_id: str, after: int = 0) -> list[Recorded]: ...

    async def post(self, post: Post) -> tuple[int, bool]:
        """Add to the task's inbox (its root's, for ``forward_if_ended``
        when it has ended); returns ``(inbox_seq, created)`` of the inbox it
        went to. A known ``dedupe_key`` returns the first post's seq and
        ``False``. Raises ``noeta.core.errors.UnknownTask``."""
        ...

    async def inbox(self, task_id: str, after: int = 0) -> list[InboxItem]: ...

    async def get(self, task_id: str) -> TaskRow | None: ...

    async def tasks(
        self,
        *,
        parent_id: str | None = None,
        status: Status | None = None,
        limit: int = 100,
        tenant: str | None = None,
    ) -> list[TaskRow]:
        """The newest rows first; ``tenant`` keeps only that tenant's."""
        ...

    async def claim(
        self, worker: str, queues: Sequence[str], lease_seconds: float,
        *, roots: Sequence[str] | None = None,
        tenant_limits: Mapping[str, int] | None = None,
    ) -> Claim | None:
        """Take one claimable task: ``ready`` (and due), ``waiting`` with a due
        ``ready_at``, or ``running`` with an expired lease, of a tree no other
        worker holds (:meth:`hold`). Bumps the epoch.
        With ``roots``, only tasks whose ``root_id`` is one of them (a front
        end serving its own task trees); ``None`` takes any task.
        ``tenant_limits`` (tenant -> most tasks running at once): a task of a
        tenant that many tasks of already run stays where it is; the count
        holds across workers claiming at the same time."""
        ...

    async def renew(self, claim: Claim, lease_seconds: float) -> Claim | None:
        """Extend the lease; ``None`` when the claim was lost."""
        ...

    async def hold(self, root_id: str, worker: str, seconds: float,
                   *, epoch: int | None = None) -> int | None:
        """Keep the task tree of ``root_id`` with ``worker``: until the hold
        is released or reaped, ``claim`` by any other worker skips every task
        of the tree. ``seconds`` is how long it lasts without renewal before
        it may be reaped.

        Without ``epoch`` it takes the hold (or extends the one ``worker``
        already has) and returns its epoch, never reused by another hold;
        ``None`` when another worker holds the tree (one whose hold ran out
        counts until it is reaped): a tree has at most one holding worker,
        decided in the same transaction. With ``epoch`` it renews that hold:
        ``None`` when it is gone (released, or reaped while its worker was
        silent)."""
        ...

    async def unhold(self, root_id: str, worker: str, epoch: int,
                     *, lost: bool = False) -> None:
        """Release that hold; a no-op once it is gone. ``lost``: what it
        held is gone too (its worker is stopping on purpose), so
        ``hold_lost`` is posted as :meth:`reap_holds` does, but without
        waking a waiting task: the task hears of it with whatever wakes it
        next (as Claude Code, closed, kills its background shells and says
        nothing until the person writes again)."""
        ...

    async def reap_holds(self) -> int:
        """Release every hold whose time ran out (its worker died, froze or
        lost the store) and, in the same transaction, post
        ``External(HOLD_LOST, {"worker", "epoch"})`` to each unended task of
        its tree (dedupe key ``hold_lost:<epoch>``), which wakes a waiting
        one. Returns how many holds were reaped. Every worker runs it."""
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
