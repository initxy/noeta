"""Worker processes on one store, faults, and the run's outcome.

The test drives noeta only from outside: worker processes started as
``noeta worker --lease``, a :class:`~noeta.core.client.Client` on the same
store to create tasks, send messages and read logs, and signals.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from noeta.core import registry
from noeta.core.client import Client
from noeta.core.events import InputTaken, Recorded, TimerFired, ToolFinished, ToolStarted
from noeta.core.store import TaskRow
from noeta.core.tool import MAX_ATTEMPTS
from tests.cluster.invariants import (
    EpochWatch,
    check_journal,
    check_log,
    child_ends,
    job_notices,
    jobs_of,
    jobs_running,
    jobs_untold,
    last_reply,
    taken_texts,
    timers_taken,
)

#: The ``noeta`` command of the environment the test runs in.
NOETA = str(Path(sys.executable).parent / "noeta")
MODEL = "cluster-scripted"
ROOT_AGENT = "cluster"
CHILD_AGENT = "cluster-child"
INTERRUPTED = "outcome is unknown"
#: Workers write fold snapshots this often, so claims under faults start
#: from them (the runs' logs are a few dozen events).
SNAPSHOT_EVERY = 5

ROOT_FILE = """---
name: cluster
description: The cluster test's root agent.
model: inherit
plugins:
  - cluster_crash:
      arm: {arm}
  - shell
  - cluster_effect:
      journal: {journal}
  - cluster_schedule:
      fire_after: {fire_after}
  - subtasks:
      agents: [cluster-child]
      inherit_config: [cwd]
---
The cluster test's scripted root agent.
"""

CHILD_FILE = """---
name: cluster-child
description: The cluster test's sub-agent; calls Effect once.
model: inherit
plugins:
  - shell
  - cluster_effect:
      journal: {journal}
---
The cluster test's scripted sub-agent.
"""


def seed_from_env() -> int:
    raw = os.environ.get("NOETA_CLUSTER_SEED", "")
    return int(raw) if raw else random.SystemRandom().randrange(1, 2**31)


@dataclass
class Spec:
    """One root task's script (see ``noeta_cluster_scripted``)."""

    id: str
    effects: int = 1
    bg: float = 0.0
    agent: bool = False
    wake: bool = False
    pings: int = 0
    lookup: float = 0.0
    child_bg: float = 0.0
    cron: int = 0

    def prompt(self, journal: str) -> str:
        return "CLUSTER-ROOT " + json.dumps({
            "id": self.id, "journal": journal, "effects": self.effects, "bg": self.bg,
            "agent": self.agent, "wake": self.wake, "lookup": self.lookup,
            "child_bg": self.child_bg, "cron": self.cron})


@dataclass
class Faults:
    kills: int = 0
    stops: int = 0
    targeted: int = 0
    log: list[str] = field(default_factory=list)


