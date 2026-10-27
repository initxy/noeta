"""Delegation as an extension: the ``Task`` tool, the whole-reply checks
that refuse a bad batch before anything runs, and the background
sub-agents' completion notices."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.events import Cancel, ChildEnded, InputTaken, Recorded, Say, Waiting
from noeta.ext.base import Deny, Extension, Gate, TaskView
from noeta.ext.subtasks import texts
from noeta.ext.subtasks.tool import (
    DEPTH_KEY,
    TYPE_KEY,
    UNATTENDED_KEY,
    TaskTool,
    batch_refusal,
)
from noeta.loop.state import TaskState
from noeta.tools.base import Tool
from noeta.types import ToolUse, user


@dataclass(frozen=True)
class SubagentSpec:
    """One entry of the delegation roster (0.6's ``AgentDefinition`` as the
    parent sees it).

    ``name`` is what the model passes as ``subagent_type``; ``description``
    is its roster line in the Task schema. ``agent`` names the registered
    ``Agent`` the child runs (default: ``name``) — that agent's definition
    decides the child's model, prompt, tools and extensions, so a child gets
    ``Task`` only when its own agent carries a ``Subtasks`` with a roster.
    ``config`` is merged into the child's ``TaskCreated.config``; ``queue``
    routes the child (default: the extension's ``queue``).
    """

    name: str
    description: str
    agent: str = ""
    config: Mapping[str, Any] = field(default_factory=dict)
    queue: str | None = None


def _roster(roster: Mapping[str, SubagentSpec] | Sequence[SubagentSpec]) -> dict[str, SubagentSpec]:
    specs = list(roster.values()) if isinstance(roster, Mapping) else list(roster)
    out: dict[str, SubagentSpec] = {}
    for spec in specs:
        if not spec.description.strip():
            raise ValueError(f"sub-agent {spec.name!r} needs a non-blank description")
        if spec.name in out:
            raise ValueError(f"duplicate sub-agent name {spec.name!r}")
        out[spec.name] = spec
    return out


class Subtasks(Extension):
    """Sub-agents: the ``Task`` tool and background children.

    Constructor arguments (what a host configures; they replace 0.6's
    ``Options.agents`` roster, ``max_subtask_depth`` and the background cap):

    - ``roster``: the sub-agents the model may start, as ``SubagentSpec``s
      (a mapping or a sequence). Empty: no ``Task`` tool is offered — the
      form every sub-agent without delegation carries, so the child-side
      rules below still apply to it.
    - ``max_depth``: a task at this depth may not delegate (root = 0; 0.6's
      default 3). ``None`` = unlimited.
    - ``max_background``: background sub-agents one task may have running
      at once (0.6: 8 per session).
    - ``max_fanout``: Task calls one reply may carry (0.6: 16).
    - ``exclusive_with``: tool names a Task call may not share a reply with
      (0.6 refused Task beside its other control tools; in 1.0 they are
      ordinary tools, so the default is none).
    - ``inherit_config``: parent ``TaskCreated.config`` keys a child copies
      (its workspace, and a host's per-task model/effort override, as 0.6
      children inherited them).
    - ``queue``: the queue children are created on.

    Child side (every task whose agent carries this extension): a task
    marked unattended (a background sub-agent and everything below it)
    that comes to wait on an approval or a question cancels itself with
    0.6's "needed an interaction that is unavailable to a background agent"
    explanation, which its parent reports.
    """

    name = "subtasks"

    def __init__(
        self,
        roster: Mapping[str, SubagentSpec] | Sequence[SubagentSpec] = (),
        *,
        max_depth: int | None = 3,
        max_background: int = texts.MAX_BACKGROUND,
        max_fanout: int = texts.MAX_FANOUT,
        exclusive_with: Collection[str] = (),
        inherit_config: Sequence[str] = ("cwd", "model", "effort"),
        queue: str = "default",
    ) -> None:
        self.roster = _roster(roster)
        self.max_depth = max_depth
        self.max_background = max_background
        self.max_fanout = max_fanout
        self.exclusive_with = frozenset(exclusive_with)
        self.inherit_config = tuple(inherit_config)
        self.queue = queue

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"background": {}, "noticed": []}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = state or self.initial_state()
        if kind == "background":
            return {**st, "background": {**st["background"], data["child_id"]: data["agent"]}}
        if kind == "noticed":
            return {**st, "noticed": [*st["noticed"], data["child_id"]]}
        return st

    def _state(self, state: TaskState) -> dict[str, Any]:
        st = state.ext.get(self.name)
        return st if isinstance(st, dict) else self.initial_state()

    def running_background(self, state: TaskState) -> list[str]:
        """This task's background children that have not ended."""
        return [c for c in self._state(state)["background"] if state.children.get(c) is None]

    def child_config(self, state: TaskState, spec: SubagentSpec, *,
                     background: bool) -> dict[str, Any]:
        config = {k: state.config[k] for k in self.inherit_config if k in state.config}
        config.update(spec.config)
        config[DEPTH_KEY] = int(state.config.get(DEPTH_KEY) or 0) + 1
        config[TYPE_KEY] = spec.name
        if background or state.config.get(UNATTENDED_KEY):
            config[UNATTENDED_KEY] = True
        return config

    # ------------------------------------------------------------ hooks

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return [TaskTool(self, view.state)] if self.roster else ()

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        """Checks that look at the whole reply, so that a bad batch is
        refused before any of it runs (0.6: one refusal stamped on every
        call)."""
        if not self.roster:
            return None
        calls = [c.use for c in view.state.calls] or [call]
        refusal = batch_refusal(self, view.state, calls)
        if refusal is None:
            return None
        text, every_call = refusal
        if every_call or call.name == texts.TASK_TOOL:
            return Deny(reason=text)
        return None

    async def on_start(self, view: TaskView) -> None:
        # Ends taken before a crash whose notice was never posted.
        noticed = set(self._state(view.state)["noticed"])
        for child_id, ended in list(view.state.children.items()):
            if ended is not None and child_id not in noticed:
                await self._notice(view, ended)
        waiting = view.state.waiting
        if waiting is not None and view.state.ended is None:
            await self._unattended_wait(view, waiting)

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, ChildEnded):
                    await self._notice(view, item.body)
        elif isinstance(ev, Waiting):
            await self._unattended_wait(view, ev)

    # ------------------------------------------------------------ background

    async def _notice(self, view: TaskView, ended: ChildEnded) -> None:
        """Turn a background child's end into 0.6's completion notice. It is
        posted as a ``next_turn`` message, so it waits for an open turn to
        finish and wakes an idle parent into a new turn; a cancelled child
        (its parent's own teardown) leaves nothing to say."""
        st = self._state(view.state)
        agent = st["background"].get(ended.child_id)
        if agent is None or ended.child_id in st["noticed"]:
            return
        notice: str | None = None
        if ended.status == "done":
            notice = texts.BACKGROUND_NOTICE.format(
                summary=texts.BACKGROUND_FINISHED.format(agent=agent), result=ended.answer,
                id=ended.child_id, status="completed")
        elif ended.status == "failed" or ended.error == texts.BACKGROUND_NEEDED_HUMAN:
            notice = texts.BACKGROUND_NOTICE.format(
                summary=texts.BACKGROUND_FAILED.format(agent=agent),
                result=ended.error or texts.BACKGROUND_NEEDED_HUMAN,
                id=ended.child_id, status="failed")
        data = {"child_id": ended.child_id, "status": ended.status}
        if notice is None:
            view.record(self.name, "noticed", data)
            return
        message = user(notice, origin="system")
        if view.post is None:
            # No inbox access (an older core): the notice still lands in
            # the conversation, but cannot wake an idle task.
            view.record(self.name, "noticed", data, message=message)
            return
        await view.post(Say(message=message, when="next_turn"),
                        dedupe_key=f"subtasks:notice:{ended.child_id}")
        view.record(self.name, "noticed", data)

    async def _unattended_wait(self, view: TaskView, waiting: Waiting) -> None:
        if waiting.on not in ("approval", "question"):
            return
        if not view.state.config.get(UNATTENDED_KEY) or view.post is None:
            return
        await view.post(Cancel(reason=texts.BACKGROUND_NEEDED_HUMAN),
                        dedupe_key=f"subtasks:unattended:{view.task_id}")
