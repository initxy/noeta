"""Many workers on one Postgres store, killed and frozen at random.

Root tasks run a scripted conversation (``noeta_cluster_scripted``): tool
calls with a journalled side effect, background ``Bash`` jobs, sub-agents,
``ScheduleWakeup`` timers, and messages sent while they run. Worker processes
(``noeta worker --lease 1``) are SIGKILLed and respawned, or SIGSTOPped
past their lease and resumed. Afterwards:

* every task finishes its script within the deadline;
* no side-effect token is journalled twice, and a call the log shows
  finished was journalled exactly once;
* every message sent is taken by its task exactly once;
* every background job ends in the log as ended or lost, never missing,
  and the model is told so exactly once;
* the log invariants of :mod:`tests.cluster.invariants` hold.

Run with ``make cluster`` (``NOETA_CLUSTER_SEED`` replays a run;
``NOETA_CLUSTER_TASKS`` / ``NOETA_CLUSTER_SECONDS`` size it). A scenario a
known multi-machine gap breaks is marked ``xfail(strict=True, raises=Gap)``:
the gap's own check raises :class:`Gap`, any other broken invariant still
fails the test, and fixing the gap turns the xfail into an error until the
marker is removed.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from pathlib import Path
from typing import Any

import pytest

from noeta.core.events import ToolFinished, ToolStarted
from noeta.core.store import HOLD_LOST
from noeta.plugins.shell.tools import HELD_ELSEWHERE
from tests.cluster.harness import (
    SPILL_AGENT,
    Cluster,
    Faults,
    Outcome,
    Spec,
    chaos,
    collect,
    kill_all,
    kill_holder,
    root_finished,
    seed_from_env,
    send_pings,
    wait_until,
    watch_rows,
)
from tests.cluster.invariants import (
    EpochWatch,
    jobs_of,
    last_reply,
    lost_jobs_ended_late,
    taken_texts,
)

pytestmark = pytest.mark.cluster

TASKS = int(os.environ.get("NOETA_CLUSTER_TASKS", "24"))
SECONDS = float(os.environ.get("NOETA_CLUSTER_SECONDS", "20"))
DRAIN = float(os.environ.get("NOETA_CLUSTER_DRAIN", "90"))
LEASE = 1.0
#: Faults in the cron scenario last past the jobs' second firing.
CRON_SECONDS = 70.0


class Gap(AssertionError):
    """A known multi-machine gap showed up: the expected failure of an xfail
    scenario."""


def fail(outcome: Outcome, problems: list[str]) -> str:
    known = {*outcome.roots.values(), *(r.task_id for r in outcome.rows)}
    ids = [w for p in problems for w in re.findall(r"[0-9a-f]{32}[\w.-]*", p) if w in known]
    ids += [outcome.roots[m] for p in problems for m in re.findall(r"root (r\d+)", p)
            if m in outcome.roots]
    head = "\n".join(problems[:30])
    return f"{len(problems)} problem(s):\n{head}\n{outcome.describe(ids[:3])}"


def make_specs(rng: random.Random, n: int, *, bg: bool) -> dict[str, Spec]:
    specs = {}
    for i in range(n):
        sid = f"r{i:02d}"
        spec = Spec(id=sid, effects=rng.randint(1, 3), agent=rng.random() < 0.5,
                    wake=rng.random() < 0.5,
                    lookup=round(rng.uniform(0.3, 1.0), 1) if rng.random() < 0.5 else 0.0)
        if bg and i % 2 == 0:
            spec.bg = round(rng.uniform(1.0, 3.0), 1)
        if bg and i % 4 == 0:
            # a job and nothing else: once the root waits, only the job's
            # end (or its loss) can wake it
            spec.effects, spec.agent, spec.wake, spec.bg, spec.lookup = 1, False, False, 4.0, 0.0
        else:
            spec.pings = rng.choice([0, 0, 1, 2, 3])
        specs[sid] = spec
    return specs


async def run_cluster(tmp_path: Path, url: str, seed: int, specs: dict[str, Spec], *,
                      seconds: float, targets: set[str] | None = None,
                      workers: int = 3, drain: float = DRAIN, faulty: Any = chaos,
                      fire_after: float = 2.0) -> Outcome:
    """Create the roots, run the faults and the pings for ``seconds``, then
    let the workers drain until every root is done or ``DRAIN`` passes."""
    rng = random.Random(seed)
    cluster = Cluster(tmp_path, url, workers=workers, lease=LEASE, fire_after=fire_after)
    faults, watch, stop = Faults(), EpochWatch(), asyncio.Event()
    await cluster.start()
    watcher = asyncio.create_task(watch_rows(cluster, watch, stop))
    try:
        roots = {sid: await cluster.create(spec) for sid, spec in specs.items()}
        target_ids = {roots[s] for s in targets or ()}
        plan = [(rng.uniform(0, seconds), roots[sid], spec, j)
                for sid, spec in specs.items() for j in range(spec.pings)]
        if seconds > 0:
            await asyncio.gather(faulty(cluster, rng, seconds, faults, targets=target_ids),
                                 send_pings(cluster, rng, seconds, plan))
        else:
            await send_pings(cluster, rng, 0, plan)
        for i, p in enumerate(cluster.procs):
            if p.poll() is not None:
                cluster.kill(i)  # respawn anything that exited by itself

        async def all_done() -> bool:
            done = await asyncio.gather(*(root_finished(cluster, roots[s], spec)
                                          for s, spec in specs.items()))
            return all(done)

        await wait_until(all_done, drain, interval=1.0)
        stop.set()
        await watcher
        cluster.stop_workers()
        outcome = await collect(cluster, seed=seed, specs=specs, roots=roots,
                                pings={s: sp.pings for s, sp in specs.items()}, watch=watch,
                                faults=faults)
    finally:
        stop.set()
        await cluster.close()
    print(f"seed={seed} roots={len(specs)} tasks={len(outcome.rows)} kills={faults.kills} "
          f"stops={faults.stops} targeted={faults.targeted} "
          f"interrupted={outcome.interrupted_calls()} lost_jobs={outcome.lost_jobs()} "
          f"lookups_rerun={sum(s > 1 for s, _t in outcome.lookups().values())} "
          f"snapshots={outcome.snapshots} "
          f"journal={len(outcome.journal)} samples={watch.samples}")
    return outcome


def assert_invariants(outcome: Outcome) -> None:
    """The invariants no known gap excuses."""
    problems = (outcome.log_violations() + outcome.effect_violations()
                + outcome.lookup_violations() + outcome.watch.violations)
    assert not problems, fail(outcome, problems)


def assert_all_done(outcome: Outcome, *, error: type[AssertionError] = AssertionError) -> None:
    problems = [p for sid in outcome.specs for p in outcome.root_done(sid)]
    if problems:
        raise error(fail(outcome, problems))


# ------------------------------------------------------------------ scenarios


async def test_chaos_tools_agents_timers_messages(tmp_path: Path, pg_url: str) -> None:
    """Tool calls, sub-agents, timers and mid-run messages under random
    SIGKILL / SIGSTOP: everything finishes, nothing runs twice or is lost."""
    seed = seed_from_env()
    specs = make_specs(random.Random(seed), TASKS, bg=False)
    outcome = await run_cluster(tmp_path, pg_url, seed, specs, seconds=SECONDS)
    assert outcome.faults.kills >= 3, fail(outcome, ["too few faults to mean anything"])
    assert_invariants(outcome)
    assert_all_done(outcome)


async def test_timers_and_sub_agents_outlive_every_worker(tmp_path: Path, pg_url: str) -> None:
    """Every worker is killed at once, again and again, while roots wait on
    wake-ups and sub-agents: a timer or a child never belongs to the process
    that set or started it, so every root still finishes."""
    seed = seed_from_env()
    specs = {f"r{i:02d}": Spec(id=f"r{i:02d}", effects=1, agent=True, wake=True)
             for i in range(8)}
    outcome = await run_cluster(tmp_path, pg_url, seed, specs, seconds=12, faulty=kill_all)
    assert outcome.faults.kills >= 9, fail(outcome, ["too few faults to mean anything"])
    assert_invariants(outcome)
    assert_all_done(outcome)


async def test_cron_jobs_outlive_every_worker(tmp_path: Path, pg_url: str) -> None:
    """Recurring ``CronCreate`` jobs (every minute) under a minute of
    SIGKILL / SIGSTOP. The clock runs ten minutes behind, so a job's first
    firing is due at once; it then fires every real minute. Each root
    deletes its job after one or two firings: every job fires that often,
    is deleted, and never fires once its deletion is in the log (the
    one-firing roots are a minute past their deletion by the end)."""
    seed = seed_from_env()
    rng = random.Random(seed)
    specs = {f"r{i:02d}": Spec(id=f"r{i:02d}", effects=1, cron=[1, 2][i] if i < 2
                               else rng.choice([1, 2]), pings=rng.choice([0, 1]))
             for i in range(8)}
    outcome = await run_cluster(tmp_path, pg_url, seed, specs, seconds=CRON_SECONDS,
                                fire_after=-540.0)
    assert outcome.faults.kills >= 3, fail(outcome, ["too few faults to mean anything"])
    assert_invariants(outcome)
    assert_all_done(outcome)


async def test_background_jobs_one_worker(tmp_path: Path, pg_url: str) -> None:
    """The background-job script itself, on one worker with no faults:
    every job ends, and its end reaches its root."""
    seed = seed_from_env()
    specs = make_specs(random.Random(seed), 8, bg=True)
    outcome = await run_cluster(tmp_path, pg_url, seed, specs, seconds=0, workers=1)
    assert_invariants(outcome)
    assert_all_done(outcome)
    not_ended = [f"{tid}: job {job} {[k for _s, k in marks]}"
                 for tid, records in outcome.logs.items()
                 for job, marks in jobs_of(records).items()
                 if [k for _s, k in marks] != ["started", "ended"]]
    assert not not_ended, fail(outcome, not_ended)


async def test_background_job_end_taken_by_another_worker(tmp_path: Path, pg_url: str) -> None:
    """No faults, several workers: a job that finished is never called lost,
    whichever worker takes its end (it is posted before the hold is given
    back)."""
    seed = seed_from_env()
    specs = {f"r{i:02d}": Spec(id=f"r{i:02d}", effects=1, bg=1.0) for i in range(16)}
    outcome = await run_cluster(tmp_path, pg_url, seed, specs, seconds=0, workers=5)
    assert_invariants(outcome)
    assert_all_done(outcome)
    assert outcome.lost_jobs() == 0, fail(outcome, ["a job that finished was called lost"])


async def test_chaos_with_background_jobs(tmp_path: Path, pg_url: str) -> None:
    """The chaos run with background jobs, some faults aimed at the worker
    holding a waiting root's job: such a root still hears of its job (the
    hold is reaped and ``hold_lost`` wakes it)."""
    seed = seed_from_env()
    specs = make_specs(random.Random(seed), TASKS, bg=True)
    targets = {s for s, spec in specs.items() if spec.bg and not spec.pings
               and not spec.agent and not spec.wake}
    # Jobs run at most 4 s and wake-ups fire 2 s after they are set: what
    # has not finished 30 s after the faults stop never will.
    outcome = await run_cluster(tmp_path, pg_url, seed, specs, seconds=SECONDS,
                                targets=targets, drain=30)
    assert_invariants(outcome)
    assert outcome.faults.targeted > 0, fail(outcome, ["no holder was killed; rerun"])
    assert_all_done(outcome)


async def test_retry_safe_call_runs_again_after_its_worker_dies(tmp_path: Path,
                                                                 pg_url: str) -> None:
    """SIGKILL the worker in the middle of a ``retry_safe`` call: the call
    runs again elsewhere with the same idempotency key and finishes with its
    real result, not "outcome unknown"."""
    cluster = Cluster(tmp_path, pg_url, workers=3, lease=LEASE)
    await cluster.start()
    spec = Spec(id="r00", effects=1, lookup=4.0)
    faults = Faults()
    try:
        tid = await cluster.create(spec)

        async def looking() -> bool:
            row = await cluster.client.get(tid)
            records = await cluster.client.events(tid)
            return row.status == "running" and row.owner is not None and any(
                isinstance(r.event, ToolStarted) and r.event.call.name == "Lookup"
                for r in records)

        assert await wait_until(looking, 30), "the lookup never started"
        row = await cluster.client.get(tid)
        assert cluster.kill_pid(int(str(row.owner).split(":")[1]))
        faults.kills += 1
        await wait_until(lambda: root_finished(cluster, tid, spec), 10 * LEASE + 8.0)
        outcome = await _outcome(cluster, tid, spec, faults)
        runs = Path(cluster.journal + ".lookups").read_text().splitlines()
    finally:
        await cluster.close()
    assert_invariants(outcome)
    assert_all_done(outcome)
    (starts, text), = outcome.lookups().values()
    assert (starts, text) == (2, "found r00-l"), fail(outcome, [f"{starts} starts: {text!r}"])
    assert len(runs) == 2 and len({line.split()[1] for line in runs}) == 1, runs


# ------------------------------------------------------------------ one gap each


@pytest.mark.parametrize("crash_after_take", [False, True], ids=["", "crash-after-take"])
async def test_lost_job_of_an_ended_sub_agent_reaches_the_root(tmp_path: Path, pg_url: str,
                                                               crash_after_take: bool) -> None:
    """A sub-agent starts a background job and ends; SIGKILL the worker
    running the job while the root waits on it: the root is told the job
    is lost (the sub-agent's log is closed) and finishes; the job is gone.
    With ``crash_after_take`` the worker that takes ``hold_lost`` for the
    root dies right after the take, before the root decides the notice:
    the next pickup still tells it, once."""
    cluster = Cluster(tmp_path, pg_url, workers=3, lease=LEASE)
    await cluster.start()
    spec = Spec(id="r00", effects=1, agent=True, child_bg=8.0)
    faults = Faults()
    try:
        tid = await cluster.create(spec)

        async def child_gone_job_running() -> bool:
            tree = await cluster.tree(tid)
            root = next(r for r in tree if r.task_id == tid)
            return root.status == "waiting" and any(
                r.parent_id == tid and r.status == "ended" for r in tree) \
                and bool(await cluster.running_job_pids())

        assert await wait_until(child_gone_job_running, 30), "the sub-agent's job never ran"
        if crash_after_take:
            Path(cluster.arm).write_text(HOLD_LOST)
        assert await kill_holder(cluster, {tid}, faults)
        await wait_until(lambda: root_finished(cluster, tid, spec), 10 * LEASE + 6.0)
        await asyncio.sleep(2.0)
        outcome = await _outcome(cluster, tid, spec, faults)
    finally:
        await cluster.close()
    assert_invariants(outcome)
    assert_all_done(outcome)
    told = [t for t in taken_texts(outcome.logs[tid])
            if "<tool-use-id>cbg-r00</tool-use-id>" in t]
    assert len(told) == 1 and "<status>failed</status>" in told[0], fail(outcome, told)
    assert not any(line.startswith("r00-cbg ") for line in outcome.journal), fail(
        outcome, ["the job of a dead worker wrote after the root was told it was lost"])
    if crash_after_take:
        done = Path(cluster.arm + ".done")
        assert done.exists() and tid in done.read_text(), fail(
            outcome, ["no worker died after taking hold_lost"])


async def _one_job(tmp_path: Path, url: str, bg: float) -> tuple[Cluster, str, Spec, int]:
    """A cluster with one root whose background job is running while the
    root waits; returns the job's worker pid."""
    cluster = Cluster(tmp_path, url, workers=3, lease=LEASE)
    await cluster.start()
    spec = Spec(id="r00", effects=1, bg=bg)
    tid = await cluster.create(spec)

    async def holding() -> bool:
        row = await cluster.client.get(tid)
        return row.status == "waiting" and bool(await cluster.running_job_pids())

    assert await wait_until(holding, 30), "the job never started"
    (_root, pid), = await cluster.running_job_pids()
    return cluster, tid, spec, pid


async def _outcome(cluster: Cluster, tid: str, spec: Spec, faults: Faults) -> Outcome:
    cluster.stop_workers()
    return await collect(cluster, seed=0, specs={spec.id: spec}, roots={spec.id: tid},
                         pings={spec.id: spec.pings}, watch=EpochWatch(), faults=faults)


async def test_holder_killed_while_root_waits(tmp_path: Path, pg_url: str) -> None:
    """SIGKILL the worker holding a waiting root's job, send nothing else:
    the root must still learn the job is lost and finish."""
    cluster, tid, spec, _pid = await _one_job(tmp_path, pg_url, bg=6.0)
    faults = Faults()
    try:
        assert await kill_holder(cluster, {tid}, faults)
        await wait_until(lambda: root_finished(cluster, tid, spec), 10 * LEASE + 6.0)
        outcome = await _outcome(cluster, tid, spec, faults)
    finally:
        await cluster.close()
    assert_invariants(outcome)
    assert_all_done(outcome)
    assert outcome.lost_jobs() == 1, fail(outcome, ["the job was not recorded lost"])


async def test_killed_holder_job_stops_writing(tmp_path: Path, pg_url: str) -> None:
    """SIGKILL the holder, then a message wakes the root on another worker,
    which records the job lost. The job's process must be gone too: its
    side effect may not land after the log says lost."""
    cluster, tid, spec, _pid = await _one_job(tmp_path, pg_url, bg=4.0)
    spec.pings = 1
    faults = Faults()
    try:
        assert await kill_holder(cluster, {tid}, faults)
        await cluster.client.send(tid, "CLUSTER-PING r00 0", dedupe_key="ping-r00-0")
        await wait_until(lambda: root_finished(cluster, tid, spec), 10 * LEASE)
        await asyncio.sleep(5.0)  # longer than the job had left to run
        outcome = await _outcome(cluster, tid, spec, faults)
    finally:
        await cluster.close()
    assert_invariants(outcome)
    assert_all_done(outcome)
    assert outcome.lost_jobs() == 1, fail(outcome, ["the job was not recorded lost"])
    assert not any(line.startswith("r00-bg ") for line in outcome.journal), fail(
        outcome, ["the job of a dead worker wrote after the log said lost"])


async def test_frozen_holder_job_not_reported_twice(tmp_path: Path, pg_url: str) -> None:
    """SIGSTOP the holder past its hold, wake the root with a message (the
    job is recorded lost elsewhere), then SIGCONT: the job's end must not
    reach the task after the log called it lost."""
    cluster, tid, spec, pid = await _one_job(tmp_path, pg_url, bg=4.0)
    spec.pings = 1
    faults = Faults()
    try:
        i = cluster.index_of(pid)
        assert i is not None
        cluster.pause(i)
        faults.stops += 1
        await asyncio.sleep(2 * LEASE + 1.0)  # the hold runs out
        await cluster.client.send(tid, "CLUSTER-PING r00 0", dedupe_key="ping-r00-0")

        async def lost() -> bool:
            records = await cluster.client.events(tid)
            return any(k == "lost" for marks in jobs_of(records).values() for _s, k in marks)

        assert await wait_until(lost, 10 * LEASE), "no other worker recorded the job lost"
        cluster.resume(pid)
        await asyncio.sleep(6.0)  # the job finishes and posts its end
        await wait_until(lambda: root_finished(cluster, tid, spec), 10 * LEASE)
        outcome = await _outcome(cluster, tid, spec, faults)
    finally:
        await cluster.close()
    assert_invariants(outcome)
    assert_all_done(outcome)
    late = lost_jobs_ended_late(outcome.logs[tid])
    assert not late, fail(outcome, late)
    assert not any(line.startswith("r00-bg ") for line in outcome.journal), fail(
        outcome, ["the job of a frozen worker wrote after the log said lost"])


#: How long the jobs of the two-workers scenario run: past the checks.
LONG_JOB = 20.0


def _finished(records: list[Any], call_id: str) -> str | None:
    """The text of call ``call_id``'s result in ``records``, if it has one."""
    return next((r.event.result.text for r in records if isinstance(r.event, ToolFinished)
                 and r.event.result.call_id == call_id), None)


async def test_long_jobs_of_one_tree_on_two_workers(tmp_path: Path, pg_url: str) -> None:
    """A root starts a sub-agent, which another worker may claim, then both
    start a long background job. A tree has one holding worker, so the
    second job is refused (the model is told) and the root still takes a
    message while the first job runs: its tree never has two holders that
    each keep it from the other."""
    cluster = Cluster(tmp_path, pg_url, workers=2, lease=LEASE)
    await cluster.start()
    specs = {f"r{i:02d}": Spec(id=f"r{i:02d}", effects=1, agent=True, agent_first=True,
                               lookup=1.0, bg=LONG_JOB, child_bg=LONG_JOB, pings=1)
             for i in range(8)}
    faults = Faults()
    try:
        roots = {sid: await cluster.create(spec) for sid, spec in specs.items()}

        async def both_jobs_asked() -> bool:
            for sid, tid in roots.items():
                tree = await cluster.tree(tid)
                children = [r.task_id for r in tree if r.parent_id == tid]
                if _finished(await cluster.client.events(tid), f"bg-{sid}") is None \
                        or not children or _finished(await cluster.client.events(children[0]),
                                                     f"cbg-{sid}") is None:
                    return False
            return True

        assert await wait_until(both_jobs_asked, 30), "the jobs were never started"
        for sid, tid in roots.items():
            await cluster.client.send(tid, f"CLUSTER-PING {sid} 0", dedupe_key=f"ping-{sid}-0")

        async def pings_taken() -> bool:
            for sid, tid in roots.items():
                if f"CLUSTER-PING {sid} 0" not in taken_texts(await cluster.client.events(tid)):
                    return False
            return True

        taken = await wait_until(pings_taken, LONG_JOB / 2)
        await wait_until(lambda: _all_finished(cluster, roots, specs), LONG_JOB + 30)
        cluster.stop_workers()
        outcome = await collect(cluster, seed=0, specs=specs, roots=roots,
                                pings={s: 1 for s in specs}, watch=EpochWatch(), faults=faults)
    finally:
        await cluster.close()
    refused = [f"{tid}: {r.event.result.call_id}" for tid, records in outcome.logs.items()
               for r in records if isinstance(r.event, ToolFinished)
               and HELD_ELSEWHERE in r.event.result.text]
    assert taken, fail(outcome, ["a root held by two workers took no message while its "
                                 "jobs ran"])
    assert refused, fail(outcome, ["no sub-agent ran on another worker than its root; rerun"])
    assert_invariants(outcome)
    assert_all_done(outcome)


async def _all_finished(cluster: Cluster, roots: dict[str, str], specs: dict[str, Spec]) -> bool:
    done = await asyncio.gather(*(root_finished(cluster, roots[s], spec)
                                  for s, spec in specs.items()))
    return all(done)


async def test_a_saved_output_is_read_back_on_another_worker(tmp_path: Path,
                                                              pg_url: str) -> None:
    """A long ``Bash`` output is saved for the model; every worker is then
    killed, and the task's next turn, on a worker that never ran it, reads
    the saved file back through ``Read``, with no approval in default mode."""
    cluster = Cluster(tmp_path, pg_url, workers=2, lease=LEASE)
    await cluster.start()
    try:
        tid = await cluster.client.create(
            SPILL_AGENT, 'CLUSTER-SPILL {"id": "s1", "lines": 4000}',
            config={"cwd": str(cluster.work)})

        async def replied(prefix: str) -> bool:
            reply = last_reply(await cluster.client.events(tid)) or ""
            row = await cluster.client.get(tid)
            return reply.startswith(prefix) and row.inbox_read_seq == row.inbox_seq \
                and row.status == "waiting"

        assert await wait_until(lambda: replied("SPILLED s1"), 60), \
            await cluster.client.events(tid)
        ran_on = {p.pid for p in cluster.procs}
        for i in range(len(cluster.procs)):
            cluster.kill(i)
        assert not ran_on & {p.pid for p in cluster.procs}
        await cluster.client.send(tid, "CLUSTER-SPILL-READ s1")
        assert await wait_until(lambda: replied("SPILL-READ s1"), 60), \
            await cluster.client.events(tid)
        records = await cluster.client.events(tid)
        assert last_reply(records) == "SPILL-READ s1 ok", "\n".join(
            str(r.event)[:400] for r in records if isinstance(r.event, ToolFinished))
        saved = [r.event.result.text for r in records if isinstance(r.event, ToolFinished)]
        assert "Full output saved to: " + str(cluster.home / "tool-results" / tid) in saved[0]
    finally:
        await cluster.close()
