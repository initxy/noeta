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

from tests.cluster.harness import (
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
from tests.cluster.invariants import EpochWatch, jobs_of, lost_jobs_ended_late

pytestmark = pytest.mark.cluster

TASKS = int(os.environ.get("NOETA_CLUSTER_TASKS", "24"))
SECONDS = float(os.environ.get("NOETA_CLUSTER_SECONDS", "20"))
DRAIN = float(os.environ.get("NOETA_CLUSTER_DRAIN", "90"))
LEASE = 1.0


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
                    wake=rng.random() < 0.5)
        if bg and i % 2 == 0:
            spec.bg = round(rng.uniform(1.0, 3.0), 1)
        if bg and i % 4 == 0:
            # a job and nothing else: once the root waits, only the job's
            # end (or its loss) can wake it
            spec.effects, spec.agent, spec.wake, spec.bg = 1, False, False, 4.0
        else:
            spec.pings = rng.choice([0, 0, 1, 2, 3])
        specs[sid] = spec
    return specs


async def run_cluster(tmp_path: Path, url: str, seed: int, specs: dict[str, Spec], *,
                      seconds: float, targets: set[str] | None = None,
                      workers: int = 3, drain: float = DRAIN, faulty: Any = chaos) -> Outcome:
    """Create the roots, run the faults and the pings for ``seconds``, then
    let the workers drain until every root is done or ``DRAIN`` passes."""
    rng = random.Random(seed)
    cluster = Cluster(tmp_path, url, workers=workers, lease=LEASE)
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
          f"journal={len(outcome.journal)} samples={watch.samples}")
    return outcome


def assert_invariants(outcome: Outcome) -> None:
    """The invariants no known gap excuses."""
    problems = outcome.log_violations() + outcome.effect_violations() + outcome.watch.violations
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


# ------------------------------------------------------------------ one gap each


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