class Cluster:
    """Worker processes serving one store from a shared directory."""

    def __init__(self, base: Path, url: str, *, workers: int = 3, lease: float = 1.0,
                 fire_after: float = 2.0) -> None:
        self.base = base
        self.url = url
        self.lease = lease
        self.home = base / "home"
        self.work = base / "work"
        self.logs = base / "logs"
        self.journal = str(base / "journal")
        #: Write an ``External`` name here to have the root's worker die
        #: right after taking one (``cluster_crash``).
        self.arm = str(base / "crash-arm")
        for d in (self.home / "agents", self.work, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        (self.home / "agents" / "cluster.md").write_text(
            ROOT_FILE.format(journal=self.journal, fire_after=fire_after, arm=self.arm))
        (self.home / "agents" / "cluster-child.md").write_text(
            CHILD_FILE.format(journal=self.journal))
        models = self.home / "models.json"
        models.write_text(json.dumps({"models": [
            {"id": MODEL, "context_window": 200_000, "max_output_tokens": 8_000}]}))
        self.env = {**os.environ, "NOETA_HOME": str(self.home), "NOETA_MODEL": MODEL,
                    "NOETA_PROVIDER": "cluster_scripted", "NOETA_MODELS_FILE": str(models),
                    "NOETA_STORE": url}
        for key in ("NOETA_BASE_URL", "NOETA_API_KEY", "NOETA_EFFORT"):
            self.env.pop(key, None)
        self.size = workers
        self.procs: list[subprocess.Popen[bytes]] = []
        self.stopped: set[int] = set()
        self.spawned = 0
        self.store: Any = None
        self.client: Client

    # ------------------------------------------------------------ processes

    def _spawn(self) -> subprocess.Popen[bytes]:
        self.spawned += 1
        log = open(self.logs / f"worker-{self.spawned}.log", "wb")
        with log:
            return subprocess.Popen([NOETA, "worker", "--lease", str(self.lease),
                                     "--snapshot-every", str(SNAPSHOT_EVERY)],
                                    cwd=self.work, env=self.env, stdout=log, stderr=log)

    async def start(self) -> None:
        self.store = registry.store(self.url)
        self.client = Client(self.store, poll_interval=0.2)
        await self.store.tasks(limit=1)  # creates the schema before the workers race to
        self.procs = [self._spawn() for _ in range(self.size)]

    def kill(self, i: int, *, respawn: bool = True) -> None:
        p = self.procs[i]
        p.send_signal(signal.SIGKILL)
        p.wait()
        self.stopped.discard(p.pid)
        if respawn:
            self.procs[i] = self._spawn()

    def kill_pid(self, pid: int, *, respawn: bool = True) -> bool:
        for i, p in enumerate(self.procs):
            if p.pid == pid and p.poll() is None:
                self.kill(i, respawn=respawn)
                return True
        return False

    def index_of(self, pid: int) -> int | None:
        return next((i for i, p in enumerate(self.procs) if p.pid == pid), None)

    def pause(self, i: int) -> int:
        pid = self.procs[i].pid
        os.kill(pid, signal.SIGSTOP)
        self.stopped.add(pid)
        return pid

    def resume(self, pid: int) -> None:
        if pid in self.stopped:
            self.stopped.discard(pid)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)

    async def close(self) -> None:
        self.stop_workers()
        if self.store is not None:
            await self.store.close()
            self.store = None

    def stop_workers(self) -> None:
        for pid in list(self.stopped):
            self.resume(pid)
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        for p in self.procs:
            try:
                p.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    # ------------------------------------------------------------ tasks

    async def create(self, spec: Spec) -> str:
        return await self.client.create(ROOT_AGENT, spec.prompt(self.journal),
                                        config={"cwd": str(self.work)},
                                        dedupe_key=f"root-{spec.id}")

    async def ping(self, task_id: str, spec: Spec, j: int, *, rng: random.Random) -> None:
        when = "now" if rng.random() < 0.3 else "next_turn"
        text = f"CLUSTER-PING {spec.id} {j}"
        await self.client.send(task_id, text, when=when, dedupe_key=f"ping-{spec.id}-{j}")
        if rng.random() < 0.3:  # a retried send: the dedupe key makes it one message
            await self.client.send(task_id, text, when=when, dedupe_key=f"ping-{spec.id}-{j}")

    async def rows(self) -> list[TaskRow]:
        return await self.client.tasks(limit=10_000)

    async def tree(self, root: str) -> list[TaskRow]:
        return [r for r in await self.rows() if r.root_id == root]

    def journal_lines(self) -> list[str]:
        path = Path(self.journal)
        return path.read_text().splitlines() if path.exists() else []

    async def running_job_pids(self) -> list[tuple[str, int]]:
        """``(root id, worker pid)`` of every background job some task's log
        shows running, from the job's ``worker`` (``host:pid:random``)."""
        out = []
        for row in await self.rows():
            records = await self.client.events(row.task_id)
            for job, marks in jobs_of(records).items():
                if [k for _s, k in marks] != ["started"]:
                    continue
                for r in records:
                    data = getattr(r.event, "data", None)
                    if isinstance(data, dict) and data.get("job_id") == job \
                            and "worker" in data:
                        out.append((row.root_id, int(str(data["worker"]).split(":")[1])))
        return out


# ------------------------------------------------------------------ outcome


