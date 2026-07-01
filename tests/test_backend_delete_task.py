"""Backend session-delete acceptance — DELETE /tasks/{id}.

The thin backend has no independent session entity (a conversation IS a Task),
so "delete the session" purges the task's persisted stream. Unlike the command
verbs (202 + the change lands via the stream), a delete purges the stream
itself, so it answers synchronously: 200 with the purged ids, 409 when a task
in the tree is actively running, 404 when the root is unknown.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider


def _provider(n: int = 6) -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=f"reply-{i}")],
                usage=Usage(uncached=1, output=1),
            )
            for i in range(n)
        ]
    )


def _room(workspace: Path) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="finish each turn",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=_provider(),
        workspace_dir=workspace,
    )


def _req(host: str, port: int, method: str, path: str) -> tuple[int, Any]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, (json.loads(data) if data else None)


def test_delete_purges_session(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        t1 = room.start(goal="alpha conversation")
        t2 = room.start(goal="beta conversation")
        _status, body = _req(host, port, "GET", "/tasks")
        assert {r["task_id"] for r in body} == {t1, t2}

        status, body = _req(host, port, "DELETE", f"/tasks/{t1}")
        assert status == 200, body
        assert body["ok"] is True and body["deleted"] == [t1]

        # Purged from the session list + its stream is gone; the sibling stays.
        _status, body = _req(host, port, "GET", "/tasks")
        assert {r["task_id"] for r in body} == {t2}
        assert room.events(t1) == []
        assert room.events(t2)
    finally:
        shutdown()


def test_delete_unknown_returns_404(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        status, body = _req(host, port, "DELETE", "/tasks/does-not-exist")
        assert status == 404
        assert body["ok"] is False and body["reason"] == "not_found"
    finally:
        shutdown()


def test_delete_running_returns_409(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        t1 = room.start(goal="alpha conversation")
        # Simulate an in-flight worker: the dispatcher reports an active lease on
        # t1, so the purge must refuse rather than race the running turn.
        room._client._host.dispatcher.has_active_lease = (  # type: ignore[attr-defined]
            lambda tid: tid == t1
        )
        status, body = _req(host, port, "DELETE", f"/tasks/{t1}")
        assert status == 409
        assert body["ok"] is False and body["reason"] == "running"
        assert room.events(t1)  # not purged
    finally:
        shutdown()
