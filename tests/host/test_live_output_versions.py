"""Live output across versions: a reader skips a delta kind it does not
know (a newer worker's) and keeps the rest of the stream; late progress
never reopens a finished call."""

from __future__ import annotations

import json
from typing import Any

import httpx
import msgspec

from noeta.backends.stores.postgres_feed import ChangeFeed
from noeta.core.events import Recorded, TaskEnded
from noeta.core.types import Delta
from noeta.host._acp_updates import PromptTurn
from noeta.host.remote import RemoteClient


def _future(text: str) -> dict[str, Any]:
    return {"kind": "from-a-newer-version", "text": text}


async def test_a_remote_watch_skips_a_delta_kind_it_does_not_know() -> None:
    ended = msgspec.json.encode(Recorded(seq=1, at=0.0, event=TaskEnded(status="done"))).decode()
    body = "".join([
        f"event: delta\ndata: {json.dumps(_future('x'))}\n\n",
        f"event: delta\ndata: {msgspec.json.encode(Delta(kind='text', text='hi')).decode()}\n\n",
        f"event: event\ndata: {ended}\n\n",
        "event: end\ndata: {}\n\n"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = RemoteClient("http://noeta", transport=httpx.MockTransport(handler))
    seen: list[Delta] = []
    client.on_delta = lambda _t, d: seen.append(d)
    events = [r async for r in client.watch("t1")]
    assert [d.text for d in seen] == ["hi"] and len(events) == 1
    await client.http.aclose()


def test_a_postgres_feed_keeps_the_kinds_it_knows_of_a_batch() -> None:
    feed = ChangeFeed("postgresql://unused", "noeta", connect_timeout=1,
                      listen_check_interval=1)
    seen: list[tuple[str, Delta]] = []
    feed._delta_listeners.append(lambda t, d: seen.append((t, d)))
    payload = json.dumps([["t1", _future("x")],
                          ["t1", msgspec.to_builtins(Delta(kind="text", text="kept"))]])
    feed._remote_deltas(0, payload)
    assert [(t, d.text) for t, d in seen] == [("t1", "kept")]


def test_acp_progress_after_the_result_is_dropped() -> None:
    turn = PromptTurn("s1")
    turn.finished.add("c1")
    turn.on_delta("s1", Delta(kind="tool", text="late", call_id="c1", tool="Bash"))
    assert turn.out.empty()
