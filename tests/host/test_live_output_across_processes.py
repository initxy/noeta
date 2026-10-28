"""Live model output reaches a front end attached to a server that does not
run the task: on Postgres a worker in another process publishes it through
the store; on sqlite it stays in its process and only the recorded events
travel, without an error."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")

from noeta.backends.stores.sqlite import SqliteStore  # noqa: E402
from noeta.core.agent import Agent  # noqa: E402
from noeta.core.events import ModelReplied, Waiting  # noqa: E402
from noeta.core.types import Delta  # noqa: E402
from noeta.host.http import create_app  # noqa: E402
from noeta.host.local import LocalApp  # noqa: E402
from noeta.host.remote import RemoteClient  # noqa: E402
from noeta.testing import ScriptedProvider, reply  # noqa: E402
from tests.conftest import Pg  # noqa: E402

WORKER = str(Path(__file__).parent / "procs" / "delta_worker.py")


async def _serve(front: LocalApp) -> AsyncIterator[str]:
    api = create_app(front.client, token="t", deltas=front.deltas)
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    await serving


async def _watch(url: str) -> tuple[list[str], str]:
    client = RemoteClient(url, token="t")
    seen: list[str] = []
    client.on_delta = lambda _t, d: seen.append(d.text)
    tid = await client.create("main", "hi")
    replied = ""
    async for rec in client.watch(tid):
        if isinstance(rec.event, ModelReplied):
            replied = rec.event.response.message.text
        if isinstance(rec.event, Waiting) and rec.event.on == "input":
            break
    await client.close()
    return seen, replied


async def test_deltas_of_a_worker_elsewhere_reach_the_watch(pg: Pg) -> None:
    front = LocalApp(pg.store(), [], ScriptedProvider())
    await front.start(worker=False)  # this server runs no task itself
    hearing = front.deltas(lambda _t, _d: None)
    worker = subprocess.Popen([sys.executable, WORKER, pg.dsn, pg.schema],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        await asyncio.sleep(0.3)  # the front's LISTEN is up
        async for url in _serve(front):
            seen, replied = await asyncio.wait_for(_watch(url), 30)
            assert replied == "hello from another machine"
            assert "".join(seen) == replied
    finally:
        worker.kill()
        worker.wait()
        hearing()
        await front.stop()


async def test_on_sqlite_live_output_stays_in_its_process(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")
    provider = ScriptedProvider([reply("only events travel")])
    worker = LocalApp(SqliteStore(db), [Agent(name="main", model="m")], provider,
                      env_for=lambda _s: None)
    front = LocalApp(SqliteStore(db), [], ScriptedProvider())
    await worker.start()
    await front.start(worker=False)
    here: list[Any] = []
    worker.deltas(lambda _t, d: here.append(d))
    try:
        async for url in _serve(front):
            seen, replied = await asyncio.wait_for(_watch(url), 30)
            assert replied == "only events travel" and seen == []
            assert [d.text for d in here if isinstance(d, Delta)] == ["only events travel"]
    finally:
        await front.stop()
        await worker.stop()


def test_long_output_is_cut_under_the_notification_limit() -> None:
    pytest.importorskip("psycopg")
    import msgspec

    from noeta.backends.stores.postgres import _DELTA_PAYLOAD_BYTES, _delta_payloads

    text = "日本語の文" * 3000 + "\u0000end"
    payloads = _delta_payloads([("t1", Delta(kind="text", text=text, call_seq=7)),
                                ("t2", Delta(kind="thinking", text="short"))])
    assert len(payloads) > 1
    assert all(len(p.encode()) < _DELTA_PAYLOAD_BYTES + 100 for p in payloads)
    back = [item for p in payloads
            for item in msgspec.json.decode(p, type=list[tuple[str, Delta]])]
    assert "".join(d.text for t, d in back if t == "t1") == text
    assert {d.call_seq for t, d in back if t == "t1"} == {7}
    assert back[-1] == ("t2", Delta(kind="thinking", text="short"))
