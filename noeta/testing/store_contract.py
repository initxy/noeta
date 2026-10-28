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

from noeta.core.errors import Conflict, UnknownTask
from noeta.core.events import Cancel, External, Say, TaskCreated, TaskEnded, Waiting
from noeta.core.store import HOLD_LOST, NewTask, Patch, Post, Snapshot, Store
from noeta.core.types import user


def new(task_id: str = "t1", **kw: object) -> NewTask:
    return NewTask(task_id=task_id, created=TaskCreated(agent="a"), **kw)  # type: ignore[arg-type]


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
    epoch = await store.hold("t1", "w", 30)
    assert epoch is not None
    seen: list[tuple[str, str]] = []
    unsubscribe = store.on_change(lambda t, k: seen.append((t, k)))
    await store.unhold("t1", "w2", epoch)  # not w2's hold
    await store.unhold("t1", "w", epoch)
    await store.unhold("t1", "w", epoch)  # already gone
    await store.release(claim)
    await store.release(claim)  # already given back
    unsubscribe()
    assert seen == [("t1", "released"), ("t1", "released")]


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


async def test_a_held_tree_is_claimed_only_by_its_holders(store: Store) -> None:
    await store.create(new("root"))
    await store.create(NewTask(task_id="root.child",
                               created=TaskCreated(agent="a", parent_id="root", root_id="root")))
    await store.create(new("other"))
    epoch = await store.hold("root", "w1", 30)
    assert epoch is not None
    assert await store.hold("root", "w1", 30, epoch=epoch) == epoch  # renewing
    assert await store.hold("root", "w1", 30) == epoch  # holding again: the same hold
    claim = await store.claim("w2", ["default"], 30)
    assert claim is not None and claim.task_id == "other"
    assert await store.claim("w2", ["default"], 30) is None
    got = {c.task_id for c in [await store.claim("w1", ["default"], 30) for _ in range(2)] if c}
    assert got == {"root", "root.child"}


async def test_an_expired_hold_blocks_claims_until_it_is_reaped(store: Store) -> None:
    await store.create(new("root"))
    root = await store.claim("w1", ["default"], 30)
    assert root is not None
    await store.append(root, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input"))
    await store.create(NewTask(task_id="root.child",
                               created=TaskCreated(agent="a", parent_id="root", root_id="root")))
    epoch = await store.hold("root", "w1", 0.05)
    assert epoch is not None
    await asyncio.sleep(0.15)
    # Run out but not reaped: still held (nobody has told the tree yet).
    assert await store.claim("w2", ["default"], 30) is None
    seen: list[tuple[str, str]] = []
    unsubscribe = store.on_change(lambda t, k: seen.append((t, k)))
    assert await store.reap_holds() == 1
    assert await store.reap_holds() == 0
    unsubscribe()
    assert {("root", "released"), ("root", "posted"), ("root.child", "posted")} <= set(seen)
    # The holder lost it: renewing fails, a fresh hold gets a new epoch.
    assert await store.hold("root", "w1", 30, epoch=epoch) is None
    lost = External(name=HOLD_LOST, data={"worker": "w1", "epoch": epoch})
    assert [i.body for i in await store.inbox("root")] == [lost]
    assert [i.body for i in await store.inbox("root.child")] == [lost]
    row = await store.get("root")
    assert row is not None and row.status == "ready"
    got = {c.task_id for c in [await store.claim("w2", ["default"], 30) for _ in range(2)] if c}
    assert got == {"root", "root.child"}
    again = await store.hold("root", "w1", 30)
    assert again is not None and again != epoch


async def test_a_released_hold_frees_the_tree(store: Store) -> None:
    await store.create(new("a"))
    epoch = await store.hold("a", "w1", 30)
    assert epoch is not None
    await store.unhold("a", "w2", epoch)  # not w2's hold: no change
    await store.unhold("a", "w1", epoch + 1)  # not this hold
    assert await store.claim("w2", ["default"], 30, roots=["a"]) is None
    await store.unhold("a", "w1", epoch)
    assert await store.hold("a", "w1", 30, epoch=epoch) is None
    assert await store.inbox("a") == []  # released, not lost
    claim = await store.claim("w2", ["default"], 30, roots=["a"])
    assert claim is not None and claim.task_id == "a"


async def test_an_abandoned_hold_tells_the_tree(store: Store) -> None:
    await store.create(new("a"))
    epoch = await store.hold("a", "w1", 30)
    assert epoch is not None
    await store.unhold("a", "w1", epoch, lost=True)
    await store.unhold("a", "w1", epoch, lost=True)  # gone: nothing more
    [item] = await store.inbox("a")
    assert item.body == External(name=HOLD_LOST, data={"worker": "w1", "epoch": epoch})


async def test_snapshots_keep_the_latest_seq(store: Store) -> None:
    """Optional members: a store without them is skipped."""
    get, put = getattr(store, "snapshot", None), getattr(store, "put_snapshot", None)
    if get is None or put is None:
        pytest.skip("the store keeps no snapshots")
    assert await get("t1") is None
    await put("t1", Snapshot(seq=5, version="v1", data=b"\x00five"))
    await put("t1", Snapshot(seq=3, version="v2", data=b"three"))
    assert await get("t1") == Snapshot(seq=5, version="v1", data=b"\x00five")
    await put("t1", Snapshot(seq=9, version="v2", data=b"nine"))
    await put("t2", Snapshot(seq=1, version="v1", data=b"one"))
    assert await get("t1") == Snapshot(seq=9, version="v2", data=b"nine")
    assert (await get("t2") or Snapshot(seq=0, version="", data=b"")).seq == 1