@dataclass
class Outcome:
    """What one run left: rows, logs, journal; and the checks over them."""

    seed: int
    specs: dict[str, Spec]
    roots: dict[str, str]
    pings: dict[str, int]
    rows: list[TaskRow]
    logs: dict[str, list[Recorded]]
    journal: list[str]
    watch: EpochWatch
    faults: Faults
    worker_logs: Path
    #: Tasks the workers left a fold snapshot of.
    snapshots: int = 0

    def settled(self, row: TaskRow) -> bool:
        return row.status in ("waiting", "ended") and row.inbox_read_seq == row.inbox_seq

    def root_done(self, sid: str) -> list[str]:
        """Why root ``sid`` has not finished its script (empty: it has)."""
        tid = self.roots[sid]
        row = next(r for r in self.rows if r.task_id == tid)
        records = self.logs[tid]
        out = []
        if not self.settled(row):
            out.append(f"root {sid} is {row.status} (inbox {row.inbox_read_seq}/{row.inbox_seq})")
        reply = last_reply(records)
        if reply != f"DONE {sid}":
            out.append(f"root {sid} last replied {reply!r}, not 'DONE {sid}'")
        texts = taken_texts(records)
        for j in range(self.pings.get(sid, 0)):
            n = sum(t == f"CLUSTER-PING {sid} {j}" for t in texts)
            if n != 1:
                out.append(f"root {sid}: ping {j} taken {n} times")
        for child in (r for r in self.rows if r.parent_id == tid):
            if child.status != "ended":
                out.append(f"child {child.task_id} of {sid} is {child.status}")
            elif child_ends(records)[child.task_id] != 1:
                out.append(f"child {child.task_id} of {sid}: its end taken "
                           f"{child_ends(records)[child.task_id]} times")
        tree = [r for r in self.rows if r.root_id == tid]
        told = job_notices(records, [j for r in tree for j in jobs_of(self.logs[r.task_id])])
        for row in tree:
            if row.task_id != tid and row.status == "ended":
                # Its log stays as it was when it ended: the root hears of its jobs.
                out += [f"{row.task_id}: job {job} of an ended sub-agent: the root was told "
                        f"{told[job]} times" for job in jobs_of(self.logs[row.task_id])
                        if told[job] != 1]
                continue
            out += jobs_running(row.task_id, self.logs[row.task_id])
            out += jobs_untold(row.task_id, self.logs[row.task_id])
        spec = self.specs[sid]
        if spec.wake and "schedule.wakeup" not in timers_taken(records) \
                and not self.interrupted(tid, f"wake-{sid}"):
            out.append(f"root {sid}: its wake-up never fired")
        if spec.cron:
            out += self.cron_problems(sid)
        return out

    def cron_problems(self, sid: str) -> list[str]:
        """The cron job fired at least ``cron`` times, and never once its
        deletion is in the log (unless creating it had an unknown outcome,
        which leaves no job)."""
        tid, spec = self.roots[sid], self.specs[sid]
        records = self.logs[tid]
        if self.interrupted(tid, f"cron-{sid}"):
            return []
        deleted = next((r.seq for r in records if isinstance(r.event, ToolFinished)
                        and r.event.result.call_id.startswith(f"cdel-{sid}-")
                        and not r.event.result.is_error), None)
        fires = [r.seq for r in records if isinstance(r.event, InputTaken)
                 for i in r.event.items if isinstance(i.body, TimerFired)
                 and i.body.name.startswith("schedule.job:")]
        out = []
        if deleted is None:
            out.append(f"root {sid}: its cron job was never deleted")
        elif any(seq > deleted for seq in fires):
            out.append(f"root {sid}: its cron job fired after it was deleted (seq {deleted})")
        if len(fires) < spec.cron:
            out.append(f"root {sid}: its cron job fired {len(fires)} times, not {spec.cron}")
        return out

    def interrupted(self, tid: str, call_id: str) -> bool:
        return any(isinstance(r.event, ToolFinished) and r.event.result.call_id == call_id
                   and INTERRUPTED in r.event.result.text for r in self.logs[tid])

    def finished_ok(self) -> dict[str, tuple[str, str]]:
        """Calls the logs show finished with a real result: token -> (task, call)."""
        out = {}
        for tid, records in self.logs.items():
            for r in records:
                ev = r.event
                if isinstance(ev, ToolFinished) and not ev.result.is_error \
                        and ev.result.text.startswith("did "):
                    out[ev.result.text[4:]] = (tid, ev.result.call_id)
        return out

    def log_violations(self) -> list[str]:
        out = []
        for row in self.rows:
            out += check_log(row, self.logs[row.task_id], settled=self.settled(row))
        return out

    def effect_violations(self) -> list[str]:
        """Each token at most once; exactly once when its call finished."""
        out = check_journal(self.journal)
        counts = Counter(line.split(" ", 1)[0] for line in self.journal if line.strip())
        for token, (tid, call) in sorted(self.finished_ok().items()):
            if counts[token] != 1:
                out.append(f"{tid} call {call}: finished as done but journalled "
                           f"{counts[token]} times")
        return out

    def lookups(self) -> dict[str, tuple[int, str]]:
        """Each ``Lookup`` call (task:call) -> (its starts, its result)."""
        out = {}
        for tid, records in self.logs.items():
            starts: Counter[str] = Counter(r.event.call.id for r in records
                                           if isinstance(r.event, ToolStarted)
                                           and r.event.call.name == "Lookup")
            for r in records:
                ev = r.event
                if isinstance(ev, ToolFinished) and ev.result.call_id in starts:
                    out[f"{tid}:{ev.result.call_id}"] = (starts[ev.result.call_id],
                                                         ev.result.text)
        return out

    def lookup_violations(self) -> list[str]:
        """A retry-safe call a crash interrupted ran again and finished with
        its real result, unless every attempt it had was cut short."""
        return [f"{key}: {starts} start(s) ended {text!r}"
                for key, (starts, text) in sorted(self.lookups().items())
                if not text.startswith("found ") and starts < MAX_ATTEMPTS]

    def interrupted_calls(self) -> int:
        return sum(isinstance(r.event, ToolFinished) and INTERRUPTED in r.event.result.text
                   for records in self.logs.values() for r in records)

    def lost_jobs(self) -> int:
        return sum(1 for records in self.logs.values()
                   for marks in jobs_of(records).values()
                   for _s, k in marks if k == "lost")

    def describe(self, task_ids: Iterable[str]) -> str:
        """The seed and the event logs of ``task_ids`` (and their trees)."""
        lines = [f"NOETA_CLUSTER_SEED={self.seed}  faults: kills={self.faults.kills} "
                 f"stops={self.faults.stops} targeted={self.faults.targeted}",
                 f"worker logs: {self.worker_logs}"]
        wanted = set(task_ids)
        rows = {r.task_id: r for r in self.rows}
        for tid in list(wanted):
            row = rows.get(tid)
            if row is not None:
                wanted |= {r.task_id for r in self.rows if r.root_id == row.root_id}
        for tid in sorted(wanted)[:6]:
            row = rows.get(tid)
            lines.append(f"--- task {tid} {row.status if row else '?'} "
                         f"epoch={row.owner_epoch if row else '?'} "
                         f"inbox={row.inbox_read_seq if row else '?'}/"
                         f"{row.inbox_seq if row else '?'}")
            for r in self.logs.get(tid, []):
                lines.append(f"  {r.seq:3d} {_brief(r)}")
        return "\n".join(lines)


