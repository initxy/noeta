"""The client API against a ``noeta serve`` server: the same methods as
``noeta.core.client.Client``, over HTTP. ``RemoteApp`` is the remote stand-in for
``LocalApp`` (what the TUI runs on with ``--server``)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

import httpx
import msgspec

from noeta.core.client import ClientAPI, Input
from noeta.core.commands import Typed
from noeta.core.errors import NoetaError, UnknownTask
from noeta.core.events import Recorded
from noeta.core.extension import Command, CommandContext
from noeta.core.store import Status, TaskRow
from noeta.core.types import Delta
from noeta.host.app import AppAPI, archive_task

#: Reconnects in a row (without an event in between) before ``watch`` gives up.
WATCH_RETRIES = 5


def _body(value: Any) -> Any:
    return value if value is None or isinstance(value, str) else msgspec.to_builtins(value)


class RemoteClient:
    def __init__(self, url: str, *, token: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.AsyncClient(base_url=url.rstrip("/"), headers=headers,
                                      timeout=httpx.Timeout(30, read=None), transport=transport)
        #: Called with each live output fragment seen while watching.
        self.on_delta: Callable[[str, Delta], None] | None = None

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self.http.request(method, path, **kw)
        if resp.status_code == 404:
            raise UnknownTask(path)
        resp.raise_for_status()
        return resp.json()

    async def create(self, agent: str, input: Input | None = None, *,
                     task_id: str | None = None, config: dict[str, Any] | None = None,
                     queue: str = "default", dedupe_key: str | None = None,
                     history_from: str | None = None, history_through: int | None = None) -> str:
        body = {"agent": agent, "input": _body(input), "config": config or {}, "queue": queue,
                "dedupe_key": dedupe_key, "history_from": history_from,
                "history_through": history_through}
        return str((await self._call("POST", "/tasks", json=body))["task_id"])

    async def send(self, task_id: str, input: Input, *,
                   when: Literal["now", "next_turn"] = "next_turn",
                   dedupe_key: str | None = None) -> int:
        body = {"input": _body(input), "when": when, "dedupe_key": dedupe_key}
        return int((await self._call("POST", f"/tasks/{task_id}/messages", json=body))["inbox_seq"])

    async def answer(self, task_id: str, request_id: str,
                     decision: Literal["allow", "deny"] | None = None, *,
                     always: bool = False, data: Any = None) -> int:
        got = await self._call("POST", f"/tasks/{task_id}/answers", json={
            "request_id": request_id, "decision": decision, "always": always, "data": data})
        return int(got.get("inbox_seq", 0))

    async def signal(self, task_id: str, name: str, data: Any = None, *,
                     dedupe_key: str | None = None, wake: bool = True) -> int:
        got = await self._call("POST", f"/tasks/{task_id}/signals", json={
            "name": name, "data": data, "dedupe_key": dedupe_key, "wake": wake})
        return int(got.get("inbox_seq", 0))

    async def cancel(self, task_id: str, reason: str = "", *, turn_only: bool = False) -> None:
        await self._call("POST", f"/tasks/{task_id}/cancel",
                         json={"reason": reason, "turn_only": turn_only})

    async def get(self, task_id: str) -> TaskRow:
        return msgspec.convert(await self._call("GET", f"/tasks/{task_id}"), TaskRow)

    async def tasks(self, *, parent_id: str | None = None, status: Status | None = None,
                    limit: int = 100, roots_only: bool = False) -> list[TaskRow]:
        params = {k: v for k, v in {"parent_id": parent_id, "status": status,
                                    "limit": limit,
                                    "roots_only": "true" if roots_only else None}.items()
                  if v is not None}
        return msgspec.convert(await self._call("GET", "/tasks", params=params), list[TaskRow])

    async def events(self, task_id: str, after: int = 0) -> list[Recorded]:
        data = await self._call("GET", f"/tasks/{task_id}/events", params={"after": after})
        return msgspec.convert(data, list[Recorded])

    async def watch(self, task_id: str, after: int = 0) -> AsyncIterator[Recorded]:
        """The task's events from ``after`` on until the server says the
        stream has ended. A dropped connection is picked up again from the
        last event seen (``WATCH_RETRIES`` times in a row at most)."""
        failures = 0
        while True:
            try:
                async with self.http.stream("GET", f"/tasks/{task_id}/watch",
                                            params={"after": after}) as resp:
                    if resp.status_code == 404:
                        raise UnknownTask(task_id)
                    resp.raise_for_status()
                    kind = ""
                    async for line in resp.aiter_lines():
                        if line.startswith("event: "):
                            kind = line[7:]
                        elif line.startswith("data: "):
                            data = line[6:]
                            if kind == "event":
                                rec = msgspec.json.decode(data, type=Recorded)
                                after, failures = rec.seq, 0
                                yield rec
                            elif kind == "delta" and self.on_delta is not None:
                                try:
                                    delta = msgspec.json.decode(data, type=Delta)
                                except msgspec.ValidationError:
                                    continue  # live output of a kind this version lacks
                                self.on_delta(task_id, delta)
                            elif kind == "end":
                                error = (msgspec.json.decode(data) or {}).get("error")
                                if error:
                                    raise NoetaError(f"the server's watch failed: {error}")
                                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and \
                        exc.response.status_code < 500:
                    raise
                failures += 1
                if failures > WATCH_RETRIES:
                    raise
            else:
                failures += 1  # closed without "end": reconnect
                if failures > WATCH_RETRIES:
                    raise NoetaError(f"the watch of {task_id} kept closing without an end")
            await asyncio.sleep(min(0.2 * 2 ** (failures - 1), 5.0))

    async def context(self, task_id: str) -> dict[str, Any]:
        """What fills the task's next request (``GET /tasks/{id}/context``)."""
        got: dict[str, Any] = await self._call("GET", f"/tasks/{task_id}/context")
        return got

    async def wait(self, task_id: str, *, timeout: float | None = None) -> TaskRow:
        async def settled() -> TaskRow:
            while True:
                row = await self.get(task_id)
                if row.status in ("waiting", "ended"):
                    return row
                await asyncio.sleep(0.2)

        return await asyncio.wait_for(settled(), timeout)

    async def close(self) -> None:
        await self.http.aclose()


