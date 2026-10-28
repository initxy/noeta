"""What a task (and its sub-agents) is waiting on a human for, with the ids
a host answers with."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from noeta.core import records
from noeta.core.events import (
    Answer,
    InputTaken,
    ModelCalled,
    ModelReplied,
    Recorded,
    TaskCreated,
    TaskEnded,
    ToolFinished,
    Waiting,
)
from noeta.core.types import ToolUse

if TYPE_CHECKING:
    from noeta.core.client import Client


@dataclass(frozen=True)
class Pending:
    """One open request. Answer it with
    ``client.answer(task_id, request_id, "allow" | "deny", always=...)``
    (an approval) or ``client.answer(task_id, request_id, data=...)`` (a
    question; ``noeta.host.questions.answer_question`` validates first).

    ``detail``: for an approval, what the gate said (``prompt``, ``reason``,
    ``always`` — the rule an "always" answer would remember, or ``None`` —
    and ``always_label``, that rule as a person reads it); for a question,
    ``{"questions": [...]}`` as the tool normalised them (each with its
    ``id``)."""

    task_id: str
    request_id: str
    kind: Literal["approval", "question"]
    tool: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: The agent of the task that asks (a sub-agent's name, for showing).
    agent: str = ""


def choices(detail: dict[str, Any]) -> list[dict[str, str]]:
    """An approval's options, Claude Code's card: ``Yes``, the "don't ask
    again" rule, a mode switch, ``No``. Each is ``{"id", "label"}``; answer
    with :func:`answer_choice`. A gate that names no choices gets yes /
    always (when it offers one) / no."""
    given = detail.get("choices")
    if isinstance(given, list) and given:
        return [{"id": str(c.get("id")), "label": str(c.get("label"))}
                for c in given if isinstance(c, dict)]
    out = [{"id": "allow", "label": "Yes"}]
    always = always_option(detail)
    if always is not None:
        out.append({"id": "always", "label": always})
    out.append({"id": "deny", "label": "No"})
    return out


def card_title(item: Pending, root_task_id: str = "") -> str:
    """What the card is about (``Bash command``), and which sub-agent asks
    (``· from the Explore agent``) when it is not the root."""
    title = str(item.detail.get("title") or item.tool or "Tool call")
    if root_task_id and item.task_id != root_task_id and item.agent:
        title += f" · from the {item.agent} agent"
    return title


async def answer_choice(client: Any, task_id: str, request_id: str, choice: str,
                        message: str = "") -> None:
    """Answer an approval with one of its :func:`choices` (``message``: what
    the person said when declining)."""
    if choice == "deny":
        await client.answer(task_id, request_id, "deny",
                            data={"message": message} if message else None)
    elif choice == "always":
        await client.answer(task_id, request_id, "allow", always=True)
    elif choice.startswith("mode:"):
        await client.answer(task_id, request_id, "allow", data={"mode": choice[5:]})
    else:
        await client.answer(task_id, request_id, "allow")


def always_option(detail: dict[str, Any]) -> str | None:
    """What an approval's "always" choice says it remembers: ``Always allow
    Bash(git push *)`` when the gate named its rule, a bare ``Always allow``
    when the gate said nothing about one, ``None`` when the gate said nothing
    would be remembered (no "always" choice to offer)."""
    label = detail.get("always_label")
    if isinstance(label, str) and label:
        return f"Always allow {label}"
    if "always" in detail and detail["always"] is None:
        return None
    return "Always allow"


def pending(events: Sequence[Recorded], task_id: str = "") -> list[Pending]:
    """The approvals and questions of the task's last ``Waiting`` that no
    answer has been taken for yet. Empty when the task is not waiting on a
    human."""
    uses: dict[str, ToolUse] = {}
    parked: dict[str, dict[str, Any]] = {}
    waiting: Waiting | None = None
    done: set[str] = set()
    agent = ""
    for rec in events:
        ev = rec.event
        if isinstance(ev, TaskCreated):
            agent = ev.agent
        elif isinstance(ev, ModelReplied):
            uses = {u.id: u for u in ev.response.message.tool_uses}
            parked = {}
        elif isinstance(core := records.decode(ev), records.Parked):
            parked[str(core.call_id)] = dict(core.detail or {})
        elif isinstance(ev, Waiting):
            waiting, done = ev, set()
        elif isinstance(ev, (ModelCalled, TaskEnded)):
            waiting = None
        elif isinstance(ev, InputTaken):
            done |= {i.body.request_id for i in ev.items if isinstance(i.body, Answer)}
        elif isinstance(ev, ToolFinished):
            done.add(ev.result.call_id)
    if waiting is None or waiting.on not in ("approval", "question"):
        return []
    out: list[Pending] = []
    for rid in waiting.ids:
        if rid in done or (waiting.on == "question" and rid not in parked):
            continue  # a question wait also lists the children of other parked calls
        use = uses.get(rid)
        tool = use.name if use else ""
        args = dict(use.input) if use else {}
        if waiting.on == "approval":
            d = dict(waiting.detail.get(rid) or {})
            out.append(Pending(task_id=task_id, request_id=rid, kind="approval", tool=tool,
                               input=args, prompt=str(d.get("prompt") or ""), detail=d,
                               agent=agent))
        else:
            out.append(Pending(task_id=task_id, request_id=rid, kind="question", tool=tool,
                               input=args, detail=parked.get(rid, {}), agent=agent))
    return out


async def pending_tree(client: Client, task_id: str) -> list[Pending]:
    """:func:`pending` for the task and every waiting descendant: a
    sub-agent's approval or question surfaces at the root, carrying the
    sub-agent's ``task_id`` to answer it on."""
    out = pending(await client.events(task_id), task_id)
    for child in await client.tasks(parent_id=task_id, limit=10_000):
        if child.status != "ended":
            out.extend(await pending_tree(client, child.task_id))
    return out


async def tree_busy(client: Client, task_id: str) -> bool:
    """Whether a task below ``task_id`` (a background sub-agent) has not ended."""
    for child in await client.tasks(parent_id=task_id, limit=10_000):
        if child.status != "ended" or await tree_busy(client, child.task_id):
            return True
    return False


async def follow_tree(client: Client, task_id: str, after: int = 0, *,
                      poll: float = 1.0) -> AsyncIterator[Recorded | list[Pending]]:
    """``client.watch(task_id, after)``, plus what its sub-agents wait on a
    human for: while the task waits and a sub-agent below it is still
    running, the tree is checked every ``poll`` seconds and, when a
    descendant waits on an approval or a question, that list is yielded
    (again on every check until answered — a front end remembers what it
    has shown)."""
    row = await client.get(task_id)
    on_children = row.status == "waiting" and (row.waiting_on == "children"
                                               or await tree_busy(client, task_id))
    records = client.watch(task_id, after).__aiter__()
    nxt: asyncio.Future[Recorded] = asyncio.ensure_future(records.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({nxt}, timeout=poll if on_children else None)
            if not done:
                found = [p for p in await pending_tree(client, task_id) if p.task_id != task_id]
                if found:
                    yield found
                on_children = await tree_busy(client, task_id)
                continue
            try:
                rec = nxt.result()
            except StopAsyncIteration:
                return
            on_children = isinstance(rec.event, Waiting) and (
                rec.event.on == "children" or await tree_busy(client, task_id))
            nxt = asyncio.ensure_future(records.__anext__())
            yield rec
    finally:
        nxt.cancel()
        with contextlib.suppress(BaseException):
            await nxt
        aclose = getattr(records, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
