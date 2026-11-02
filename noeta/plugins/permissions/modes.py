"""Switching permission modes (plan mode with its plan file and name), and
the notes a mode owes the next request."""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from noeta.core.extension import Reminder, TaskView
from noeta.core.tool import ToolContext
from noeta.core.types import ToolResult
from noeta.plugins.permissions import plan as pl
from noeta.plugins.permissions import policy as p
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.state import (
    AUTO_DURING_PLAN,
    NAME,
    PermState,
    auto_active,
    state_of,
)

log = logging.getLogger("noeta.permissions")

#: The signal the plan-name side request answers with.
PLAN_NAME_SIGNAL = pl.PLAN_NAME_SIGNAL
#: Places in the joined system turn (``Reminder.order``): before the token
#: budget, after the skill list.
ORDER_PLAN = 64
ORDER_AUTO = 70
#: The plan file given again after a compaction: after the files read
#: again and the background tasks' status, ahead of the invoked skills
#: (Claude Code's order).
ORDER_PLAN_FILE = 15
#: The local command wrapper ``/plan`` sends (Claude Code's tags).
PLAN_COMMAND = ("<command-name>/plan</command-name>\n            <command-message>plan"
                "</command-message>\n            <command-args>{args}</command-args>\n\n"
                "<local-command-stdout>Enabled plan mode</local-command-stdout>")
PLAN_COMMAND_HEAD = "<command-name>/plan</command-name>"


def switch(view: TaskView, mode: Any, seq: int, *, current: str, disable_bypass: bool,
           plans_dir: str, by_command: bool = False, auto_during_plan: bool = True) -> None:
    """Record a switch from ``current`` to ``mode`` (asked for at log seq
    ``seq``; ``by_command``: by a typed ``/plan``), when it is one.
    ``auto_during_plan``: plan mode keeps the auto-mode classifier (kept
    with the plan: whether planning after auto mode leaves it)."""
    if not isinstance(mode, str) or mode not in p.PERMISSION_MODES:
        return
    if mode == "bypassPermissions" and disable_bypass:
        view.notify("bypassPermissions is disabled here")
        return
    if mode == current:
        return
    data: dict[str, Any] = {"mode": mode}
    if mode == "plan":
        st = state_of(view)
        path = (st.plan or {}).get("path") or pl.new_plan_path(plans_dir)
        data["plan"] = {"path": path, "pre_mode": current, "entered_seq": seq,
                        **({"by_command": True} if by_command else {}),
                        **({} if auto_during_plan else {AUTO_DURING_PLAN: False})}
    view.record(NAME, "mode", data)


def plan_start(view: TaskView, mode: str, *, default_mode: str, plans_dir: str,
               parent_plan: Any = None, auto_during_plan: bool = True) -> None:
    """A task that starts in plan mode (its config's ``permission_mode``, a
    sub-agent of a planning task): recorded as entered with a plan file of
    its own, so it plans from its first request. A sub-agent's is named
    after its parent's plan (``parent_plan``): ``<name>-agent-<task id>.md``,
    as Claude Code names it."""
    pre = default_mode if default_mode != "plan" else "default"
    path = (posixpath.join(plans_dir, f"{parent_plan}-agent-{view.task_id}.md")
            if isinstance(parent_plan, str) and pl.plan_name_ok(parent_plan)
            else pl.new_plan_path(plans_dir))
    view.record(NAME, "mode", {"mode": mode, "plan": {
        "path": path, "pre_mode": pre, "entered_seq": 0,
        **({} if auto_during_plan else {AUTO_DURING_PLAN: False})}})


def enter_plan(ctx: ToolContext, *, default_mode: str, plans_dir: str,
               auto_during_plan: bool = True) -> bool:
    """``EnterPlanMode``: record the switch to plan mode, as ``/plan``
    does; ``False`` when the task is planning already."""
    st = ctx.state.get(NAME)
    st = st if isinstance(st, PermState) else PermState()
    current = st.mode or default_mode
    if current == "plan":
        return False
    path = (st.plan or {}).get("path") or pl.new_plan_path(plans_dir)
    ctx.record(NAME, "mode", {"mode": "plan", "plan": {
        "path": path, "pre_mode": current, "entered_seq": 0,
        **({} if auto_during_plan else {AUTO_DURING_PLAN: False})}})
    return True