def _is_client(client: RemoteClient) -> ClientAPI:
    return client  # mypy checks RemoteClient against ClientAPI here


def _is_app(app: RemoteApp) -> AppAPI:
    return app  # and RemoteApp against AppAPI


class RemoteApp:
    """What a front end runs on (``noeta.host.app.AppAPI``) against a
    remote server; workers run wherever the server's store is. Live output
    arrives with the events of a task being watched."""

    def __init__(self, url: str, *, token: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.client = RemoteClient(url, token=token, transport=transport)
        self._sinks: list[Callable[[str, Delta], None]] = []
        self.client.on_delta = self._fan_out

    def _fan_out(self, task_id: str, delta: Delta) -> None:
        for sink in list(self._sinks):
            sink(task_id, delta)

    def deltas(self, sink: Callable[[str, Delta], None]) -> Callable[[], None]:
        self._sinks.append(sink)
        return lambda: self._sinks.remove(sink) if sink in self._sinks else None

    async def adopt(self, task_id: str) -> None:
        """Workers run on the server's side; nothing to do here."""
        return None

    async def agent_names(self) -> list[str]:
        return sorted(str(a["name"]) for a in await self.client._call("GET", "/agents"))

    async def commands(self, agent: str, ctx: CommandContext) -> list[Command]:
        params = {k: v for k, v in {"agent": agent, "task_id": ctx.task_id,
                                    "cwd": ctx.cwd}.items() if v}
        listed = await self.client._call("GET", "/commands", params=params)
        return [Command(name=c["name"], description=c.get("description") or "",
                        argument_hint=c.get("argument_hint") or "",
                        local=bool(c.get("local"))) for c in listed]

    async def run_command(self, agent: str, ctx: CommandContext, text: str) -> Typed | None:
        got = await self.client._call("POST", "/commands/run", json={
            "text": text, "agent": agent, "task_id": ctx.task_id, "cwd": ctx.cwd})
        return None if got is None else Typed(str(got["text"]), bool(got.get("local")))

    async def summaries(self, limit: int = 50, *,
                        archived: bool | None = None) -> list[dict[str, Any]]:
        """The server's root tasks (``noeta.host.summaries.root_summaries``)."""
        params: dict[str, Any] = {"limit": limit}
        if archived is not None:
            params["archived"] = "true" if archived else "false"
        got: list[dict[str, Any]] = await self.client._call("GET", "/summaries", params=params)
        return got

    async def archive(self, task_id: str, archived: bool = True) -> None:
        await archive_task(self.client, task_id, archived)

    async def files(self, query: str, ctx: CommandContext, limit: int = 30) -> list[str]:
        """Paths under the task's (or ``ctx.cwd``'s) directory on the server
        holding ``query`` (``noeta.host.files.file_matches``)."""
        params = {k: v for k, v in {"q": query, "task_id": ctx.task_id, "cwd": ctx.cwd,
                                    "limit": limit}.items() if v}
        got: list[str] = await self.client._call("GET", "/files", params=params)
        return got

    async def context(self, task_id: str) -> dict[str, Any] | None:
        try:
            return await self.client.context(task_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 501:
                return None
            raise

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()
