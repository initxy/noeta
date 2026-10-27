"""The client API hosts, the TUI and the server use. Everything that is not
the running task talks to it through here: create, send, answer, cancel,
read, watch."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal, Union

from noeta.core.errors import UnknownTask
from noeta.core.events import Answer, Cancel, Recorded, Say, TaskCreated
from noeta.core.store.base import NewTask, Post, Status, Store, TaskRow
from noeta.core.types import Message, user

Input = Union[str, Message]


def _message(value: Input) -> Message:
    return user(value, origin="human") if isinstance(value, str) else value


class Client:
    def __init__(self, store: Store, *, poll_interval: float = 0.5) -> None:
        self.store = store
        self.poll_interval = poll_interval

    async def create(
        self,
        agent: str,
        input: Input | None = None,
        *,
        task_id: str | None = None,
        config: dict[str, Any] | None = None,
        queue: str = "default",
        dedupe_key: str | None = None,
    ) -> str:
        """Create a task for ``agent``, optionally with its first message.
        With ``dedupe_key`` a repeated call returns the first task's id."""
        tid = task_id or uuid.uuid4().hex
        task = NewTask(
            task_id=tid,
            created=TaskCreated(agent=agent, config=dict(config or {}), root_id=tid),
            queue=queue,
            dedupe_key=dedupe_key,
            inbox=[Say(message=_message(input))] if input is not None else [],
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
            Post(task_id=task_id, body=Say(message=_message(input), when=when),
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

    async def cancel(self, task_id: str, reason: str = "", *, turn_only: bool = False) -> None:
        """End the task, or with ``turn_only`` stop only the current turn."""
        await self.store.post(Post(task_id=task_id,
                                   body=Cancel(reason=reason, turn_only=turn_only)))

    async def get(self, task_id: str) -> TaskRow:
        row = await self.store.get(task_id)
        if row is None:
            raise UnknownTask(task_id)
        return row

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
                with _suppress_timeout():
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


class _suppress_timeout:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is asyncio.TimeoutError