def _brief(r: Recorded) -> str:
    ev = r.event
    name = type(ev).__name__
    text = repr(ev)
    if hasattr(ev, "response"):
        msg = ev.response.message
        text = msg.text or ", ".join(f"{u.name}#{u.id}" for u in msg.tool_uses)
    elif hasattr(ev, "result"):
        text = f"{ev.result.call_id}: {ev.result.text}"
    elif hasattr(ev, "call"):
        text = f"{ev.call.name}#{ev.call.id}"
    text = " ".join(text.split())
    return f"{name} {text[:200]}"


async def collect(cluster: Cluster, *, seed: int, specs: dict[str, Spec],
                  roots: dict[str, str], pings: dict[str, int], watch: EpochWatch,
                  faults: Faults) -> Outcome:
    rows = await cluster.rows()
    logs = {r.task_id: await cluster.client.events(r.task_id) for r in rows}
    snapshots = [await cluster.store.snapshot(r.task_id) for r in rows]
    return Outcome(seed=seed, specs=specs, roots=roots, pings=pings, rows=rows, logs=logs,
                   journal=cluster.journal_lines(), watch=watch, faults=faults,
                   worker_logs=cluster.logs, snapshots=sum(s is not None for s in snapshots))


async def watch_rows(cluster: Cluster, watch: EpochWatch, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(Exception):
            watch.observe(await cluster.rows())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), 0.15)


