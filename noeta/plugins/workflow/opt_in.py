"""How the person opts into orchestration, as Claude Code tells the model:
the keyword ``ultracode`` in a prompt (a note for that turn), and the
task's ``ultracode`` switch (config, changeable while it runs): a note when
it is on, again every :data:`ULTRACODE_EVERY` prompts, and one when it is
turned off. Checked when the person's new prompts first reach the model, in
the main conversation, while ``Workflow`` is offered; what was told is in
the task's log, so any worker tells the same."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.extension import Reminder, TaskView
from noeta.core.types import ToolResult

__all__ = ["KEYWORD", "ULTRACODE_KEY", "opt_in_reminders", "reduce_opt_in"]

#: The opt-in keyword (Claude Code's), a word of the person's prompt.
KEYWORD = re.compile(r"\bultracode\b", re.IGNORECASE)
#: Task config: ultracode is on for the task.
ULTRACODE_KEY = "ultracode"
#: Prompts between two "still on" notes (Claude Code's 10).
ULTRACODE_EVERY = 10

KEYWORD_NOTE = (
    'The user\'s message contains the keyword "ultracode": for this turn they opt into '
    "multi-agent orchestration, so carry out the request with the Workflow tool.")
ULTRACODE_ON = (
    "Ultracode is on: aim for the most thorough and correct answer, not the quickest or the "
    "cheapest. Use the Workflow tool for every substantial task; token cost does not matter. "
    "Work alone only on conversational or trivial turns.")
ULTRACODE_STILL = "Ultracode is still on: keep using the Workflow tool."
ULTRACODE_OFF = "Ultracode is off: the Workflow tool's usual opt-in rule applies again."


def reduce_opt_in(st: Mapping[str, Any], kind: str, data: Any) -> dict[str, Any] | None:
    """The plugin state after one of these records, or ``None`` for another
    kind."""
    if kind not in ("prompts_seen", "keyword", "ultracode") or not isinstance(data, dict):
        return None
    out = {**st, "prompts_through": int(data.get("through") or 0)}
    if kind == "ultracode":
        out["ultracode"] = {"mode": data.get("mode"), "seq": out["prompts_through"]}
    return out


def _prompts(view: TaskView, after: int) -> list[tuple[int, str]]:
    """The person's prompts logged after seq ``after``."""
    return [(seq, m.text) for seq, m in zip(view.state.message_seqs, view.state.messages,
                                            strict=False)
            if seq > after and m.from_person and m.origin == "human"
            and not any(isinstance(b, ToolResult) for b in m.content)]


def opt_in_reminders(view: TaskView, st: Mapping[str, Any]) -> Sequence[Reminder]:
    """The opt-in notes owed with this request (recorded as told)."""
    if view.state.parent_id is not None or view.find_tool("Workflow") is None:
        return ()
    new = _prompts(view, int(st.get("prompts_through") or 0))
    if not new:
        return ()
    through = new[-1][0]
    out: list[Reminder] = []
    if any(KEYWORD.search(text) for _seq, text in new):
        out.append(Reminder(text=KEYWORD_NOTE, kind="keyword", data={"through": through}))
    last = st.get("ultracode") if isinstance(st.get("ultracode"), dict) else None
    compaction = view.state.compaction
    if last is not None and compaction is not None and compaction.through >= last["seq"]:
        last = None  # the note went with the summarized part
    entered = last is not None and last.get("mode") == "enter"
    mode = None
    if view.state.config.get(ULTRACODE_KEY):
        if not entered:
            mode, text = "enter", ULTRACODE_ON
        elif last is not None and len(_prompts(view, int(last["seq"]))) >= ULTRACODE_EVERY:
            mode, text = "enter", ULTRACODE_STILL
    elif entered:
        mode, text = "exit", ULTRACODE_OFF
    if mode is not None:
        out.append(Reminder(text=text, kind="ultracode", data={"mode": mode,
                                                               "through": through}))
    if not out:
        view.record("workflow", "prompts_seen", {"through": through})
    return out
