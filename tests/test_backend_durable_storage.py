"""Durable storage — the HostConfig sqlite triple survives restarts.

D3: the SDK's HostConfig accepts an external ``(event_log, content_store,
dispatcher)`` triple so a product backend can persist conversations. This pins
the host-side material (``noeta.agent.host.storage.open_sqlite_storage``) + its
wiring through ``serve_backend`` (the ``NOETA_AGENT_SQLITE`` knob): a task driven
on one process is enumerable + foldable after a fresh open over the same file.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.host.storage import open_sqlite_storage
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import HostConfig, Options
from noeta.testing.fake_llm import FakeLLMProvider


def _fake(n: int = 4) -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
            )
            for _ in range(n)
        ]
    )


def _opts() -> Options:
    return Options(
        system_prompt="finish",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )


def _host_config(db: str) -> tuple[HostConfig, Any]:
    (event_log, content_store, dispatcher), close = open_sqlite_storage(db)
    return (
        HostConfig(
            event_log=event_log,
            content_store=content_store,
            dispatcher=dispatcher,
        ),
        close,
    )


def test_sqlite_triple_persists_task_across_rooms(tmp_path: Path) -> None:
    db = str(tmp_path / "noeta.db")
    ws = tmp_path / "ws"
    ws.mkdir()

    # Room A drives one conversation, then fully closes its storage.
    hc_a, close_a = _host_config(db)
    room_a = EngineRoom(_opts(), provider=_fake(), workspace_dir=ws, host_config=hc_a)
    task_id = room_a.start(goal="persist me")
    room_a.shutdown()
    close_a()

    # Room B opens the SAME sqlite file fresh — the task is enumerable + foldable.
    hc_b, close_b = _host_config(db)
    room_b = EngineRoom(_opts(), provider=_fake(), workspace_dir=ws, host_config=hc_b)
    try:
        streams = [getattr(s, "task_id", None) for s in room_b.task_streams()]
        assert task_id in streams
        events = room_b.events(task_id)
        assert events, "the persisted stream replays after a fresh open"
        assert any(e.type == "TaskCreated" for e in events)
    finally:
        room_b.shutdown()
        close_b()


def _get_tasks(host: str, port: int) -> list[dict[str, Any]]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/tasks")
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    return body


def test_serve_backend_sqlite_session_list_survives_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "noeta.db")
    ws = tmp_path / "ws"
    ws.mkdir()
    config = BackendConfig(host="127.0.0.1", port=0, workspace_dir=ws, sqlite_path=db)

    # First boot: drive a conversation over HTTP, confirm it lists.
    server, _url, shutdown = serve_backend(config, provider=_fake())
    host, port = server.server_address[:2]
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/tasks",
            body=json.dumps({"goal": "durable goal"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 202
        task_id = json.loads(resp.read())["task_id"]
        conn.close()
        rows = _get_tasks(host, port)
        assert [r["task_id"] for r in rows] == [task_id]
        assert rows[0]["title"] == "durable goal"
    finally:
        shutdown()
    assert Path(db).exists()

    # Second boot over the same file: the session survives the restart.
    server2, _url2, shutdown2 = serve_backend(config, provider=_fake())
    host2, port2 = server2.server_address[:2]
    try:
        rows = _get_tasks(host2, port2)
        assert [r["task_id"] for r in rows] == [task_id]
        assert rows[0]["title"] == "durable goal"
    finally:
        shutdown2()


def test_no_sqlite_path_is_in_memory_default(tmp_path: Path) -> None:
    # No sqlite_path ⇒ the SDK's in-memory default; a fresh boot starts empty.
    ws = tmp_path / "ws"
    ws.mkdir()
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=ws), provider=_fake()
    )
    host, port = server.server_address[:2]
    try:
        assert _get_tasks(host, port) == []
    finally:
        shutdown()
