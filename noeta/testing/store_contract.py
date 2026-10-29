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

from noeta.core.errors import Conflict, InboxUnread, UnknownTask, UnservedQueue
from noeta.core.events import DEFAULT_TENANT, Cancel, External, Say, TaskCreated, TaskEnded, Waiting
from noeta.core.store import (
    HOLD_LOST,
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


async def test_a_task_row_carries_its_tenant_and_lists_filter_by_it(store: Store) -> None:
    await store.create(new("plain"))
    await store.create(NewTask(task_id="theirs", created=TaskCreated(agent="a", tenant="acme")))
    plain, theirs = await store.get("plain"), await store.get("theirs")
    assert plain is not None and plain.tenant == DEFAULT_TENANT
    assert theirs is not None and theirs.tenant == "acme"
    assert [r.task_id for r in await store.tasks(tenant="acme")] == ["theirs"]
    assert [r.task_id for r in await store.tasks(tenant=DEFAULT_TENANT)] == ["plain"]
    assert {r.task_id for r in await store.tasks()} == {"plain", "theirs"}


def _of(tenant: str, task_id: str) -> NewTask:
    return NewTask(task_id=task_id, created=TaskCreated(agent="a", tenant=tenant))


async def test_a_tenant_at_its_running_limit_waits_its_turn(store: Store) -> None:
    for task in (_of("acme", "a1"), _of("acme", "a2"), _of("beta", "b1")):
        await store.create(task)
    limits = {"acme": 1}
    first = await store.claim("w", ["default"], 30, tenant_limits=limits)
    second = await store.claim("w", ["default"], 30, tenant_limits=limits)
    assert first is not None and first.task_id == "a1"
    assert second is not None and second.task_id == "b1"  # a2 waits
    assert await store.claim("w", ["default"], 30, tenant_limits=limits) is None
    await store.append(first, [TaskEnded(status="done")], expected_seq=1,
                       patch=Patch(status="ended"))
    third = await store.claim("w", ["default"], 30, tenant_limits=limits)
    assert third is not None and third.task_id == "a2"


async def test_a_task_whose_lease_ran_out_does_not_count_toward_its_tenants_limit(
        store: Store) -> None:
    """A running task whose worker went silent past its lease holds no
    worker: counting it would keep its tenant's other tasks waiting until
    someone reclaims it."""
    for task in (_of("acme", "a1"), _of("acme", "a2")):
        await store.create(task)
    limits = {"acme": 1}
    lapsed = await store.claim("w", ["default"], -60, tenant_limits=limits)
    assert lapsed is not None and lapsed.task_id == "a1"
    second = await store.claim("w2", ["default"], 30, tenant_limits=limits)
    assert second is not None and second.task_id == "a2"


async def test_reclaiming_a_lapsed_task_counts_toward_its_tenants_limit(
        store: Store) -> None:
    """Taking back a task whose lease ran out starts it running again like
    any claim: with its tenant at the limit it stays where it is."""
    for task in (_of("acme", "a1"), _of("acme", "a2")):
        await store.create(task)
    limits = {"acme": 1}
    lapsed = await store.claim("w1", ["default"], -60, tenant_limits=limits)
    assert lapsed is not None and lapsed.task_id == "a1"
    second = await store.claim("w2", ["default"], 30, tenant_limits=limits)
    assert second is not None and second.task_id == "a2"
    assert await store.claim("w2", ["default"], 30, tenant_limits=limits) is None
    running = [r.task_id for r in await store.tasks(status="running")
               if r.owner == "w2"]
    assert running == ["a2"]


async def test_workers_claiming_at_once_keep_a_tenant_under_its_limit(store: Store) -> None:
    for i in range(6):
        await store.create(_of("acme", f"a{i}"))
    got = await asyncio.gather(*(store.claim(f"w{i}", ["default"], 30,
                                             tenant_limits={"acme": 2}) for i in range(6)))
    assert len([c for c in got if c is not None]) == 2
    assert len(await store.tasks(tenant="acme", status="running")) == 2


async def test_a_tenant_at_its_limit_leaves_racing_claims_to_the_others(store: Store) -> None:
    """Claims racing for a limited tenant's tasks that find it full after
    all take another tenant's task instead of coming back empty."""
    for i in range(6):
        await store.create(_of("acme", f"a{i}"))
    for i in range(12):
        await store.create(_of("beta", f"b{i:02d}"))
    got = await asyncio.gather(*(store.claim(f"w{i}", ["default"], 30,
                                             tenant_limits={"acme": 2}) for i in range(12)))
    assert all(c is not None for c in got)
    assert len(await store.tasks(tenant="acme", status="running")) == 2


async def test_tenant_usage_adds_up_per_period(store: Store) -> None:
    add = getattr(store, "add_tenant_usage", None)
    if add is None:
        pytest.skip("this store keeps no tenant usage")
    await asyncio.gather(*(add("acme", "2026-10", 10, 0.5) for _ in range(4)))
    await add("acme", "2026-11", 1, 0.0)
    assert await store.tenant_usage("acme", "2026-10") == (40, 2.0)  # type: ignore[attr-defined]
    assert await store.tenant_usage("beta", "2026-10") == (0, 0.0)  # type: ignore[attr-defined]


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


async def test_a_post_to_an_ended_task_can_go_to_its_root(store: Store) -> None:
    await store.create(new("root"))
    await store.create(NewTask(task_id="root.child",
                               created=TaskCreated(agent="a", parent_id="root", root_id="root")))
    claims = [await store.claim("w", ["default"], 30) for _ in range(2)]
    claim = next(c for c in claims if c is not None and c.task_id == "root.child")
    live = Post(task_id="root.child", body=say("live"), forward_if_ended=True)
    await store.post(live)
    read = (await store.get("root.child")).inbox_seq  # type: ignore[union-attr]
    await store.append(claim, [TaskEnded(status="done")], expected_seq=1,
                       patch=Patch(status="ended", inbox_read_seq=read))
    late = Post(task_id="root.child", body=say("late"), dedupe_key="k", forward_if_ended=True)
    assert (await store.post(late))[1]
    assert not (await store.post(late))[1]  # the same dedupe key, at the root
    assert [i.body for i in await store.inbox("root")] == [say("late")]
    assert [i.body for i in await store.inbox("root.child")] == [say("live")]
    await store.post(Post(task_id="root.child", body=say("plain")))
    assert len(await store.inbox("root.child")) == 2


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


async def test_a_claim_names_the_tasks_queue(store: Store) -> None:
    await store.create(new("t1", queue="gpu"))
    claim = await store.claim("w", ["default", "gpu"], 30)
    assert claim is not None and claim.queue == "gpu"
    renewed = await store.renew(claim, 30)
    assert renewed is not None and renewed.queue == "gpu"


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


async def test_a_hold_is_refused_to_a_worker_not_serving_every_queue_of_the_tree(
        store: Store) -> None:
    """Other workers skip a held tree, so its holder must be able to claim
    each of its unended tasks: else a task on a queue it does not serve
    waits for as long as the hold is renewed."""
    await store.create(new("root"))
    await store.create(NewTask(task_id="root.gpu", queue="gpu",
                               created=TaskCreated(agent="a", parent_id="root", root_id="root")))
    assert await store.hold("root", "B", 30, queues=["gpu"]) is None
    epoch = await store.hold("root", "A", 30, queues=["default", "gpu"])
    assert epoch is not None
    assert await store.hold("root", "A", 30, queues=["gpu"], epoch=epoch) == epoch  # renewing
    await store.unhold("root", "A", epoch)
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None and claim.task_id == "root"
    await store.append(claim, [TaskEnded(status="done")], expected_seq=1,
                       patch=Patch(status="ended", end_status="done"))
    assert await store.hold("root", "B", 30, queues=["gpu"]) is not None  # ended: not counted


async def test_an_append_refuses_a_child_on_a_queue_the_trees_holder_does_not_serve(
        store: Store) -> None:
    """Checked when the child is written, not only when it is staged: the
    tree may have been held since (by this worker, for another of its
    tasks), and its holder could never claim the child."""
    await store.create(new("root"))
    claim = await store.claim("A", ["default"], 30)
    assert claim is not None

    def child(task_id: str, queue: str) -> NewTask:
        return NewTask(task_id=task_id, queue=queue, inbox=[say("go")],
                       created=TaskCreated(agent="a", parent_id="root", root_id="root"))

    epoch = await store.hold("root", "A", 30, queues=["default"])
    assert epoch is not None
    with pytest.raises(UnservedQueue) as refused:
        await store.append(claim, [Waiting(on="children", ids=["root.ok", "root.gpu"])],
                           expected_seq=1,
                           creates=[child("root.ok", "default"), child("root.gpu", "gpu")])
    assert refused.value.queue == "gpu"
    assert await store.get("root.gpu") is None and await store.get("root.ok") is None
    assert [r.seq for r in await store.read("root")] == [1]  # nothing was written
    await store.append(claim, [], expected_seq=1, creates=[child("root.ok", "default")])
    assert await store.get("root.ok") is not None
    await store.unhold("root", "A", epoch)
    await store.append(claim, [], expected_seq=1, creates=[child("root.gpu", "gpu")])
    assert await store.get("root.gpu") is not None  # no hold: any queue


async def test_a_tree_has_at_most_one_holding_worker(store: Store) -> None:
    """A second worker's hold is refused, so the held tree's tasks always
    have a worker that may claim them; once released, another may hold."""
    await store.create(new("root"))
    await store.create(NewTask(task_id="root.child",
                               created=TaskCreated(agent="a", parent_id="root", root_id="root")))
    epoch = await store.hold("root", "A", 30)
    assert epoch is not None
    assert await store.hold("root", "B", 30) is None
    assert await store.hold("root", "A", 30) == epoch
    assert await store.claim("B", ["default"], 30) is None
    assert await store.claim("C", ["default"], 30) is None
    claims = [c for c in [await store.claim("A", ["default"], 30) for _ in range(2)] if c]
    assert {c.task_id for c in claims} == {"root", "root.child"}
    await store.unhold("root", "A", epoch)
    assert await store.hold("root", "B", 30) is None  # its tasks still run on A
    for claim in claims:
        await store.release(claim)
    other = await store.hold("root", "B", 30)
    assert other is not None and other != epoch


async def test_a_hold_is_refused_while_a_task_of_the_tree_runs_on_another_worker(
        store: Store) -> None:
    """A worker running one task of a tree cannot hold it while another
    worker runs another of its tasks: the root could then run where the
    tree's jobs are not. An expired lease does not count."""
    await store.create(new("root"))
    await store.create(NewTask(task_id="root.child",
                               created=TaskCreated(agent="a", parent_id="root", root_id="root")))
    on_b = await store.claim("B", ["default"], 30)
    on_a = await store.claim("A", ["default"], 30)
    assert on_b is not None and on_b.task_id == "root"
    assert on_a is not None and on_a.task_id == "root.child"
    assert await store.hold("root", "A", 30) is None
    assert await store.hold("root", "B", 30) is None
    await store.release(on_b)
    epoch = await store.hold("root", "A", 30)
    assert epoch is not None
    assert await store.claim("B", ["default"], 30) is None
    await store.create(new("other"))
    stale = await store.claim("B", ["default"], 0.05)
    assert stale is not None and stale.task_id == "other"
    await asyncio.sleep(0.15)
    assert await store.hold("other", "A", 30) is not None


async def test_a_hold_revokes_another_workers_lapsed_claim_of_the_tree(store: Store) -> None:
    """A worker that stalled past its lease (a pause, SIGSTOP) cannot renew
    or write once another worker holds the tree: the hold took the lapsed
    claim back, so only the holder runs the tree's tasks."""
    await store.create(new("root"))
    stalled = await store.claim("B", ["default"], 0.05)
    assert stalled is not None
    await asyncio.sleep(0.15)
    epoch = await store.hold("root", "A", 30)
    assert epoch is not None
    assert await store.renew(stalled, 30) is None
    with pytest.raises(Conflict):
        await store.append(stalled, [Waiting(on="input")], expected_seq=1)
    row = await store.get("root")
    assert row is not None and row.status == "ready" and row.owner is None
    assert await store.claim("B", ["default"], 30) is None
    again = await store.claim("A", ["default"], 30)
    assert again is not None and again.task_id == "root" and again.epoch > stalled.epoch


async def test_workers_holding_one_tree_at_once_leave_one_holder(store: Store) -> None:
    await store.create(new("root"))
    got = await asyncio.gather(*(store.hold("root", f"w{i}", 30) for i in range(6)))
    assert len([e for e in got if e is not None]) == 1


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
    claims = [c for c in [await store.claim("w2", ["default"], 30) for _ in range(2)] if c]
    assert {c.task_id for c in claims} == {"root", "root.child"}
    for claim in claims:
        await store.release(claim)
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


async def test_an_abandoned_hold_tells_the_tree_without_waking_it(store: Store) -> None:
    await store.create(new("a"))
    claim = await store.claim("w1", ["default"], 30)
    assert claim is not None
    await store.append(claim, [Waiting(on="input")], expected_seq=1,
                       patch=Patch(status="waiting", waiting_on="input", ready_at=10**12))
    epoch = await store.hold("a", "w1", 30)
    assert epoch is not None
    await store.unhold("a", "w1", epoch, lost=True)
    await store.unhold("a", "w1", epoch, lost=True)  # gone: nothing more
    [item] = await store.inbox("a")
    assert item.body == External(name=HOLD_LOST, data={"worker": "w1", "epoch": epoch})
    row = await store.get("a")
    assert row is not None and (row.status, row.ready_at) == ("waiting", 10**12)
    await store.post(Post(task_id="a", body=say("hi")))  # the next message wakes it
    row = await store.get("a")
    assert row is not None and row.status == "ready" and row.inbox_seq == 2


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