async def mode_reminders(view: TaskView, mode: str) -> Sequence[Reminder]:
    """The plan-mode and auto-mode notes owed to this request (recorded as
    sent)."""
    st = state_of(view)
    out: list[Reminder] = []
    sent: list[str] = []
    plan_noted = st.plan_noted
    reminders = st.plan_reminders
    if mode == "plan" and st.plan is not None:
        newest = _newest_prompt(view)
        entered = int(st.plan.get("entered_seq") or 0)
        # A typed /plan with a request: Claude Code's text comes with the
        # call after the one that request started (recorded in m1).
        held = bool(st.plan.get("by_command")) and view.state.steps_in_turn == 0 \
            and newest == entered
        after = max(st.plan_noted, entered)
        due = "plan_active" in st.due and not held
        # Checked before every model call, as Claude Code's plan_mode
        # attachment: a prompt that arrived mid-turn counts at once.
        cadence = not due and "plan_active" not in st.due and newest > after \
            and _prompts_after(view, after) >= pl.PLAN_TURNS_BETWEEN
        if due or cadence:
            path = st.plan["path"]
            exists = await _exists(view, path)
            if "plan_reentry" in st.due and exists:
                out.append(Reminder(text=pl.plan_reentry_text(path), kind="plan_reentry",
                                    role="system", order=ORDER_PLAN - 1))
                sent.append("plan_reentry")
            reminders = 1 if due else reminders + 1
            sparse = (reminders - 1) % pl.PLAN_FULL_EVERY != 0
            out.append(Reminder(text=pl.plan_active_text(
                path, exists, sparse=sparse, subagent=view.state.parent_id is not None),
                kind="plan_active", role="system", order=ORDER_PLAN))
            sent.append("plan_active")
            plan_noted = max(newest, entered)
    if "plan_exited" in st.due and st.plan is not None and mode != "plan":
        path = st.plan["path"]
        out.append(Reminder(text=pl.plan_exited_text(path if await _exists(view, path)
                                                     else None),
                            kind="plan_exited", role="system", order=ORDER_PLAN))
        sent.append("plan_exited")
    auto_noted = st.auto_noted
    active = auto_active(mode, st.plan)
    if "auto_exited" in st.due and not active:
        if view.state.parent_id is None:
            out.append(Reminder(text=w.AUTO_MODE_EXITED, kind="auto_exited", role="system",
                                order=ORDER_AUTO))
            sent.append("auto_exited")
        auto_noted = False
    # Claude Code sends the same note in bypassPermissions mode; noeta does
    # not: with it the eval model worked around a deny rule through the
    # shell more often (parity/deviations.md).
    if mode == "auto" and not auto_noted and _shell_first(view) \
            and _guidance_due(view, st.guidance_after):
        out.append(Reminder(text=w.AUTO_MODE_GUIDANCE, kind="auto_guidance", role="system",
                            order=ORDER_AUTO))
        sent.append("auto_guidance")
        auto_noted = True
    # Owed notes a later switch made moot: the exit from auto mode when it
    # is back on (or in a sub-agent), the plan texts once out of plan mode,
    # the plan-exit note once planning again.
    stale = [k for k in st.due if k not in sent and (
        k == "auto_exited"
        or (k in ("plan_active", "plan_reentry") and mode != "plan")
        or (k == "plan_exited" and mode == "plan")
        or (k == "plan_reentry" and "plan_active" in sent))]
    if sent or stale or plan_noted != st.plan_noted or auto_noted != st.auto_noted:
        view.record(NAME, "noted", {"kinds": [*sent, *stale], "plan_noted": plan_noted,
                                    "plan_reminders": reminders, "auto_noted": auto_noted})
    return out


async def plan_file_resident(view: TaskView) -> None:
    """At a compaction: the task's plan file, when it has one with text, is
    read and held for the next request, in plan mode or not (Claude Code's
    ``plan_file_reference``)."""
    plan = state_of(view).plan
    path = plan.get("path") if isinstance(plan, dict) else None
    if view.keeps_tail is None or not isinstance(path, str):
        return
    text = await _read(view, path)
    if text and text.strip():
        view.record(NAME, "plan_file", {"path": path, "text": text})


def plan_file_reminders(view: TaskView) -> list[Reminder]:
    held = state_of(view).plan_file
    if not held:
        return []
    return [Reminder(text=pl.plan_file_text(str(held["path"]), str(held["text"])),
                     kind="plan_file_sent", data={}, role="system", order=ORDER_PLAN_FILE)]


async def _read(view: TaskView, path: str) -> str | None:
    try:
        if view.env is not None:
            return (await view.env.read_bytes(path)).decode("utf-8", "replace")
        return await asyncio.to_thread(Path(path).read_text, "utf-8")
    except Exception:  # noqa: BLE001 — absent or unreadable: no plan
        return None


def _shell_first(view: TaskView) -> bool:
    """Whether the auto-mode note fits the agent: it has the shell and a file
    tool that writes (Claude Code sends none to an agent without them, such
    as Explore)."""
    return view.find_tool("Bash") is not None and any(
        view.find_tool(name) is not None for name in ("Edit", "Write"))


def _guidance_due(view: TaskView, after: int) -> bool:
    """The auto-mode note goes with the main conversation's first request or
    the next one with input newer than seq ``after`` (a compaction), and a
    sub-agent's second (as Claude Code sends them)."""
    if view.state.parent_id is not None:
        return any(m.role == "assistant" for m in view.state.history)
    return _new_input(view, after)


async def _exists(view: TaskView, path: str) -> bool:
    try:
        if view.env is not None:
            stat = await view.env.stat(path)
            return stat is not None
    except Exception:  # noqa: BLE001
        pass
    return await asyncio.to_thread(os.path.isfile, path)


def _new_input(view: TaskView, after: int) -> bool:
    """Whether a prompt or a tool result came in after log seq ``after``."""
    return any(seq > after and m.role == "user" and (
        m.origin in (None, "human") or any(isinstance(b, ToolResult) for b in m.content))
        for seq, m in zip(view.state.message_seqs, view.state.messages, strict=False))


def _prompts_after(view: TaskView, after: int) -> int:
    """The person's prompts logged after seq ``after``."""
    return sum(1 for seq, m in zip(view.state.message_seqs, view.state.messages, strict=False)
               if m.from_person and seq > after)


def _newest_prompt(view: TaskView) -> int:
    return max((seq for seq, m in zip(view.state.message_seqs, view.state.messages,
                                      strict=False)
                if m.from_person and seq), default=0)
