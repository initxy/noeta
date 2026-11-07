"""Rewind: go back to before one of the person's prompts, as Claude Code's
``/rewind`` does — the conversation (a new task continues the log up to
just before that prompt), the files (put back as the ``checkpoints``
plugin recorded them), or both.

Every front end uses these helpers over the client API, so a rewind works
the same in the TUI, over ACP, HTTP and the headless protocol. A rewind
point may lie in the conversation a task continues (``history_from``): the
fork is then made from that earlier log.
"""

from __future__ import annotations

import base64
import difflib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from noeta.core.client import CONTINUED_AS_KEY, ClientAPI
from noeta.core.events import ExtensionRecorded, InputTaken, Recorded, Say, TaskCreated
from noeta.core.loop.state import PLUGINS_OFF
from noeta.toolkit.plugin_records import CHECKPOINTS

__all__ = ["CHECKPOINTS", "RESTORE_SIGNAL", "FilePlan", "RewindPoint", "Rewound",
           "diff_stats", "file_plan", "rewind", "rewind_points"]

#: The checkpoints plugin's signal (its documented records).
RESTORE_SIGNAL = "restore_files"


@dataclass(frozen=True)
class RewindPoint:
    """One prompt the person sent. ``task_id`` / ``seq``: the log and the
    ``InputTaken`` that took it; ``inbox_seq``: its inbox item (what a
    sender got back from ``send``); ``files``: paths changed since."""

    task_id: str
    seq: int
    at: float
    text: str
    inbox_seq: int = 0
    files: tuple[str, ...] = ()


@dataclass
class FilePlan:
    """What a file rewind writes: per path its content then, ``None`` for a
    file that did not exist; ``kept`` paths whose content was not saved
    (too large, unreadable) and stay as they are."""

    files: dict[str, bytes | None] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Rewound:
    """``task_id``: the task to go on with (the fork, or the same task
    when only files were restored); ``prompt``: the text of the prompt
    rewound to, for the person to edit and send again."""

    task_id: str
    prompt: str
    files: tuple[str, ...] = ()


async def _lineage(client: ClientAPI, task_id: str) -> list[tuple[str, list[Recorded]]]:
    """The task's log and, oldest first, each log it continues (trimmed to
    the event it continues from)."""
    out: list[tuple[str, list[Recorded]]] = []
    current: str | None = task_id
    through: int | None = None
    while current and len(out) < 64:
        events = await client.events(current)
        if through is not None:
            events = [r for r in events if r.seq <= through]
        out.append((current, events))
        first = events[0].event if events else None
        if not isinstance(first, TaskCreated):
            break
        current, through = first.history_from, first.history_through
    return list(reversed(out))


async def _tree(client: ClientAPI, task_id: str) -> list[str]:
    ids = [task_id]
    for child in await client.tasks(parent_id=task_id, limit=10_000):
        ids.extend(await _tree(client, child.task_id))
    return ids


async def _snapshots(client: ClientAPI, task_id: str) -> tuple[list[tuple[float, dict[str, Any]]],
                                                         dict[str, str]]:
    """Every snapshot of the task's lineage and its sub-agents, by time,
    and every blob recorded with them."""
    snaps: list[tuple[float, dict[str, Any]]] = []
    blobs: dict[str, str] = {}
    seen: set[str] = set()
    for tid, _events in await _lineage(client, task_id):
        for member in await _tree(client, tid):
            if member in seen:
                continue
            seen.add(member)
            for rec in await client.events(member):
                ev = rec.event
                if isinstance(ev, ExtensionRecorded) and ev.extension == CHECKPOINTS \
                        and ev.kind == "snapshot" and isinstance(ev.data, dict):
                    snaps.append((rec.at, ev.data))
                    if ev.data.get("blob") is not None and ev.data.get("sha"):
                        blobs[str(ev.data["sha"])] = str(ev.data["blob"])
    snaps.sort(key=lambda item: item[0])
    return snaps, blobs


