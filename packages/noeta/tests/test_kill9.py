"""Several worker processes on one sqlite file, killed with SIGKILL at random
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

import pytest

from noeta.client import Client
from noeta.events import ToolFinished, ToolStarted
from noeta.runner import INTERRUPTED_CALL_ERROR
from noeta.store.sqlite import SqliteStore

WORKER = str(Path(__file__).parent / "procs" / "worker.py")
TASKS = int(os.environ.get("NOETA_KILL9_TASKS", "60"))
ROUNDS_SEED = int(os.environ.get("NOETA_KILL9_SEED", "0")) or None


def _spawn(db: str, journal: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, WORKER, db, journal],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def test_kill9_workers(tmp_path: Path) -> None:
    rng = random.Random(ROUNDS_SEED)
    db, journal = str(tmp_path / "t.db"), str(tmp_path / "journal")
    store = SqliteStore(db)
    client = Client(store)
    ids = [await client.create("a", f"task {i}") for i in range(TASKS)]
    procs = [_spawn(db, journal) for _ in range(3)]
    kills = 0
    try:
        for _ in range(600):
            rows = [await client.get(t) for t in ids]
            if all(r.status == "waiting" for r in rows):
                break
            await asyncio.sleep(rng.uniform(0.05, 0.25))
            victim = rng.randrange(len(procs))
            procs[victim].send_signal(signal.SIGKILL)
            procs[victim].wait()
            kills += 1
            procs[victim] = _spawn(db, journal)
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
    print(f"kills={kills} interrupted={interrupted} runs={sum(runs.values())}")
    await store.close()
