"""Several worker processes on one sqlite file (or one Postgres schema), killed with SIGKILL at random
moments and restarted: every task still finishes, no tool step runs twice,
and every started call is answered."""

from __future__ import annotations

import asyncio
import os
import random
import signal
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ToolFinished, ToolStarted
from noeta.core.runner import INTERRUPTED_CALL_ERROR

WORKER = str(Path(__file__).parent / "procs" / "worker.py")
TASKS = int(os.environ.get("NOETA_KILL9_TASKS", "60"))
ROUNDS_SEED = int(os.environ.get("NOETA_KILL9_SEED", "0")) or None


def _spawn(args: list[str], stall: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, WORKER, *args],
                            env={**os.environ, "NOETA_KILL9_STALL": stall},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_kill9_workers(tmp_path: Path, backend: str,
                             request: pytest.FixtureRequest) -> None:
    rng = random.Random(ROUNDS_SEED)
    journal = str(tmp_path / "journal")
    store: Any
    if backend == "postgres":
        pg = request.getfixturevalue("pg")
        store, args = pg.store(), [pg.dsn, journal, pg.schema]
    else:
        db = str(tmp_path / "t.db")
        store, args = SqliteStore(db), [db, journal]
    client = Client(store)
    ids = [await client.create("a", f"task {i}") for i in range(TASKS)]
    # One tool run stalls until killed, so at least one kill surely lands
    # mid-call whatever the random kills hit.
    stall = str(tmp_path / "stall")
    Path(stall).touch()
    procs = [_spawn(args, stall) for _ in range(3)]
    kills = 0
    try:
        for _ in range(600):
            if os.path.exists(stall + ".pid"):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("no tool run stalled")
        pid = int(Path(stall + ".pid").read_text())
        [victim] = [i for i, p in enumerate(procs) if p.pid == pid]
        procs[victim].send_signal(signal.SIGKILL)
        procs[victim].wait()
        kills += 1
        procs[victim] = _spawn(args, stall)
        for _ in range(600):
            rows = [await client.get(t) for t in ids]
            if all(r.status == "waiting" for r in rows):
                break
            await asyncio.sleep(rng.uniform(0.05, 0.25))
            victim = rng.randrange(len(procs))
            procs[victim].send_signal(signal.SIGKILL)
            procs[victim].wait()
            kills += 1
            procs[victim] = _spawn(args, stall)
        else:
            pytest.fail("tasks did not all finish")
    finally:
        for p in procs:
            p.kill()
            p.wait()

    assert kills > 5
    interrupted = 0
    runs = Counter(Path(journal).read_text().splitlines())
    assert max(runs.values()) == 1, [k for k, v in runs.items() if v > 1]
    for t in ids:
        events = [r.event for r in await client.events(t)]
        started = [e.call.id for e in events if isinstance(e, ToolStarted)]
        finished = {e.result.call_id for e in events if isinstance(e, ToolFinished)}
        interrupted += sum(isinstance(e, ToolFinished) and e.result.text == INTERRUPTED_CALL_ERROR
                           for e in events)
        assert len(started) == len(set(started)) and set(started) <= finished
        row = await client.get(t)
        assert row.inbox_read_seq == row.inbox_seq
    # The run must actually have exercised recovery, not just raced past it.
    assert interrupted > 0, f"no interrupted call in {kills} kills"
    print(f"{backend}: kills={kills} interrupted={interrupted} runs={sum(runs.values())}")
    await store.close()
