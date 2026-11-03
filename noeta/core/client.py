"""The client API hosts, the TUI and the server use. Everything that is not
the running task talks to it through here: create, send, answer, cancel,
read, watch."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, Union

from noeta.core.errors import UnknownTask
from noeta.core.events import (
    Answer,
    Cancel,
    External,
    Recorded,
    Say,
    TaskCreated,
)
from noeta.core.images import fit_message
from noeta.core.store import NewTask, Post, Status, Store, TaskRow, pin_history
from noeta.core.types import Image, Message, user

Input = Union[str, Message]

#: Config key of a task created with ``history_from``: how its
#: conversation continues the source's, for plugins that tell a session's
#: start (Claude Code's session sources): ``"resume"`` (a channel thread
#: carried on), ``"fork"`` (the default: a new branch of the conversation),
#: or ``"rewind"`` (no start told, as Claude Code's rewind tells none).
CONTINUED_AS_KEY = "continued_as"
#: The values :data:`CONTINUED_AS_KEY` takes.
CONTINUED_AS = ("resume", "fork", "rewind")


async def _message(value: Input) -> Message:
    if isinstance(value, str):
        return user(value, origin="human")
    if not any(isinstance(b, Image) for b in value.content):
        return value
    # An image enters the history once and is sent with every later request,
    # so it is made to fit here, where every front end's input passes.
    return await asyncio.to_thread(fit_message, value)


class ClientAPI(Protocol):
    """What every client offers, in process (:class:`Client`) or over HTTP
    (``noeta.host.remote.RemoteClient``): create, drive and read tasks."""

    async def create(self, agent: str, input: Input | None = None, *,
                     task_id: str | None = None, config: dict[str, Any] | None = None,
                     queue: str = "default", dedupe_key: str | None = None,
                     history_from: str | None = None,
                     history_through: int | None = None) -> str: ...

    async def send(self, task_id: str, input: Input, *,
                   when: Literal["now", "next_turn"] = "next_turn",
                   dedupe_key: str | None = None) -> int: ...

    async def answer(self, task_id: str, request_id: str,
                     decision: Literal["allow", "deny"] | None = None, *,
                     always: bool = False, data: Any = None) -> int: ...

    async def signal(self, task_id: str, name: str, data: Any = None, *,
                     dedupe_key: str | None = None) -> int: ...

    async def cancel(self, task_id: str, reason: str = "", *,
                     turn_only: bool = False) -> None: ...

    async def get(self, task_id: str) -> TaskRow: ...

    async def tasks(self, *, parent_id: str | None = None, status: Status | None = None,
                    limit: int = 100) -> list[TaskRow]: ...

    async def events(self, task_id: str, after: int = 0) -> list[Recorded]: ...

    def watch(self, task_id: str, after: int = 0) -> AsyncIterator[Recorded]: ...

    async def wait(self, task_id: str, *, timeout: float | None = None) -> TaskRow: ...


class Client:
    def __init__(self, store: Store, *, poll_interval: float = 0.5) -> None:
        self.store = store
        self.poll_interval = poll_interval

    async def _row(self, task_id: str) -> TaskRow:
        row = await self.store.get(task_id)
        if row is None:
            raise UnknownTask(task_id)
        return row

    async def create(
        self,
        agent: str,
        input: Input | None = None,
        *,
        task_id: str | None = None,
        config: dict[str, Any] | None = None,
        queue: str = "default",
        dedupe_key: str | None = None,
        history_from: str | None = None,
        history_through: int | None = None,
    ) -> str:
        """Create a task for ``agent``, optionally with its first message.
        With ``dedupe_key`` a repeated call returns the first task's id. A
        ``task_id`` that exists is returned as it is (its ``input`` is not
        posted), also when processes race to create it: to start a
        conversation idempotently, create it without ``input`` and ``send``
        each message with its own ``dedupe_key``.
        ``history_from`` starts it from another task's conversation, up to
        that task's event ``history_through`` (a fork, a rewind); a source
        that is no task raises ``UnknownTask``."""
        if history_from is not None:
            await self.get(history_from)
        tid = task_id or uuid.uuid4().hex
        created = await pin_history(self.store, TaskCreated(
            agent=agent, config=dict(config or {}), root_id=tid,
            history_from=history_from, history_through=history_through))
        task = NewTask(
            task_id=tid,
            created=created,
            queue=queue,
            dedupe_key=dedupe_key,
            inbox=[Say(message=await _message(input))] if input is not None else [],
        )
        created_id, _ = await self.store.create(task)
        return created_id

    async def send(
        self,
        task_id: str,
        input: Input,
        *,
        when: Literal["now", "next_turn"] = "next_turn",
        dedupe_key: str | None = None,
    ) -> int:
        """Send a message. ``now`` lands at the next step of the running
        turn; ``next_turn`` waits for the current turn to finish."""
        seq, _ = await self.store.post(
            Post(task_id=task_id, body=Say(message=await _message(input), when=when),
                 dedupe_key=dedupe_key))
        return seq

    async def answer(
        self,
        task_id: str,
        request_id: str,
        decision: Literal["allow", "deny"] | None = None,
        *,
        always: bool = False,
        data: Any = None,
    ) -> int:
        """Answer an approval or a question; returns the answer's inbox seq
        (the first one's, when the question was already answered)."""
        seq, _ = await self.store.post(Post(
            task_id=task_id,
            body=Answer(request_id=request_id, decision=decision, always=always, data=data),
            dedupe_key=f"answer:{request_id}",
        ))
        return seq

    async def signal(self, task_id: str, name: str, data: Any = None, *,
                     dedupe_key: str | None = None) -> int:
        """Post a named signal (``External``) a plugin of the task reads, such
        as the permissions plugin's ``permission_mode``; returns its inbox
        seq. It opens no turn."""
        seq, _ = await self.store.post(Post(task_id=task_id, body=External(name=name, data=data),
                                            dedupe_key=dedupe_key))
        return seq

    async def cancel(self, task_id: str, reason: str = "", *, turn_only: bool = False) -> None:
        """End the task, or with ``turn_only`` stop only the current turn."""
        await self.store.post(Post(task_id=task_id,
                                   body=Cancel(reason=reason, turn_only=turn_only)))

    async def get(self, task_id: str) -> TaskRow:
        return await self._row(task_id)

    async def tasks(self, *, parent_id: str | None = None, status: Status | None = None,
                    limit: int = 100) -> list[TaskRow]:
        return await self.store.tasks(parent_id=parent_id, status=status, limit=limit)

    async def events(self, task_id: str, after: int = 0) -> list[Recorded]:
        return await self.store.read(task_id, after)

    async def watch(self, task_id: str, after: int = 0) -> AsyncIterator[Recorded]:
        """Yield the task's events from ``after`` on, live, until it ends."""
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()

        def nudge(tid: str, _kind: str) -> None:
            if tid == task_id:
                loop.call_soon_threadsafe(changed.set)

        unsubscribe = self.store.on_change(nudge)
        try:
            while True:
                changed.clear()
                for rec in await self.store.read(task_id, after):
                    after = rec.seq
                    yield rec
                row = await self.get(task_id)
                if row.status == "ended" and row.last_seq <= after:
                    return
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(changed.wait(), self.poll_interval)
        finally:
            unsubscribe()

    async def wait(self, task_id: str, *, timeout: float | None = None) -> TaskRow:
        """Wait until the task is waiting or has ended."""

        async def settled() -> TaskRow:
            while True:
                row = await self.get(task_id)
                if row.status in ("waiting", "ended"):
                    return row
                await asyncio.sleep(self.poll_interval / 5)

        return await asyncio.wait_for(settled(), timeout)
