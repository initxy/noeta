"""The store contract: what every ``Store`` must do, as tests. A store
package runs them by defining a ``store`` fixture that yields a fresh, empty
store and importing the tests next to it::

    from noeta.testing.store_contract import *  # noqa: F403

    @pytest.fixture
    async def store(tmp_path):
        s = MyStore(...)
        yield s
        await s.close()

Needs pytest and pytest-asyncio (``asyncio_mode = "auto"``)."""

from __future__ import annotations

import asyncio

import pytest

from noeta.core.errors import Conflict, InboxUnread, UnknownTask
from noeta.core.events import Cancel, ExtensionRecorded, Say, TaskCreated, TaskEnded, Waiting
from noeta.core.store import (
    SNAPSHOT_VERSIONS,
    NewTask,
    Patch,
    Post,
    Snapshot,
    Store,
)
from noeta.core.types import user


def new(task_id: str = "t1", **kw: object) -> NewTask:
    return NewTask(task_id=task_id, created=TaskCreated(agent="a"), **kw)  # type: ignore[arg-type]


def _record() -> ExtensionRecorded:
    return ExtensionRecorded(extension="x", kind="k", data={})


def say(text: str) -> Say:
    return Say(message=user(text))


async def test_create_writes_task_created_and_is_claimable(store: Store) -> None:
    assert await store.create(new()) == ("t1", True)
    row = await store.get("t1")
    assert row is not None and row.status == "ready" and row.last_seq == 1 and row.root_id == "t1"
    [rec] = await store.read("t1")
    assert isinstance(rec.event, TaskCreated) and rec.seq == 1
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None and claim.task_id == "t1" and claim.epoch == 1
    assert await store.claim("w2", ["default"], 30) is None


async def test_lists_filter_by_the_claims_owner(store: Store) -> None:
    await store.create(new("a"))
    await store.create(new("b"))
    first = await store.claim("w1", ["default"], 30)
    second = await store.claim("w2", ["default"], 30)
    assert first is not None and second is not None
    [mine] = await store.tasks(owner="w1", status="running")
    assert (mine.task_id, mine.owner_epoch) == (first.task_id, first.epoch)
    assert await store.tasks(owner="w3") == []


async def test_lists_only_the_roots_however_many_children_are_newer(store: Store) -> None:
    await store.create(new("old"))
    await store.create(new("root"))
    for n in range(3):
        await store.create(NewTask(task_id=f"root.{n}", created=TaskCreated(
            agent="a", parent_id="root", root_id="root")))
    assert [r.task_id for r in await store.tasks(limit=2)] == ["root.2", "root.1"]
    assert [r.task_id for r in await store.tasks(roots_only=True, limit=2)] == ["root", "old"]
    assert await store.tasks(roots_only=True, parent_id="root") == []


async def test_create_dedupe_key_returns_first_task(store: Store) -> None:
    assert await store.create(new("a", dedupe_key="k")) == ("a", True)
    assert await store.create(new("b", dedupe_key="k")) == ("a", False)
    assert await store.get("b") is None


async def test_create_with_a_known_task_id_returns_it_unchanged(store: Store) -> None:
    """Creating is idempotent on the task id, also across racing processes
    (a channel on several replicas creating one conversation's task)."""
    results = await asyncio.gather(*(store.create(new("t1", inbox=[say(f"m{i}")]))
                                     for i in range(4)))
    assert sorted(results) == [("t1", False)] * 3 + [("t1", True)]
    assert len(await store.read("t1")) == 1
    assert len(await store.inbox("t1", after=0)) == 1  # only the creator's input


