"""What a front end lists to resume from: the root tasks, most recent first,
each with its title, its directory and its first prompt."""

from __future__ import annotations

from typing import Any

from noeta.core.client import ClientAPI
from noeta.core.events import ExtensionRecorded, InputTaken, Recorded, Say, TaskCreated
from noeta.core.store import TaskRow

__all__ = ["root_summaries", "task_summary"]

#: Rows read to find the roots among them (children are listed too).
SCAN_ROWS = 500
#: The characters of a first prompt a summary keeps.
PROMPT_CHARS = 200


def task_summary(row: TaskRow, events: list[Recorded]) -> dict[str, Any]:
    """``row`` with the ``title`` the title plugin recorded, the ``cwd`` its
    ``TaskCreated`` names and its first ``prompt``."""
    title, cwd, prompt = "", "", ""
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
            "title": title, "cwd": cwd, "prompt": prompt}


async def root_summaries(client: ClientAPI, limit: int = 50,
                   cache: dict[str, tuple[int, dict[str, Any]]] | None = None
                   ) -> list[dict[str, Any]]:
    """The ``limit`` most recently updated root tasks as
    :func:`task_summary`. ``cache`` (task id -> ``(last_seq, summary)``)
    spares reading the log of a task that has not moved."""
    rows = [r for r in await client.tasks(limit=SCAN_ROWS) if r.parent_id is None][:limit]
    out = []
    for row in rows:
        hit = cache.get(row.task_id) if cache is not None else None
        if hit is not None and hit[0] == row.last_seq:
            out.append({**hit[1], "status": row.status, "waiting_on": row.waiting_on})
            continue
        summary = task_summary(row, await client.events(row.task_id))
        if cache is not None:
            cache[row.task_id] = (row.last_seq, summary)
        out.append(summary)
    return out