async def rewind_points(client: ClientAPI, task_id: str) -> list[RewindPoint]:
    """The person's prompts in the conversation, oldest first, each with
    the files changed since."""
    points: list[RewindPoint] = []
    for tid, events in await _lineage(client, task_id):
        for rec in events:
            if not isinstance(rec.event, InputTaken):
                continue
            for item in rec.event.items:
                body = item.body
                if isinstance(body, Say) and body.message.role == "user" \
                        and body.message.origin in (None, "human"):
                    points.append(RewindPoint(task_id=tid, seq=rec.seq, at=rec.at,
                                              text=body.message.text, inbox_seq=item.seq))
                    break
    snaps, _ = await _snapshots(client, task_id)
    out = []
    for p in points:
        changed = tuple(dict.fromkeys(str(d.get("path")) for at, d in snaps if at >= p.at))
        out.append(RewindPoint(task_id=p.task_id, seq=p.seq, at=p.at, text=p.text,
                               inbox_seq=p.inbox_seq, files=changed))
    return out


async def file_plan(client: ClientAPI, task_id: str, point: RewindPoint) -> FilePlan:
    """The files as they were when ``point``'s prompt arrived: the first
    snapshot of each file taken after it."""
    snaps, blobs = await _snapshots(client, task_id)
    plan = FilePlan()
    for at, data in snaps:
        path = str(data.get("path") or "")
        if at < point.at or not path or path in plan.files or path in plan.kept:
            continue
        sha = data.get("sha")
        if sha is None:
            plan.files[path] = None
        elif sha in blobs:
            plan.files[path] = base64.b64decode(blobs[sha])
        else:
            plan.kept.append(path)
    return plan


def diff_stats(plan: FilePlan) -> tuple[int, int]:
    """Lines a rewind of local files would add and remove (against what is
    on this machine's disk now)."""
    added = removed = 0
    for path, content in plan.files.items():
        try:
            now = Path(path).read_bytes().decode(errors="replace").splitlines()
        except OSError:
            now = []
        then = content.decode(errors="replace").splitlines() if content is not None else []
        for line in difflib.ndiff(now, then):
            if line.startswith("+ "):
                added += 1
            elif line.startswith("- "):
                removed += 1
    return added, removed


async def rewind(client: ClientAPI, task_id: str, point: RewindPoint, *, conversation: bool = True,
                 files: bool = True, config_keys: Sequence[str] | None = None,
                 queue: str | None = None) -> Rewound:
    """Rewind to before ``point``. With ``conversation`` a new task (same
    agent, queue and config, or only its ``config_keys`` when given: what a
    request may set; on ``queue`` when given) continues the log up to just
    before the prompt; with
    ``files`` the files changed since (by the task or its sub-agents) are
    written back as they were, by the task to go on with — so the front end
    following it sees the restore's notice. The checkpoints plugin checks a
    restore against the snapshots of that task, the conversations it
    continues and their sub-agents: the logs this plan is read from."""
    target = task_id
    if conversation:
        events = await client.events(point.task_id)
        first = events[0].event if events else None
        if not isinstance(first, TaskCreated):
            raise ValueError(f"task {point.task_id} has no creation event")
        # Its switches are the source's now, not at its start (``create``).
        config = {k: v for k, v in first.config.items()
                  if (config_keys is None or k in config_keys) and k != PLUGINS_OFF}
        queue = queue or (await client.get(point.task_id)).queue
        config[CONTINUED_AS_KEY] = "rewind"
        target = await client.create(first.agent, None, config=config, queue=queue,
                                     history_from=point.task_id,
                                     history_through=point.seq - 1)
    restored: tuple[str, ...] = ()
    if files:
        plan = await file_plan(client, task_id, point)
        if plan.files:
            await client.signal(target, RESTORE_SIGNAL, {"files": [
                {"path": p, "data": base64.b64encode(c).decode() if c is not None else None}
                for p, c in plan.files.items()]})
            restored = tuple(plan.files)
    return Rewound(task_id=target, prompt=point.text, files=restored)
