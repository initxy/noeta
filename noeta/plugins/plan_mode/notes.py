"""Entering plan mode (its plan file and name), and the plan-mode notes
owed to the next request."""

from __future__ import annotations

import asyncio
import os
import posixpath
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from noeta.core.extension import Reminder, TaskView
from noeta.plugins.plan_mode import plan as pl
from noeta.plugins.plan_mode.state import (
    AUTO_DURING_PLAN,
    MODE_RECORDS,
    NAME,
    PlanState,
    plan_classifier_on,
    state_of,
)

#: Places in the joined system turn (``Reminder.order``): before the token
#: budget, after the skill list, ahead of the auto-mode notes.
ORDER_PLAN = 64
#: The plan file given again after a compaction: after the files read
#: again and the background tasks' status, ahead of the invoked skills
#: (Claude Code's order).
ORDER_PLAN_FILE = 15
#: The local command wrapper ``/plan`` sends (Claude Code's tags).
PLAN_COMMAND = ("<command-name>/plan</command-name>\n            <command-message>plan"
                "</command-message>\n            <command-args>{args}</command-args>\n\n"
                "<local-command-stdout>Enabled plan mode</local-command-stdout>")
PLAN_COMMAND_HEAD = "<command-name>/plan</command-name>"


def plan_record(st: PlanState, current: str, plans_dir: str, seq: int, *,
                auto_during_plan: bool, by_command: bool = False) -> dict[str, Any]:
    """The ``mode`` record of a switch from ``current`` into plan mode: the
    task's plan file (kept from an earlier plan), and whether this plan runs
    with the auto-mode classifier (:func:`plan_classifier_on`)."""
    path = (st.plan or {}).get("path") or pl.new_plan_path(plans_dir)
    return {"mode": "plan", "plan": {
        "path": path, "pre_mode": current, "entered_seq": seq,
        **({"by_command": True} if by_command else {}),
        AUTO_DURING_PLAN: plan_classifier_on(current, auto_during_plan=auto_during_plan)}}


def plan_start(view: TaskView, pre: str, *, plans_dir: str, parent_plan: Any = None,
               classify: bool) -> None:
    """A task that starts in plan mode (its config's ``permission_mode``, a
    sub-agent of a planning task): recorded as entered with a plan file of
    its own, so it plans from its first request. A sub-agent's is named
    after its parent's plan (``parent_plan``): ``<name>-agent-<task id>.md``,
    as Claude Code names it."""
    path = (posixpath.join(plans_dir, f"{parent_plan}-agent-{view.task_id}.md")
            if isinstance(parent_plan, str) and pl.plan_name_ok(parent_plan)
            else pl.new_plan_path(plans_dir))
    view.record(MODE_RECORDS, "mode", {"mode": "plan", "plan": {
        "path": path, "pre_mode": pre, "entered_seq": 0, AUTO_DURING_PLAN: classify}})


async def plan_reminders(view: TaskView, st: PlanState) -> Sequence[Reminder]:
    """The plan-mode notes owed to this request (recorded as sent)."""
    out: list[Reminder] = []
    sent: list[str] = []
    plan_noted = st.plan_noted
    reminders = st.plan_reminders
    planning = st.mode == "plan"
    if planning and st.plan is not None:
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
    if "plan_exited" in st.due and st.plan is not None and not planning:
        path = st.plan["path"]
        out.append(Reminder(text=pl.plan_exited_text(path if await _exists(view, path)
                                                     else None),
                            kind="plan_exited", role="system", order=ORDER_PLAN))
        sent.append("plan_exited")
    # Owed notes a later switch made moot: the plan texts once out of plan
    # mode, the plan-exit note once planning again.
    stale = [k for k in st.due if k not in sent and (
        (k in ("plan_active", "plan_reentry") and not planning)
        or (k == "plan_exited" and planning)
        or (k == "plan_reentry" and "plan_active" in sent))]
    if sent or stale or plan_noted != st.plan_noted:
        view.record(NAME, "noted", {"kinds": [*sent, *stale], "plan_noted": plan_noted,
                                    "plan_reminders": reminders})
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


async def _exists(view: TaskView, path: str) -> bool:
    try:
        if view.env is not None:
            stat = await view.env.stat(path)
            return stat is not None
    except Exception:  # noqa: BLE001
        pass
    return await asyncio.to_thread(os.path.isfile, path)


def _prompts_after(view: TaskView, after: int) -> int:
    """The person's prompts logged after seq ``after``."""
    return sum(1 for seq, m in zip(view.state.message_seqs, view.state.messages, strict=False)
               if m.from_person and seq > after)


def _newest_prompt(view: TaskView) -> int:
    return max((seq for seq, m in zip(view.state.message_seqs, view.state.messages,
                                      strict=False)
                if m.from_person and seq), default=0)
