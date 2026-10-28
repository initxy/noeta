"""Channels: listeners that let an outside conversation (a chat thread, a
webhook) into tasks.

A ``Channel`` runs once per host process that enables it (``noeta worker``,
``noeta serve``; ``LocalApp(channels=...)``) and posts what arrives through
the client. Several replicas may run the same channel: :func:`deliver` makes
an inbound message idempotent (the conversation's task is created once, each
message posted once by its ``dedupe_key``), so the same delivery reaching two
replicas lands once. What a task says back goes out from inside the task (a
plugin's ``on_event``), where the task's one writer makes it a single sender.

Channels are found by name like plugins: the ``noeta.channels`` entry point
group, or a ``Channel`` subclass in a plugin file (``noeta.core.registry``).
"""

from __future__ import annotations

import asyncio
from html import escape
from typing import Any

from noeta.core.client import ClientAPI, Input

__all__ = ["Channel", "deliver", "framed"]


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
                  queue: str = "default") -> None:
    """Post one inbound ``message`` to the conversation ``task_id``,
    creating its task (for ``agent``, with ``config``) the first time.
    Idempotent: the task is created once however many replicas race, and
    the message is posted once per ``dedupe_key`` (the source's delivery
    id)."""
    await client.create(agent, task_id=task_id, config=config, queue=queue)
    await client.send(task_id, message, dedupe_key=dedupe_key)


def framed(source: str, sender: str, text: str) -> str:
    """``text`` marked as what ``sender`` wrote through ``source``, the way
    Claude Code's channels frame it: the model can tell who speaks."""
    return (f'<channel source="{escape(source)}" sender="{escape(sender)}">\n'
            f"{text}\n</channel>")
