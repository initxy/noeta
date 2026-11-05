"""What a task (and its sub-agents) is waiting on a human for, with the ids
a host answers with."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
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
from noeta.host.feed import tool_title

if TYPE_CHECKING:
    from noeta.core.client import ClientAPI


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


def _call_title(item: Pending) -> str:
    return tool_title(ToolUse(id=item.request_id, name=item.tool, input=item.input)) \
        if item.tool else ""


def card_title(item: Pending, root_task_id: str = "") -> str:
    """What the card is about: the gate's title (``Bash command``) or the
    call itself (``Read src/app.py``), and which sub-agent asks (``· from
    the Explore agent``) when it is not the root."""
    title = str(item.detail.get("title") or "") or _call_title(item) or "Tool call"
    if root_task_id and item.task_id != root_task_id and item.agent:
        title += f" · from the {item.agent} agent"
    return title


def card_prompt(item: Pending) -> str:
    """The card's body: the gate's prompt, under the call itself when the
    title names something else."""
    call = _call_title(item)
    if call and card_title(item) != call:
        return f"{call}\n{item.prompt}".strip()
    return item.prompt


def approvals(waiting: Waiting, uses: Mapping[str, ToolUse], task_id: str = "",
              agent: str = "") -> list[Pending]:
    """The approvals an approval ``Waiting`` asks for, with the calls they
    are about (``uses`` by call id; a missing one leaves the tool empty)."""
    if waiting.on != "approval":
        return []
    out = []
    for rid in waiting.ids:
        use = uses.get(rid)
        d = dict(waiting.detail.get(rid) or {})
        out.append(Pending(task_id=task_id, request_id=rid, kind="approval",
                           tool=use.name if use else "", input=dict(use.input) if use else {},
                           prompt=str(d.get("prompt") or ""), detail=d, agent=agent))
    return out


async def answer_choice(client: ClientAPI, task_id: str, request_id: str, choice: str,
                        message: str = "") -> None:
    """Answer an approval with one of its :func:`choices` (``message``: what
    the person added — a no's reason, or words the model reads after a yes's
    result)."""
    said = {"message": message} if message else {}
    if choice == "deny":
        await client.answer(task_id, request_id, "deny", data=said or None)
    elif choice == "always":
        await client.answer(task_id, request_id, "allow", always=True, data=said or None)
    elif choice.startswith("mode:"):
        await client.answer(task_id, request_id, "allow", data={"mode": choice[5:], **said})
    else:
        await client.answer(task_id, request_id, "allow", data=said or None)


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
    if waiting.on == "approval":
        return [p for p in approvals(waiting, uses, task_id, agent) if p.request_id not in done]
    out: list[Pending] = []
    for rid in waiting.ids:
        if rid in done or rid not in parked:
            continue  # a question wait also lists the children of other parked calls
        use = uses.get(rid)
        out.append(Pending(task_id=task_id, request_id=rid, kind="question",
                           tool=use.name if use else "", input=dict(use.input) if use else {},
                           detail=parked.get(rid, {}), agent=agent))
    return out


#: Tasks a :func:`pending_tree` cache keeps, at most (the oldest go first).
PENDING_CACHE_TASKS = 1024


async def pending_tree(client: ClientAPI, task_id: str,
                       cache: dict[str, tuple[int, list[Pending]]] | None = None
                       ) -> list[Pending]:
    """:func:`pending` for the task and every waiting descendant: a
    sub-agent's approval or question surfaces at the root, carrying the
    sub-agent's ``task_id`` to answer it on. ``cache`` (task id ->
    ``(last_seq, pending)``) spares reading the log of a task that has not
    moved, so a front end can poll cheaply."""
    last_seq = (await client.get(task_id)).last_seq if cache is not None else 0
    return await _pending_tree(client, task_id, last_seq, cache)


async def _pending_tree(client: ClientAPI, task_id: str, last_seq: int,
                        cache: dict[str, tuple[int, list[Pending]]] | None) -> list[Pending]:
    hit = cache.get(task_id) if cache is not None else None
    if hit is not None and hit[0] == last_seq:
        out = list(hit[1])
    else:
        out = pending(await client.events(task_id), task_id)
        if cache is not None:
            cache.pop(task_id, None)
            cache[task_id] = (last_seq, list(out))
            while len(cache) > PENDING_CACHE_TASKS:
                del cache[next(iter(cache))]
    for child in await client.tasks(parent_id=task_id, limit=10_000):
        if child.status != "ended":
            out.extend(await _pending_tree(client, child.task_id, child.last_seq, cache))
    return out


async def tree_busy(client: ClientAPI, task_id: str) -> bool:
    """Whether a task below ``task_id`` (a background sub-agent) has not ended."""
    for child in await client.tasks(parent_id=task_id, limit=10_000):
        if child.status != "ended" or await tree_busy(client, child.task_id):
            return True
    return False


async def follow_tree(client: ClientAPI, task_id: str, after: int = 0, *,
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
