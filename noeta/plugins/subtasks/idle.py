"""``SendMessage(notify_when_idle=true)``: one notice when another open
session (a root task of the store) next goes idle or ends.

Both sides keep their half in their own log. The subscriber records what it
waits for and sets a timer for the expiry; the session it asked takes the
request from its inbox (an ``External``) and records the subscriber. When
that session's turn closes with nothing queued, or it is cancelled, it
posts one ``External`` back with the record that drops the subscriber (one
append), and the subscriber turns it into a ``system`` notice if it still
waits for that id. A request taken while the session is already idle is
answered at once."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from noeta.core.events import External, InboxItem, Say, TimerFired
from noeta.core.extension import TaskView
from noeta.core.types import user
from noeta.plugins.subtasks import texts

#: Inbox signals between the two sessions.
SUBSCRIBE_SIGNAL = "subtasks.idle_subscribe"
NOTICE_SIGNAL = "subtasks.idle_notice"
#: Claude Code's limits: a subscription lapses after 12 hours; a session
#: waits on at most 32, and holds at most 32 subscribers.
EXPIRY_HOURS = 12
EXPIRY_SECONDS = EXPIRY_HOURS * 3600
MAX_SUBSCRIPTIONS = 32
#: The most of the session's last reply a notice quotes.
DETAIL_CHARS = 100
_DETAIL_UNSAFE = re.compile(r"[\x00-\x1f\x7f<>«»\"\[\]]")


def timer_name(sub_id: str) -> str:
    return f"subtasks.idle:{sub_id}"


def initial() -> dict[str, Any]:
    """``waits``: target -> {id, at} (subscriber side); ``holds``:
    subscriber task -> id (subscribed-to side)."""
    return {"waits": {}, "holds": {}}


def reduce(st: dict[str, Any], kind: str, data: Any) -> dict[str, Any] | None:
    """The idle records folded into the extension's state, or ``None`` when
    ``kind`` is not one of them."""
    if kind == "idle_subscribed":
        return {**st, "waits": {**st["waits"], data["target"]: data}}
    if kind == "idle_heard":
        return {**st, "waits": {k: v for k, v in st["waits"].items() if v["id"] != data["id"]}}
    if kind == "idle_held":
        return {**st, "holds": {**st["holds"], data["from"]: data["id"]}}
    if kind == "idle_fired":
        gone = set(data["from"])
        return {**st, "holds": {k: v for k, v in st["holds"].items() if k not in gone}}
    return None


def on_input(owner: str, view: TaskView, st: Mapping[str, Any],
             items: list[InboxItem]) -> None:
    """Take subscriptions, notices and expiries from what the task took, then
    answer the subscriptions at once if the task is idle or ending."""
    holds = dict(st["holds"])
    waits = dict(st["waits"])
    for item in items:
        body = item.body
        if isinstance(body, External) and body.name == SUBSCRIBE_SIGNAL:
            data = body.data if isinstance(body.data, dict) else {}
            sender, sub_id = data.get("from"), data.get("id")
            if not isinstance(sender, str) or not isinstance(sub_id, str) \
                    or sender == view.task_id:
                continue
            if sender not in holds and len(holds) >= MAX_SUBSCRIPTIONS:
                _send(view, sender, sub_id, {"state": "unavailable"})
                continue
            holds[sender] = sub_id
            view.record(owner, "idle_held", {"from": sender, "id": sub_id})
        elif isinstance(body, External) and body.name == NOTICE_SIGNAL:
            data = body.data if isinstance(body.data, dict) else {}
            target = next((t for t, w in waits.items() if w["id"] == data.get("id")), None)
            if target is not None:
                _heard(owner, view, waits.pop(target), notice_text(target, data))
        elif isinstance(body, TimerFired) and body.name.startswith("subtasks.idle:"):
            target = next((t for t, w in waits.items() if timer_name(w["id"]) == body.name),
                          None)
            if target is not None:
                _heard(owner, view, waits.pop(target),
                       notice_text(target, {"state": "expired"}), timer_fired=True)
    settle(owner, view, holds, at=None)


def settle(owner: str, view: TaskView, holds: Mapping[str, str], *, at: float | None) -> None:
    """Answer the held subscriptions when the task is ending (``exited``) or
    idle: no turn open, nothing queued (``at``: when its turn closed, if it
    just did). Called only while the task can still write."""
    state = view.state
    if not holds or state.ended is not None:
        return
    if state.cancel_requested is not None:
        fire(owner, view, holds, "exited", at=time.time())
    elif not state.turn_open and not state.calls:
        fire(owner, view, holds, "idle", at=at)


def fire(owner: str, view: TaskView, holds: Mapping[str, str], kind: str,
         at: float | None) -> None:
    """Send every held subscription its one notice and drop them."""
    data: dict[str, Any] = {"state": kind}
    if at is not None:
        data["finished_at"] = at
    if kind == "idle":
        detail = _detail(view.state.last_answer)
        if detail:
            data["detail"] = detail
    for sender, sub_id in holds.items():
        _send(view, sender, sub_id, data)
    view.record(owner, "idle_fired", {"from": sorted(holds)})


def _send(view: TaskView, sender: str, sub_id: str, data: Mapping[str, Any]) -> None:
    view.enqueue(External(name=NOTICE_SIGNAL, data={**data, "id": sub_id, "from": view.task_id}),
                 dedupe_key=f"subtasks:idle-notice:{sub_id}", task_id=sender)


def _heard(owner: str, view: TaskView, wait: Mapping[str, Any], text: str, *,
           timer_fired: bool = False) -> None:
    view.record(owner, "idle_heard", {"id": wait["id"]})
    if not timer_fired:
        view.clear_timer(timer_name(wait["id"]))
    view.enqueue(Say(message=user(text, origin="system")),
                 dedupe_key=f"subtasks:idle-heard:{wait['id']}")


def _detail(answer: str) -> str:
    line = next((x for x in answer.split("\n") if x.strip()), "")
    line = re.sub(r"\s+", " ", _DETAIL_UNSAFE.sub(" ", line)).strip()
    return line if len(line) <= DETAIL_CHARS else line[:DETAIL_CHARS - 1] + "…"


def _clock(at: Any) -> str:
    return time.strftime("%H:%M", time.localtime(at)) if isinstance(at, (int, float)) else ""


def notice_text(label: str, data: Mapping[str, Any]) -> str:
    """The notice the subscriber reads, by the state the session reported."""
    kind = data.get("state")
    when = _clock(data.get("finished_at"))
    if kind == "idle":
        detail = data.get("detail")
        text = texts.IDLE_NOTICE_IDLE.format(
            label=label, when=texts.IDLE_WHEN.format(time=when) if when else "",
            detail=texts.IDLE_DETAIL.format(detail=detail) if isinstance(detail, str) else "")
    elif kind == "exited":
        text = texts.IDLE_NOTICE_EXITED.format(
            label=label, when=texts.IDLE_EXIT_WHEN.format(time=when) if when else "")
    elif kind == "expired":
        return texts.IDLE_NOTICE_EXPIRED.format(
            label=label, hours=EXPIRY_HOURS,
            footer=texts.IDLE_FOOTER.format(source=texts.IDLE_SOURCE_OWN))
    else:
        text = texts.IDLE_NOTICE_UNAVAILABLE.format(label=label)
    return f"{text} {texts.IDLE_FOOTER.format(source=texts.IDLE_SOURCE_PEER)}"