async def test_append_requires_epoch_and_expected_seq(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    ended = TaskEnded(status="done")
    with pytest.raises(Conflict):
        await store.append(claim, [ended], expected_seq=5)
    assert (await store.append(claim, [Waiting(on="input")], expected_seq=1)).seq == 2
    with pytest.raises(Conflict):
        await store.append(claim, [ended], expected_seq=1)


async def test_append_returns_the_time_it_stamped(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    done = await store.append(claim, [Waiting(on="input")], expected_seq=1)
    assert [(r.seq, r.at) for r in await store.read("t1", after=1)] == [(2, done.at)]


async def test_a_read_with_a_limit_is_the_head_of_the_log(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [_record() for _ in range(4)], expected_seq=1)
    assert [r.seq for r in await store.read("t1", limit=2)] == [1, 2]
    assert [r.seq for r in await store.read("t1", after=2, limit=2)] == [3, 4]
    assert [r.seq for r in await store.read("t1", after=4, limit=9)] == [5]
    assert len(await store.read("t1")) == 5


async def test_an_append_of_no_events_applies_its_patch(store: Store) -> None:
    """A task handed back still waiting writes no event."""
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    done = await store.append(claim, [], expected_seq=1,
                              patch=Patch(status="waiting", waiting_on="approval"))
    row = await store.get("t1")
    assert done.seq == 1 and row is not None and row.last_seq == 1
    assert (row.status, row.waiting_on, row.owner) == ("waiting", "approval", None)
    assert len(await store.read("t1")) == 1


async def test_expired_lease_is_reclaimed_and_old_owner_loses(store: Store) -> None:
    await store.create(new())
    old = await store.claim("w1", ["default"], -1)  # already expired
    assert old is not None
    fresh = await store.claim("w2", ["default"], 30)
    assert fresh is not None and fresh.epoch == old.epoch + 1
    with pytest.raises(Conflict):
        await store.append(old, [Waiting(on="input")], expected_seq=1)
    assert await store.renew(old, 30) is None
    assert (await store.append(fresh, [Waiting(on="input")], expected_seq=1)).seq == 2


async def test_a_claim_says_whether_the_one_before_was_given_back(store: Store) -> None:
    await store.create(new())
    first = await store.claim("w1", ["default"], -1)  # its worker dies
    assert first is not None and not first.reclaimed
    taken = await store.claim("w2", ["default"], 30)
    assert taken is not None and taken.reclaimed
    await store.release(taken)  # a worker shutting down
    again = await store.claim("w3", ["default"], 30)
    assert again is not None and not again.reclaimed
    await store.append(again, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input"))
    await store.post(Post(task_id="t1", body=say("hi")))
    woken = await store.claim("w4", ["default"], 0.05)
    assert woken is not None and not woken.reclaimed
    # A kept wait whose worker went silent was never given back either.
    await store.append(woken, [Waiting(on="input")], expected_seq=2,
                       patch=Patch(status="waiting", waiting_on="input", inbox_read_seq=1,
                                   keep=True))
    await asyncio.sleep(0.1)
    kept = await store.claim("w5", ["default"], 30)
    assert kept is not None and kept.reclaimed


async def test_two_racing_runners_never_both_append(store: Store) -> None:
    await store.create(new())
    first = await store.claim("w1", ["default"], -1)
    second = await store.claim("w2", ["default"], 30)
    assert first is not None and second is not None
    results = await asyncio.gather(
        store.append(first, [Waiting(on="input")], expected_seq=1),
        store.append(second, [Waiting(on="input")], expected_seq=1),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Conflict) for r in results) == 1
    assert len(await store.read("t1")) == 2


async def test_waiting_releases_claim_and_post_wakes(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input"))
    row = await store.get("t1")
    assert row is not None and row.status == "waiting" and row.owner is None
    assert await store.claim("w", ["default"], 30) is None
    assert await store.post(Post(task_id="t1", body=say("hi"))) == (1, True)
    row = await store.get("t1")
    assert row is not None and row.status == "ready"


async def test_claiming_a_woken_task_clears_what_it_waited_on(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input"))
    await store.post(Post(task_id="t1", body=say("hi")))
    again = await store.claim("w", ["default"], 30)
    assert again is not None
    row = await store.get("t1")
    assert row is not None and row.status == "running" and row.waiting_on is None


async def test_going_to_wait_with_unread_inbox_stays_ready(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.post(Post(task_id="t1", body=say("arrived while running")))
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input", inbox_read_seq=0))
    row = await store.get("t1")
    assert row is not None and row.status == "ready"
    again = await store.claim("w", ["default"], 30)
    assert again is not None and again.epoch == claim.epoch + 1


async def test_an_append_that_leaves_a_task_ready_reports_it_released(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.post(Post(task_id="t1", body=say("arrived while running")))
    seen: list[tuple[str, str]] = []
    unsubscribe = store.on_change(lambda t, k: seen.append((t, k)))
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input", inbox_read_seq=0))
    unsubscribe()
    assert ("t1", "released") in seen


async def test_a_kept_wait_stays_with_its_claim(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input", keep=True))
    row = await store.get("t1")
    assert row is not None and row.status == "waiting" and row.owner == "w"
    assert await store.claim("w2", ["default"], 30) is None
    await store.post(Post(task_id="t1", body=say("hi")))
    row = await store.get("t1")
    assert row is not None and row.status == "ready"
    assert await store.claim("w2", ["default"], 30) is None  # still w's
    renewed = await store.renew(claim, 30)
    assert renewed is not None
    await store.append(renewed, [_record()], expected_seq=2, patch=Patch(inbox_read_seq=1))
    row = await store.get("t1")
    assert row is not None and row.status == "running" and row.owner_epoch == claim.epoch


async def test_a_kept_wait_is_claimable_once_its_lease_runs_out(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 0.05)
    assert claim is not None
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input", keep=True))
    await asyncio.sleep(0.1)
    # Gone silent: whoever claims it next tells what died with that worker.
    taken = await store.claim("w2", ["default"], 30)
    assert taken is not None and taken.epoch == claim.epoch + 1
    with pytest.raises(Conflict):
        await store.append(claim, [Waiting(on="input")], expected_seq=2)
    assert await store.renew(claim, 30) is None


async def test_releasing_a_kept_wait_leaves_it_waiting(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [Waiting(on="input", until=1e12)], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input", ready_at=1e12,
                                   keep=True))
    await store.release(claim)
    row = await store.get("t1")
    assert row is not None and row.status == "waiting" and row.owner is None
    assert row.lease_until is None and row.ready_at == 1e12
    assert await store.renew(claim, 30) is None
    with pytest.raises(Conflict):
        await store.append(claim, [_record()], expected_seq=2)
    assert await store.claim("w2", ["default"], 30) is None
    await store.post(Post(task_id="t1", body=say("hi")))
    assert await store.claim("w2", ["default"], 30) is not None


async def test_a_kept_wait_with_a_due_timer_is_claimable_only_once_its_lease_runs_out(
        store: Store) -> None:
    await store.create(new())
    # A lease long enough that a slow store (Postgres on CI) can't outlive it before the check.
    claim = await store.claim("w", ["default"], 1.0)
    assert claim is not None
    await store.append(claim, [Waiting(on="timer", until=0.0)], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="timer", ready_at=0.0,
                                   keep=True))
    assert await store.claim("w2", ["default"], 30) is None
    await asyncio.sleep(1.1)
    assert await store.claim("w2", ["default"], 30) is not None


async def test_ending_over_an_unread_inbox_is_refused(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    seq, _ = await store.post(Post(task_id="t1", body=say("arrived while ending")))
    with pytest.raises(InboxUnread):
        await store.append(claim, [TaskEnded(status="done")], expected_seq=1,
                           patch=Patch(status="ended", inbox_read_seq=seq - 1))
    row = await store.get("t1")
    assert row is not None and row.status == "running" and row.last_seq == 1
    await store.append(claim, [TaskEnded(status="done")], expected_seq=1,
                       patch=Patch(status="ended", inbox_read_seq=seq))
    row = await store.get("t1")
    assert row is not None and row.status == "ended"


async def test_post_dedupe_and_unknown_task(store: Store) -> None:
    await store.create(new())
    assert await store.post(Post(task_id="t1", body=say("a"), dedupe_key="k")) == (1, True)
    assert await store.post(Post(task_id="t1", body=say("b"), dedupe_key="k")) == (1, False)
    assert await store.post(Post(task_id="t1", body=Cancel())) == (2, True)
    items = await store.inbox("t1")
    assert [i.seq for i in items] == [1, 2] and isinstance(items[1].body, Cancel)
    assert [i.seq for i in await store.inbox("t1", after=1)] == [2]
    with pytest.raises(UnknownTask):
        await store.post(Post(task_id="nope", body=Cancel()))


async def test_append_posts_and_creates_are_atomic_with_events(store: Store) -> None:
    await store.create(new("parent"))
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    child = NewTask(task_id="child", created=TaskCreated(agent="a", parent_id="parent",
                                                         root_id="parent"),
                    inbox=[say("do it")])
    await store.append(claim, [Waiting(on="children", ids=["child"])], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="children"), creates=[child])
    row = await store.get("child")
    assert row is not None and row.parent_id == "parent" and row.root_id == "parent"
    assert row.inbox_seq == 1 and len(await store.inbox("child")) == 1
    assert [r.task_id for r in await store.tasks(parent_id="parent")] == ["child"]
    with pytest.raises(Conflict):
        await store.append(claim, [], expected_seq=2, creates=[new("never")])
    assert await store.get("never") is None


async def test_ended_task_is_not_claimable(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [TaskEnded(status="done")], expected_seq=1,
                       patch=Patch(status="ended", end_status="done"))
    await store.post(Post(task_id="t1", body=say("late")))
    row = await store.get("t1")
    assert row is not None and row.status == "ended" and row.end_status == "done"
    assert await store.claim("w", ["default"], 30) is None


async def test_timer_wait_becomes_claimable_when_due(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [Waiting(on="timer")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="timer", ready_at=0.0))
    assert await store.claim("w", ["default"], 30) is not None


async def test_release_hands_task_back(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.release(claim)
    assert await store.claim("w2", ["default"], 30) is not None


async def test_on_change_reports_commits(store: Store) -> None:
    seen: list[tuple[str, str]] = []
    unsubscribe = store.on_change(lambda t, k: seen.append((t, k)))
    await store.create(new())
    await store.post(Post(task_id="t1", body=Cancel()))
    unsubscribe()
    await store.post(Post(task_id="t1", body=Cancel()))
    assert seen == [("t1", "created"), ("t1", "posted")]


async def test_a_release_is_reported_only_when_it_changed_a_row(store: Store) -> None:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    seen: list[tuple[str, str]] = []
    unsubscribe = store.on_change(lambda t, k: seen.append((t, k)))
    await store.release(claim)
    await store.release(claim)  # already given back
    unsubscribe()
    assert seen == [("t1", "released")]


async def test_queues_partition_claims(store: Store) -> None:
    await store.create(new("a", queue="gpu"))
    assert await store.claim("w", ["default"], 30) is None
    claim = await store.claim("w", ["gpu", "default"], 30)
    assert claim is not None and claim.task_id == "a"


async def test_claim_roots_serves_only_those_trees(store: Store) -> None:
    await store.create(new("mine"))
    await store.create(NewTask(task_id="mine.child",
                               created=TaskCreated(agent="a", parent_id="mine", root_id="mine")))
    await store.create(new("theirs"))
    assert await store.claim("w", ["default"], 30, roots=[]) is None
    got = set()
    for _ in range(3):
        claim = await store.claim("w", ["default"], 30, roots=["mine"])
        if claim is not None:
            got.add(claim.task_id)
    assert got == {"mine", "mine.child"}
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None and claim.task_id == "theirs"


async def test_requeue_moves_a_tree_onto_another_queue(store: Store) -> None:
    await store.create(new("r", queue="front-end"))
    await store.create(NewTask(task_id="r.child", queue="front-end",
                               created=TaskCreated(agent="a", parent_id="r", root_id="r")))
    await store.create(new("other", queue="front-end"))
    seen: list[tuple[str, str]] = []
    store.on_change(lambda tid, kind: seen.append((tid, kind)))
    assert await store.requeue("r", "front-end", "default")
    assert {("r", "released"), ("r.child", "released")} <= set(seen)
    got = set()
    for _ in range(3):
        claim = await store.claim("w", ["default"], 30)
        if claim is not None:
            assert claim.queue == "default"
            got.add(claim.task_id)
    assert got == {"r", "r.child"}
    row = await store.get("other")
    assert row is not None and row.queue == "front-end"


async def test_requeue_leaves_a_tree_whose_task_is_held(store: Store) -> None:
    await store.create(new("r", queue="front-end"))
    await store.create(NewTask(task_id="r.child", queue="front-end",
                               created=TaskCreated(agent="a", parent_id="r", root_id="r")))
    claim = await store.claim("fe", ["front-end"], 30)
    assert claim is not None
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input", keep=True))
    assert not await store.requeue("r", "front-end", "default")
    assert {r.queue for r in (await store.get("r"), await store.get("r.child")) if r} \
        == {"front-end"}
    await store.release(claim)
    assert await store.requeue("r", "front-end", "default")


async def test_requeue_moves_a_tree_whose_lease_ran_out(store: Store) -> None:
    await store.create(new("r", queue="front-end"))
    assert await store.claim("gone", ["front-end"], 0.01) is not None
    await asyncio.sleep(0.05)
    assert await store.requeue("r", "front-end", "default")
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None and claim.task_id == "r"


async def test_append_skips_a_post_to_an_unknown_task(store: Store) -> None:
    await store.create(new("parent"))
    await store.create(new("other"))
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None and claim.task_id == "parent"
    done = await store.append(claim, [TaskEnded(status="cancelled")], expected_seq=1,
                              patch=Patch(status="ended", end_status="cancelled"),
                              posts=[Post(task_id="ghost", body=Cancel()),
                                     Post(task_id="other", body=Cancel())])
    assert done.seq == 2
    row = await store.get("parent")
    assert row is not None and row.status == "ended"
    assert len(await store.inbox("other")) == 1


async def test_a_claim_names_the_tasks_queue(store: Store) -> None:
    await store.create(new("t1", queue="gpu"))
    claim = await store.claim("w", ["default", "gpu"], 30)
    assert claim is not None and claim.queue == "gpu"
    renewed = await store.renew(claim, 30)
    assert renewed is not None and renewed.queue == "gpu"


async def test_a_post_that_does_not_wake(store: Store) -> None:
    await store.create(new("a"))
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input"))
    await store.post(Post(task_id="a", body=say("later"), wake=False))
    row = await store.get("a")
    assert row is not None and row.status == "waiting" and row.inbox_seq == 1
    assert await store.claim("w", ["default"], 30) is None


async def test_snapshots_keep_the_latest_seq(store: Store) -> None:
    """Optional members: a store without them is skipped."""
    get, put = getattr(store, "snapshot", None), getattr(store, "put_snapshot", None)
    if get is None or put is None:
        pytest.skip("the store keeps no snapshots")
    assert await get("t1") is None
    await put("t1", Snapshot(seq=5, version="v1", data=b"\x00five"))
    await put("t1", Snapshot(seq=3, version="v1", data=b"three"))
    assert await get("t1", "v1") == Snapshot(seq=5, version="v1", data=b"\x00five")
    await put("t1", Snapshot(seq=9, version="v1", data=b"nine"))
    await put("t2", Snapshot(seq=1, version="v1", data=b"one"))
    assert await get("t1") == Snapshot(seq=9, version="v1", data=b"nine")
    assert (await get("t2") or Snapshot(seq=0, version="", data=b"")).seq == 1


async def test_snapshots_of_two_versions_are_both_kept(store: Store) -> None:
    """Workers of two fold versions (a rolling upgrade) each keep their
    own snapshot instead of replacing each other's; without a version the
    newest is read. Past ``SNAPSHOT_VERSIONS`` the one furthest behind
    goes."""
    get, put = getattr(store, "snapshot", None), getattr(store, "put_snapshot", None)
    if get is None or put is None:
        pytest.skip("the store keeps no snapshots")
    await put("t1", Snapshot(seq=5, version="old", data=b"old five"))
    await put("t1", Snapshot(seq=3, version="new", data=b"new three"))
    await put("t1", Snapshot(seq=7, version="old", data=b"old seven"))
    assert await get("t1", "new") == Snapshot(seq=3, version="new", data=b"new three")
    assert await get("t1", "old") == Snapshot(seq=7, version="old", data=b"old seven")
    assert await get("t1") == Snapshot(seq=7, version="old", data=b"old seven")
    assert await get("t1", "other") is None
    assert SNAPSHOT_VERSIONS == 2
    await put("t1", Snapshot(seq=4, version="third", data=b"third four"))
    assert await get("t1", "new") is None  # the furthest behind
    assert (await get("t1", "old"), await get("t1", "third")) == (
        Snapshot(seq=7, version="old", data=b"old seven"),
        Snapshot(seq=4, version="third", data=b"third four"))


async def test_ending_a_task_deletes_its_snapshot(store: Store) -> None:
    """The end deletes it in the same transaction, and none is kept after."""
    get, put = getattr(store, "snapshot", None), getattr(store, "put_snapshot", None)
    if get is None or put is None:
        pytest.skip("the store keeps no snapshots")
    await store.create(new())
    await store.create(new("t2"))
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None and claim.task_id == "t1"
    await put("t1", Snapshot(seq=1, version="v1", data=b"one"))
    await put("t1", Snapshot(seq=1, version="v2", data=b"one again"))
    await put("t2", Snapshot(seq=1, version="v1", data=b"other"))
    await store.append(claim, [TaskEnded(status="done")], expected_seq=1,
                       patch=Patch(status="ended", end_status="done"))
    assert await get("t1") is None and await get("t1", "v2") is None
    await put("t1", Snapshot(seq=2, version="v1", data=b"late"))  # a write in flight
    assert await get("t1") is None
    assert await get("t2") == Snapshot(seq=1, version="v1", data=b"other")