async def wait_until(predicate: Any, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ------------------------------------------------------------------ faults


async def chaos(cluster: Cluster, rng: random.Random, seconds: float, faults: Faults, *,
                targets: set[str] | None = None) -> None:
    """For ``seconds``: SIGKILL a random worker (respawned at once), or
    SIGSTOP one for 1.5-3 leases (the zombie whose claims and holds run out
    while it still thinks it owns them), at random moments. With
    ``targets`` (root ids) some faults are aimed: kill the worker holding a
    background job of a target root that is waiting (the first such fault as
    soon as there is one)."""
    end = time.monotonic() + seconds
    paused: dict[int, float] = {}
    while time.monotonic() < end:
        await asyncio.sleep(rng.uniform(0.1, 0.6))
        now = time.monotonic()
        for pid, until in list(paused.items()):
            if now >= until:
                cluster.resume(pid)
                paused.pop(pid)
        roll = rng.random()
        if targets and (roll < 0.3 or not faults.targeted):
            if await kill_holder(cluster, targets, faults):
                continue
            roll = rng.random()
        if roll < 0.6:
            i = rng.randrange(len(cluster.procs))
            paused.pop(cluster.procs[i].pid, None)
            cluster.kill(i)
            faults.kills += 1
            faults.log.append(f"{now:.1f} kill worker {i}")
        elif roll < 0.85 and not paused:
            i = rng.randrange(len(cluster.procs))
            pid = cluster.pause(i)
            paused[pid] = now + rng.uniform(1.5, 3.0) * cluster.lease
            faults.stops += 1
            faults.log.append(f"{now:.1f} stop worker {i}")
    for pid in paused:
        cluster.resume(pid)


async def kill_all(cluster: Cluster, rng: random.Random, seconds: float, faults: Faults, *,
                   targets: set[str] | None = None) -> None:
    """For ``seconds``: every 1.5-3 s SIGKILL every worker at once (each
    respawned), so no worker lives to finish what another started."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await asyncio.sleep(rng.uniform(1.5, 3.0))
        for i in range(len(cluster.procs)):
            cluster.kill(i)
            faults.kills += 1
        faults.log.append(f"{time.monotonic():.1f} kill every worker")


async def kill_holder(cluster: Cluster, targets: set[str], faults: Faults) -> bool:
    """Kill the worker running a background job of a waiting target root."""
    for root, pid in await cluster.running_job_pids():
        if root not in targets:
            continue
        row = await cluster.client.get(root)
        if row.status == "waiting" and cluster.kill_pid(pid):
            faults.targeted += 1
            faults.kills += 1
            faults.log.append(f"{time.monotonic():.1f} kill holder {pid} of {root}")
            return True
    return False


async def send_pings(cluster: Cluster, rng: random.Random, seconds: float,
                     plan: list[tuple[float, str, Spec, int]]) -> None:
    """Send each planned ping ``(at, task id, spec, j)`` at its time."""
    start = time.monotonic()
    for at, tid, spec, j in sorted(plan, key=lambda p: p[0]):
        delay = start + min(at, seconds) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        await cluster.ping(tid, spec, j, rng=rng)


async def root_finished(cluster: Cluster, tid: str, spec: Spec) -> bool:
    """Has root ``tid`` finished its script (as far as rows and its log
    tell; :meth:`Outcome.root_done` gives the reasons when not)?"""
    row = await cluster.client.get(tid)
    if row.status != "waiting" or row.inbox_read_seq != row.inbox_seq:
        return False
    records = await cluster.client.events(tid)
    if last_reply(records) != f"DONE {spec.id}":
        return False
    texts = taken_texts(records)
    if any(f"CLUSTER-PING {spec.id} {j}" not in texts for j in range(spec.pings)):
        return False
    tree = await cluster.tree(tid)
    return all(r.status == "ended" for r in tree if r.parent_id is not None)
