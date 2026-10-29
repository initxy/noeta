"""The streaming handlers of ``noeta serve``: ``/tasks/{id}/watch`` as
server-sent events, and Claude Code's headless protocol at ``/stream-json``
over a POST body or a WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import msgspec
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from noeta.core.errors import UnknownTask
from noeta.core.types import Delta
from noeta.host._http_guard import Guard, Refused, scope

_enc = msgspec.json.Encoder()
log = logging.getLogger("noeta.host.http")


def _options(query: Any, *, ask_client: bool) -> Any:
    from noeta.host.stream_json import HeadlessOptions

    def flag(name: str) -> bool:
        return str(query.get(name, "")).lower() in ("1", "true", "yes")

    max_turns = query.get("max_turns")
    return HeadlessOptions(
        agent=query.get("agent") or "main", model=query.get("model") or "",
        cwd=query.get("cwd") or "", permission_mode=query.get("permission_mode") or None,
        max_turns=int(max_turns) if max_turns else None,
        include_partial_messages=flag("include_partial_messages"),
        replay_user_messages=flag("replay_user_messages"),
        ask_client=ask_client, resume=query.get("resume") or None, fork=flag("fork"))


class StreamRoutes:
    """The streaming handlers; ``deltas`` (``LocalApp.deltas`` when workers
    run in this process) adds live output to a watch."""

    def __init__(self, guard: Guard, deltas: Any) -> None:
        self._guard = guard
        self._deltas = deltas

    async def stream_post(self, request: Request) -> Response:
        from noeta.host.stream_json import StreamJson, drive

        options = _options(request.query_params, ask_client=False)
        scope(request).guarded.check_config({"permission_mode": options.permission_mode})
        options.cwd = scope(request).guarded.check_cwd(options.cwd)
        lines = [json.loads(x) for x in (await request.body()).decode().splitlines()
                 if x.strip()]
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def emit(message: dict[str, Any]) -> None:
            queue.put_nowait(json.dumps(message, ensure_ascii=False).encode() + b"\n")

        async def given() -> AsyncIterator[dict[str, Any]]:
            for line in lines:
                yield line

        async def run() -> None:
            try:
                await drive(StreamJson(scope(request).front, options, emit,
                                       tenant=scope(request).tenant),
                            lines=given())
            finally:
                queue.put_nowait(None)

        async def body() -> AsyncIterator[bytes]:
            running = asyncio.create_task(run())
            try:
                while (chunk := await queue.get()) is not None:
                    yield chunk
            finally:
                running.cancel()
                with contextlib.suppress(BaseException):
                    await running

        return StreamingResponse(body(), media_type="application/x-ndjson")

    async def stream_socket(self, ws: WebSocket) -> None:
        from noeta.host.stream_json import StreamJson, drive

        if not self._guard.same_site(ws.headers):
            await ws.close(code=4403)
            return
        who = self._guard.tenant(ws.headers, ws.query_params)
        if who is None:
            await ws.close(code=4401)
            return
        mine = self._guard.scope_for(who)
        options = _options(ws.query_params, ask_client=True)
        try:
            mine.guarded.check_config({"permission_mode": options.permission_mode})
            options.cwd = mine.guarded.check_cwd(options.cwd)
        except Refused:
            await ws.close(code=4403)
            return
        await ws.accept()

        async def emit(message: dict[str, Any]) -> None:
            await ws.send_text(json.dumps(message, ensure_ascii=False))

        async def lines() -> AsyncIterator[dict[str, Any]]:
            with contextlib.suppress(WebSocketDisconnect):
                while True:
                    value = json.loads(await ws.receive_text())
                    if isinstance(value, dict):
                        yield value

        try:
            await drive(StreamJson(mine.front, options, emit, tenant=who), lines=lines())
        except UnknownTask as exc:
            await ws.send_text(json.dumps({"type": "error", "error": f"unknown task {exc}"}))
        with contextlib.suppress(Exception):
            await ws.close()

    async def watch(self, request: Request) -> Response:
        task_id = request.path_params["task_id"]
        await scope(request).client.get(task_id)
        after = int(request.query_params.get("after", "0"))
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        deltas = self._deltas

        def on_delta(tid: str, delta: Delta) -> None:
            if tid == task_id:
                queue.put_nowait(b"event: delta\ndata: " + _enc.encode(delta) + b"\n\n")

        async def follow() -> None:
            end: dict[str, Any] = {}
            try:
                async for rec in scope(request).client.watch(task_id, after):
                    queue.put_nowait(b"event: event\ndata: " + _enc.encode(rec) + b"\n\n")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the stream must end, not hang
                log.warning("watch %s failed: %s", task_id, exc)
                end = {"error": f"{type(exc).__name__}: {exc}"}
            queue.put_nowait(b"event: end\ndata: " + _enc.encode(end) + b"\n\n")

        async def stream() -> AsyncIterator[bytes]:
            unsubscribe = deltas(on_delta) if deltas is not None else None
            following = asyncio.create_task(follow())
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(queue.get(), 15)
                    except TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    yield chunk
                    if chunk.startswith(b"event: end"):
                        return
            finally:
                following.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await following
                if unsubscribe is not None:
                    unsubscribe()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})
