"""Channels: listeners that let an outside conversation (a chat thread, a
webhook) into tasks.

A ``Channel`` runs once per host process that enables it (``noeta worker``,
``noeta serve``; ``LocalApp(channels=...)``) and posts what arrives through
the client. Several replicas may run the same channel: :func:`deliver` makes
an inbound message idempotent (the conversation's task is created once, each
message posted once by its ``dedupe_key``), so the same delivery reaching two
replicas lands once. A conversation outlives its task: once the task ended
(it failed, was cancelled, or its agent is one-shot) the next message starts
a continuation, ``<id>.2``, ``<id>.3`` and so on, each from the previous
one's conversation (:func:`current_task`). What a task says back goes out from inside the task (a
plugin's ``on_event``), where the task's one writer makes it a single sender.

Channels are found by name like plugins: the ``noeta.channels`` entry point
group, or a ``Channel`` subclass in a plugin file (``noeta.core.registry``).
"""

from __future__ import annotations

import asyncio
import re
from html import escape
from typing import Any

from noeta.core.client import CONTINUED_AS_KEY, ClientAPI, Input
from noeta.core.errors import UnknownTask
from noeta.core.store import TaskRow
from noeta.core.types import Message, Text

__all__ = ["Channel", "current_task", "deliver", "framed", "from_channel"]


class Channel:
    """A listener for one outside source. Subclasses set ``name`` and
    implement :meth:`run`; their constructor takes the host's arguments
    (``config.toml`` ``[channels.<name>]``)."""

    name: str = ""

    async def run(self, client: ClientAPI, stop: asyncio.Event) -> None:
        """Receive outside messages and post them (:func:`deliver`) until
        ``stop`` is set. An exception is logged and the channel started
        again after a pause."""
        raise NotImplementedError


async def deliver(client: ClientAPI, *, task_id: str, agent: str, message: Input,
                  dedupe_key: str, config: dict[str, Any] | None = None,
                  queue: str = "default", source: str = "channel", sender: str = "") -> str:
    """Post one inbound ``message`` to the conversation ``task_id``,
    creating its task (for ``agent``, with ``config``) the first time, and
    a continuation once that task ended. Returns the id of the task it went
    to. Idempotent: each task is created once however many replicas race,
    and the message is posted once per ``dedupe_key`` (the source's delivery
    id) to the conversation's current task; a delivery repeated after that
    task ended reaches the continuation again. What an outside sender wrote
    always arrives framed, as Claude Code's channel messages do: a message
    not already framed with :func:`framed` is framed here with ``source`` and
    ``sender``, so it is never taken as the task owner's own (its ``@path``
    mentions are not attached, :func:`from_channel`)."""
    message = _framed_message(message, source, sender)
    n, row = await _newest(client, task_id)
    if row is None:
        await client.create(agent, task_id=task_id, config=config, queue=queue)
        n, row = 1, await client.get(task_id)
    while True:
        if row.status == "ended":
            n += 1
            # Created once by any replica; ``history_from`` carries the talk on.
            await client.create(agent, task_id=_nth(task_id, n),
                                config={**(config or {}), CONTINUED_AS_KEY: "resume"},
                                queue=queue, history_from=row.task_id)
            row = await client.get(_nth(task_id, n))
            continue
        seq = await client.send(row.task_id, message, dedupe_key=dedupe_key)
        row = await client.get(row.task_id)
        # A task never ends over a message it did not take (the store
        # refuses it), so an unread one landed after the end: on to the next.
        if row.status != "ended" or row.inbox_read_seq >= seq:
            return row.task_id


async def current_task(client: ClientAPI, task_id: str) -> str | None:
    """The task the conversation ``task_id`` is at now (the newest of its
    continuations), or ``None`` before its first message: what a channel
    answers an approval or cancels in."""
    _, row = await _newest(client, task_id)
    return row.task_id if row is not None else None


def _nth(task_id: str, n: int) -> str:
    return task_id if n == 1 else f"{task_id}.{n}"


async def _newest(client: ClientAPI, task_id: str) -> tuple[int, TaskRow | None]:
    """The newest task of the conversation and its number: continuations
    are created in order, so the numbers that exist are 1..n; found in
    ``O(log n)`` reads."""
    async def row(n: int) -> TaskRow | None:
        try:
            return await client.get(_nth(task_id, n))
        except UnknownTask:
            return None

    found = await row(1)
    if found is None:
        return 0, None
    low, high = 1, 2
    while (r := await row(high)) is not None:
        low, found = high, r
        high *= 2
    while high - low > 1:  # ``low`` exists, ``high`` does not
        mid = (low + high) // 2
        r = await row(mid)
        if r is None:
            high = mid
        else:
            low, found = mid, r
    return low, found


def _framed_message(message: Input, source: str, sender: str) -> Input:
    if isinstance(message, str):
        return message if from_channel(message) else framed(source, sender, message)
    if from_channel(message.text):
        return message
    rest = [b for b in message.content if not isinstance(b, Text)]
    return Message(role=message.role, content=[Text(text=framed(source, sender, message.text)),
                                               *rest], origin=message.origin)


def framed(source: str, sender: str, text: str) -> str:
    """``text`` marked as what ``sender`` wrote through ``source``, the way
    Claude Code's channels frame it: the model can tell who speaks."""
    return (f'<channel source="{escape(source)}" sender="{escape(sender)}">\n'
            f"{text}\n</channel>")


_FRAME = re.compile(r"<channel\s+source=")


def from_channel(text: str) -> bool:
    """Whether ``text`` carries a channel's frame (:func:`framed`): an
    outside sender's words, not the task owner's, anywhere in it."""
    return _FRAME.search(text) is not None
