"""Host-side views across a delegation tree: approvals and questions a
sub-agent is waiting on, seen from the root, and usage including children
(0.6 surfaced both on the root task)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from noeta.events import ModelReplied, Waiting
from noeta.store.base import TaskRow
from noeta.types import Struct, Usage

if TYPE_CHECKING:  # the client layer sits above the extensions
    from noeta.client import Client


class PendingRequest(Struct):
    """Something a task in the tree waits on a person for. Answer it with
    ``client.answer(task_id, request_id, ...)``."""

    task_id: str
    on: str
    request_id: str
    detail: dict[str, Any] = {}


async def task_tree(client: Client, root_id: str, *, limit: int = 10_000) -> list[TaskRow]:
    """``root_id`` and every task below it, parents before children."""
    out = [await client.get(root_id)]
    i = 0
    while i < len(out) and len(out) < limit:
        out.extend(await client.tasks(parent_id=out[i].task_id, limit=limit))
        i += 1
    return out


async def pending_requests(client: Client, root_id: str) -> list[PendingRequest]:
    """Every approval or question waiting anywhere in the tree."""
    found: list[PendingRequest] = []
    for row in await task_tree(client, root_id):
        if row.status != "waiting" or row.waiting_on not in ("approval", "question"):
            continue
        waiting = next((r.event for r in reversed(await client.events(row.task_id))
                        if isinstance(r.event, Waiting)), None)
        if waiting is None:
            continue
        for rid in waiting.ids:
            detail = waiting.detail.get(rid)
            if waiting.on == "question" and not (
                    isinstance(detail, dict) and detail.get("on") == "question"):
                continue  # a question wait also lists the children of other parked calls
            found.append(PendingRequest(task_id=row.task_id, on=waiting.on, request_id=rid,
                                        detail=detail if isinstance(detail, dict) else {}))
    return found


async def tree_usage(client: Client, task_id: str, *, include_children: bool = True) -> Usage:
    """Model usage of the task, and with ``include_children`` of every
    sub-agent below it (foreground and background, any depth)."""
    rows = await task_tree(client, task_id) if include_children else [await client.get(task_id)]
    total = Usage()
    for row in rows:
        for rec in await client.events(row.task_id):
            if isinstance(rec.event, ModelReplied):
                total = total + rec.event.response.usage
    return total
