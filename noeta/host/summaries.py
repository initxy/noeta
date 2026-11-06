"""What a front end lists to resume from: the root tasks, most recent first,
each with its title, its directory, its first prompt and whether the person
archived it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from noeta.core.client import ClientAPI
from noeta.core.events import (
    ExtensionRecorded,
    External,
    InboxItem,
    InputTaken,
    Recorded,
    Say,
    TaskCreated,
)
from noeta.core.store import TaskRow
from noeta.toolkit.signals import ARCHIVE_SIGNAL

__all__ = ["archived_in", "root_summaries", "task_summary"]

#: Root rows read, at most, to find ``limit`` of them that pass the
#: ``archived`` filter.
SCAN_ROWS = 500
#: The characters of a first prompt a summary keeps.
PROMPT_CHARS = 200
#: Root summaries a cache keeps, least recently listed dropped first.
CACHE_ROOTS = 1000

#: A summary cache: task id -> ``(last_seq, inbox seq read through, summary)``.
Cache = dict[str, tuple[int, int, dict[str, Any]]]

#: Reads a task's inbox after a seq (``Store.inbox``).
InboxReader = Callable[[str, int], Awaitable[list[InboxItem]]]


def task_summary(row: TaskRow, events: list[Recorded],
                 since: dict[str, Any] | None = None) -> dict[str, Any]:
    """``row`` with the ``title`` the title plugin recorded, the ``cwd`` its
    ``TaskCreated`` names and its first ``prompt``; ``events`` are the ones
    after the summary ``since`` was made of, when one is given. ``archived``
    is carried over from ``since`` (:func:`archived_in` reads it)."""
    title, cwd, prompt = (str(since.get(k) or "") for k in ("title", "cwd", "prompt")) \
        if since is not None else ("", "", "")
    for rec in events:
        ev = rec.event
        if isinstance(ev, TaskCreated):
            cwd = str(ev.config.get("cwd") or "")
        elif isinstance(ev, ExtensionRecorded) and ev.extension == "title" \
                and ev.kind == "title" and isinstance(ev.data, dict):
            title = str(ev.data.get("title") or "") or title
        elif isinstance(ev, InputTaken) and not prompt:
            prompt = next((i.body.message.text for i in ev.items
                           if isinstance(i.body, Say) and i.body.message.from_person
                           and i.body.message.text.strip()), "")[:PROMPT_CHARS]
    return {"task_id": row.task_id, "agent": row.agent, "status": row.status,
            "waiting_on": row.waiting_on, "end_status": row.end_status,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "title": title, "cwd": cwd, "prompt": prompt,
            "archived": bool(since and since.get("archived"))}


def archived_in(items: list[InboxItem], archived: bool = False) -> bool:
    """Whether the task is archived after ``items`` (its inbox, in order),
    ``archived`` before them: the last ``archive`` signal says. Read from the
    inbox rather than the log because an ended task never takes the signal."""
    for item in items:
        body = item.body
        if isinstance(body, External) and body.name == ARCHIVE_SIGNAL:
            data = body.data if isinstance(body.data, dict) else {}
            archived = bool(data.get("archived", True))
    return archived


async def root_summaries(client: ClientAPI, limit: int = 50, cache: Cache | None = None, *,
                         inbox: InboxReader | None = None,
                         archived: bool | None = None) -> list[dict[str, Any]]:
    """The ``limit`` most recently updated root tasks as
    :func:`task_summary`, each with ``archived`` read through ``inbox``
    (always false without it); ``archived`` keeps only the archived ones
    (``True``) or only the others (``False``). ``cache`` (at most
    :data:`CACHE_ROOTS` of them) spares reading the log and inbox of a task
    that has not moved, and of one that has, all but what was added since."""
    # Filtered by ``archived`` after: more rows read than are listed.
    scan = limit if archived is None else max(limit, SCAN_ROWS)
    rows = await client.tasks(roots_only=True, limit=scan)
    out: list[dict[str, Any]] = []
    for row in rows:
        if len(out) >= limit:
            break
        hit = cache.pop(row.task_id, None) if cache is not None else None
        if hit is not None and hit[0] == row.last_seq:
            summary = {**hit[2], "status": row.status, "waiting_on": row.waiting_on,
                       "end_status": row.end_status, "updated_at": row.updated_at}
        elif hit is not None and hit[0] < row.last_seq:
            summary = task_summary(row, await client.events(row.task_id, hit[0]), hit[2])
        else:
            summary = task_summary(row, await client.events(row.task_id))
        read = hit[1] if hit is not None else 0
        if inbox is not None and read < row.inbox_seq:
            items = await inbox(row.task_id, read)
            summary["archived"] = archived_in(items, summary["archived"])
            read = items[-1].seq if items else row.inbox_seq
        if cache is not None:
            cache[row.task_id] = (row.last_seq, read, summary)  # the newest at the end
            while len(cache) > CACHE_ROOTS:
                cache.pop(next(iter(cache)))
        if archived is None or summary["archived"] == archived:
            out.append(summary)
    return out
