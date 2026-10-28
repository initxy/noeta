"""Delegation as an extension: the ``Agent`` tool and its companions, the
whole-reply checks that refuse a bad batch before anything runs, the
sub-agent's hand-back, and the completion notices."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import msgspec

from noeta.core import models
from noeta.core.events import ChildEnded, External, InputTaken, ModelReplied, Recorded, Say
from noeta.core.extension import Deny, Extension, Gate, Reminder, Rewrite, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.tool import Tool, ToolContext
from noeta.core.types import Message, ModelRequest, ToolUse, system_message, user
from noeta.plugins.subtasks import texts
from noeta.plugins.subtasks.tool import (
    DELEGATION_TOOLS,
    DEPTH_KEY,
    HANDBACK_KEY,
    TYPE_KEY,
    AgentTool,
    HandbackTool,
    ListAgentsTool,
    SendMessageTool,
    batch_refusal,
    handback_sender,
)
from noeta.toolkit.notices import NOTICE_GUARD, escape

log = logging.getLogger("noeta.plugins.subtasks")

#: ``agents=["*"]``: every agent of the worker that has a description.
ALL_AGENTS = "*"
#: The sub-agent's model call that also asks for a progress label (Claude
#: Code's was alongside the third), and the inbox signal carrying it.
PROGRESS_AT_CALL = 3
PROGRESS_SIGNAL = "subtasks.progress"
#: The permission mode in which agents hand back with ``SubagentHandback``.
AUTO_MODE = "auto"
#: The agent list's place in the first request's joined system turn
#: (``Reminder.order``): after the environment and the deferred tools.
AGENT_LIST_ORDER = 40


@dataclass(frozen=True)
class SubagentSpec:
    """One agent type the model may start.

    ``name`` is what the model passes as ``subagent_type``; ``description``
    its line in the agent list. ``agent`` names the registered ``Agent`` the
    child runs (default: ``name``), whose definition decides the child's
    model, prompt and tools. ``config`` is merged into the child's
    ``TaskCreated.config``; ``queue`` routes the child (default: the
    extension's ``queue``). ``tools`` is the agent list's tools column.
    """

    name: str
    description: str
    agent: str = ""
    config: Mapping[str, Any] = field(default_factory=dict)
    queue: str | None = None
    tools: str = "*"


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


def _tools_column(agent: Any) -> str:
    allowed = getattr(agent, "allowed_tools", None)
    denied = list(getattr(agent, "denied_tools", ()) or ())
    if allowed is not None:
        return ", ".join(allowed)
    if denied:
        return "All tools except " + ", ".join(denied)
    return "*"


def task_notification(*, agent_id: str, call_id: str, status: str, summary: str,
                      result: str, ended: ChildEnded, duration_ms: int) -> str:
    """A finished agent's ``<task-notification>``, behind the not-the-user guard."""
    usage = ended.last_usage
    tokens = usage.input + usage.output if usage is not None else 0
    return (f"{NOTICE_GUARD}\n\n<task-notification>\n<task-id>{agent_id}</task-id>\n"
            f"<tool-use-id>{call_id}</tool-use-id>\n<status>{status}</status>\n"
            f"<summary>{escape(summary)}</summary>\n<note>{texts.NOTICE_NOTE}</note>\n"
            f"<result>{escape(result)}\n</result>\n"
            f"<usage><subagent_tokens>{tokens}</subagent_tokens><tool_uses>{ended.tool_calls}"
            f"</tool_uses><duration_ms>{duration_ms}</duration_ms></usage>\n"
            "</task-notification>")


class Subtasks(Extension):
    """Sub-agents: ``Agent``, ``SendMessage``, ``ListAgents`` and, in a task
    ``Agent`` started, ``SubagentHandback``.

    Constructor arguments (what a host configures):

    - ``roster``: agent types the model may start, as ``SubagentSpec``s.
    - ``agents``: agent types named by agent: each runs that agent, and its
      line is that agent's ``description`` (``TaskView.agents``); ``"*"``
      names every agent with a description except the task's own. With no
      type at all, no ``Agent`` tool is offered — the form a sub-agent
      without delegation carries, so the child-side tools still apply.
    - ``max_depth``: a task at this depth may not delegate (root = 0).
    - ``max_background``: agents one task may have running at once.
    - ``max_fanout``: Agent calls one reply may carry.
    - ``exclusive_with``: tool names an Agent call may not share a reply with.
    - ``inherit_config``: parent ``TaskCreated.config`` keys a child copies.
      ``model`` and ``effort`` are copied only when neither the call nor the
      agent's definition sets them.
    - ``queue``: the queue children are created on.
    - ``progress_labels``: a sub-agent asks the model once, beside its third
      call, for a 3-5 word label of what it is doing; front ends get it as a
      notice, the model never sees it.
    """

    name = "subtasks"

    def __init__(
        self,
        roster: Mapping[str, SubagentSpec] | Sequence[SubagentSpec] = (),
        *,
        agents: Sequence[str] = (),
        max_depth: int | None = 3,
        max_background: int = texts.MAX_BACKGROUND,
        max_fanout: int = texts.MAX_FANOUT,
        exclusive_with: Collection[str] = (),
        inherit_config: Sequence[str] = ("cwd", "model", "effort"),
        queue: str = "default",
        progress_labels: bool = True,
    ) -> None:
        self.roster = _roster(roster)
        self.progress_labels = progress_labels
        self._labels: set[asyncio.Task[None]] = set()
        self.agents = tuple(agents)
        self._resolved: dict[tuple[Any, ...], dict[str, SubagentSpec]] = {}
        self.max_depth = max_depth
        self.max_background = max_background
        self.max_fanout = max_fanout
        self.exclusive_with = frozenset(exclusive_with)
        self.inherit_config = tuple(inherit_config)
        self.queue = queue

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"agents": {}, "tasks": {}, "handbacks": {}, "pending": {}, "noticed": [],
                "reminded": False, "listed": False, "held_list": None}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = {**self.initial_state(), **(state or {})}
        if kind == "launched":
            agent_id = data["agent_id"]
            handbacks = {k: v for k, v in st["handbacks"].items() if k != agent_id}
            return {**st, "agents": {**st["agents"], agent_id: data},
                    "tasks": {**st["tasks"], data["task_id"]: agent_id}, "handbacks": handbacks}
        if kind == "handback":
            return {**st, "handbacks": {**st["handbacks"], data["agent_id"]: "taken"}}
        if kind == "seen":
            return {**st, "handbacks": {k: "seen" for k in st["handbacks"]}}
        if kind == "pending":
            return {**st, "pending": {**st["pending"], data["task_id"]: data}}
        if kind == "noticed":
            pending = {k: v for k, v in st["pending"].items() if k != data["task_id"]}
            noticed = st["noticed"] if data["task_id"] in st["noticed"] \
                else [*st["noticed"], data["task_id"]]
            return {**st, "pending": pending, "noticed": noticed}
        if kind == "handback_reminder":
            return {**st, "reminded": True}
        if kind == "agent_list":
            held = data.get("text") if isinstance(data, dict) else None
            return {**st, "listed": True, "held_list": held}
        if kind == "agent_list_sent":
            return {**st, "held_list": None}
        return st

    def _state(self, state: TaskState) -> dict[str, Any]:
        st = state.ext.get(self.name)
        return {**self.initial_state(), **st} if isinstance(st, dict) else self.initial_state()

    def agents_of(self, state: TaskState) -> dict[str, dict[str, Any]]:
        """The agents this task launched, by agent id (the newest task of each)."""
        return dict(self._state(state)["agents"])

    def running(self, state: TaskState) -> list[str]:
        """Agent ids whose newest task has not ended."""
        return [a for a, d in self._state(state)["agents"].items()
                if state.children.get(d["task_id"]) is None]

    def child_config(self, state: TaskState, ctx: ToolContext, agent: str,
                     spec_config: Mapping[str, Any], kind: str, args: Mapping[str, Any],
                     *, fork: bool) -> dict[str, Any]:
        config = {k: state.config[k] for k in self.inherit_config if k in state.config}
        config.update(spec_config)
        if not fork:
            if isinstance(args.get("model"), str):
                config["model"] = models.resolve_alias(args["model"])
            if isinstance(args.get("effort"), str):
                config["effort"] = args["effort"]
        config[DEPTH_KEY] = int(state.config.get(DEPTH_KEY) or 0) + 1
        config[TYPE_KEY] = kind
        return config

    async def add_worktree(self, ctx: ToolContext, config: Mapping[str, Any]
                           ) -> dict[str, str] | str:
        """A fresh git worktree for an isolated agent, or why there is none."""
        env = ctx.env
        if env is None:
            return texts.WORKTREE_FAILED.format(reason="this task has no execution environment")
        cwd = str(config.get("cwd") or env.cwd)
        top = await env.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
        if top.exit_code != 0:
            return texts.WORKTREE_FAILED.format(reason=f"{cwd} is not in a git repository")
        root = top.stdout.decode().strip()
        base = (await env.run(["git", "rev-parse", "HEAD"], cwd=root)).stdout.decode().strip()
        name = "agent-" + re.sub(r"[^A-Za-z0-9_-]", "", ctx.call_id)[-16:]
        path, branch = f"{root}/.noeta/worktrees/{name}", f"worktree-{name}"
        made = await env.run(["git", "worktree", "add", "-b", branch, path, "HEAD"], cwd=root)
        if made.exit_code != 0:
            reason = made.stderr.decode(errors="replace").strip() or "git worktree add failed"
            return texts.WORKTREE_FAILED.format(reason=reason)
        return {"path": path, "branch": branch, "base": base, "root": root}

    async def _close_worktree(self, view: TaskView, tree: Mapping[str, str]) -> str:
        """Remove an unchanged worktree; otherwise say where the work is."""
        env = view.env
        if env is None:
            return ""
        status = await env.run(["git", "status", "--porcelain"], cwd=tree["path"])
        ahead = await env.run(["git", "rev-list", "--count", f"{tree['base']}..HEAD"],
                              cwd=tree["path"])
        if status.exit_code == 0 and not status.stdout.strip() \
                and ahead.stdout.decode().strip() == "0":
            await env.run(["git", "worktree", "remove", "--force", tree["path"]],
                          cwd=tree["root"])
            await env.run(["git", "branch", "-D", tree["branch"]], cwd=tree["root"])
            return ""
        return texts.WORKTREE_KEPT.format(path=tree["path"], branch=tree["branch"])

    # ------------------------------------------------------------ tools

    def roster_for(self, view: TaskView) -> dict[str, SubagentSpec]:
        """The agent types: the given specs, plus each named agent the worker
        has (a name it lacks is left out)."""
        if not self.agents:
            return self.roster
        names = [n for n in view.agents if n != view.state.agent] \
            if ALL_AGENTS in self.agents else [n for n in self.agents if n in view.agents]
        named = tuple((n, view.agents[n].description, _tools_column(view.agents[n]),
                       _freeze(_definition_config(view.agents[n])))
                      for n in names if view.agents[n].description.strip())
        roster = self._resolved.get(named)
        if roster is None:
            roster = _roster([*self.roster.values(),
                              *(SubagentSpec(name=n, description=d, tools=t, config=dict(c))
                                for n, d, t, c in named if n not in self.roster)])
            self._resolved[named] = roster
        return roster

    def tools(self, view: TaskView) -> Sequence[Tool]:
        state = view.state
        roster = self.roster_for(view)
        out: list[Tool] = []
        if roster:
            out.append(AgentTool(self, state, roster, handback=_auto_mode(self, view)))
            if not state.parent_id:
                out.append(ListAgentsTool(self, state))
        if roster or state.parent_id:
            out.append(SendMessageTool(self, state))
        if _hands_back(state.config):
            out.append(HandbackTool(state))
        return out

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        """``Task`` is the old name of ``Agent``; checks that look at the
        whole reply refuse a bad batch before any of it runs."""
        roster = self.roster_for(view)
        if not roster:
            return None
        if call.name == texts.LEGACY_AGENT_TOOL:
            return Rewrite(name=texts.AGENT_TOOL, input=call.input)
        calls = [c.use for c in view.state.calls] or [call]
        refusal = batch_refusal(self, view.state, calls, roster)
        if refusal is None:
            return None
        text, every_call = refusal
        if every_call or call.name in DELEGATION_TOOLS:
            return Deny(reason=text)
        return None

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        """Beside a sub-agent's third call, ask for its progress label: the
        same request with one more user text, answered in the background."""
        state = view.state
        if (self.progress_labels and TYPE_KEY in state.config and view.complete is not None
                and view.post is not None and state.steps_in_turn == PROGRESS_AT_CALL - 1):
            ask = msgspec.structs.replace(
                request, messages=[*request.messages, user(texts.PROGRESS_INSTRUCTION)])
            job = asyncio.ensure_future(self._label(view, ask, state.last_seq))
            self._labels.add(job)
            job.add_done_callback(self._labels.discard)
        return request

    async def _label(self, view: TaskView, request: ModelRequest, at: int) -> None:
        assert view.complete is not None and view.post is not None
        try:
            response = await view.complete(request)
            label = response.message.text.strip().split("\n", 1)[0][:80]
            if label:
                await view.post(External(name=PROGRESS_SIGNAL, data={"label": label}),
                                dedupe_key=f"{PROGRESS_SIGNAL}:{view.task_id}:{at}")
        except Exception:  # a label is decoration; it never fails the run
            log.debug("progress label for %s failed", view.task_id, exc_info=True)

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        st = self._state(view.state)
        out: list[Reminder] = []
        if _hands_back(view.state.config) and not st["reminded"]:
            out.append(Reminder(text=texts.HANDBACK_REMINDER, kind="handback_reminder"))
        if st["held_list"]:
            # The first request: the agent list joins the environment's
            # system turn, after the deferred tools.
            out.append(Reminder(text=st["held_list"], kind="agent_list_sent", data={},
                                role="system", order=AGENT_LIST_ORDER))
        return out

    # ------------------------------------------------------------ events

    def agent_list(self, view: TaskView) -> str | None:
        """The agent types this task can start, one line each with its tools."""
        roster = self.roster_for(view)
        if not roster:
            return None
        lines = [f"- {s.name}: {s.description} (Tools: {s.tools})"
                 for s in sorted(roster.values(), key=lambda s: s.name.lower())]
        return "\n".join([texts.AGENT_LIST_HEAD, *lines, "", texts.AGENT_LIST_TAIL])

    async def resident(self, view: TaskView) -> Sequence[Message]:
        text = None if view.state.parent_id else self.agent_list(view)
        return [system_message(text)] if text else []

    async def on_start(self, view: TaskView) -> None:
        st = self._state(view.state)
        # Only the main conversation is told the agent types (as Claude
        # Code's sub-agents are not).
        text = None if view.state.parent_id else self.agent_list(view)
        if text and not st["listed"]:
            # Held for the first request's system turn (``reminders``).
            view.record(self.name, "agent_list", {"text": text})
        # Ends taken before a crash whose notice was never recorded.
        done = set(st["noticed"]) | set(st["pending"])
        for task_id in st["tasks"]:
            ended = view.state.children.get(task_id)
            if ended is not None and task_id not in done:
                await self._ended(view, ended, at=None, handbacks=dict(st["handbacks"]))

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        st = self._state(view.state)
        if isinstance(ev, InputTaken):
            handbacks = dict(st["handbacks"])
            for item in ev.items:
                body = item.body
                if isinstance(body, External) and body.name == PROGRESS_SIGNAL:
                    view.notify(str((body.data or {}).get("label") or ""))
                elif isinstance(body, Say):
                    sender = handback_sender(body.message)
                    if sender is not None and sender in st["agents"]:
                        handbacks[sender] = "taken"
                        view.record(self.name, "handback", {"agent_id": sender})
                elif isinstance(body, ChildEnded) and body.child_id in st["tasks"]:
                    await self._ended(view, body, at=item.at, handbacks=handbacks)
        elif isinstance(ev, ModelReplied):
            if any(v == "taken" for v in st["handbacks"].values()):
                view.record(self.name, "seen")
            seen = {k for k, v in st["handbacks"].items() if v in ("taken", "seen")}
            for task_id, data in st["pending"].items():
                if st["tasks"].get(task_id) in seen:
                    self._deliver(view, task_id, data["text"])

    def _deliver(self, view: TaskView, task_id: str, text: str) -> None:
        tools_used = texts.TOOLS_BEFORE_SUMMARY.format(tool=texts.HANDBACK_TOOL)
        view.record(self.name, "tools_used", message=user(tools_used, origin="system"))
        view.record(self.name, "noticed", {"task_id": task_id},
                    message=user(text, origin="system"))

    async def _ended(self, view: TaskView, ended: ChildEnded, *, at: float | None,
                     handbacks: Mapping[str, str]) -> None:
        """Turn an agent's end into its completion notice. After a hand-back
        the notice waits until the model has answered the report and then
        rides along with the next request; otherwise it wakes the task."""
        st = self._state(view.state)
        agent_id = st["tasks"][ended.child_id]
        agent = st["agents"][agent_id]
        description = agent.get("description") or agent.get("type") or agent_id
        handed_back = ended.status == "done" and agent_id in handbacks
        if ended.status == "done":
            status, summary = "completed", texts.SUMMARY_FINISHED.format(description=description)
            if handed_back:
                result = texts.RESULT_POINTER.format(id=agent_id)
            elif (agent.get("config") or {}).get(HANDBACK_KEY, True):
                result = texts.RESULT_NO_REPORT
            else:  # no SubagentHandback offered: its last reply is the report
                result = ended.answer
        elif ended.status == "failed":
            error = ended.error or texts.SUBAGENT_FAILED
            status = "failed"
            summary = texts.SUMMARY_FAILED.format(description=description, error=error)
            result = error
        else:
            status, summary = "killed", texts.SUMMARY_STOPPED.format(description=description)
            result = ended.error or ""
        tree = agent.get("worktree")
        if tree and agent["task_id"] == ended.child_id:
            try:
                kept = await self._close_worktree(view, tree)
            except Exception:
                log.exception("closing worktree %s failed", tree.get("path"))
                kept = texts.WORKTREE_KEPT.format(path=tree["path"], branch=tree["branch"])
            if kept:
                result = f"{result}\n{kept}"
        launched = agent.get("at")
        duration = int(max(0.0, (at - launched) * 1000)) if at and launched else 0
        text = task_notification(agent_id=agent_id, call_id=agent.get("call_id", ""),
                                 status=status, summary=summary, result=result, ended=ended,
                                 duration_ms=duration)
        if handed_back:
            if handbacks.get(agent_id) == "seen":
                self._deliver(view, ended.child_id, text)
            else:
                view.record(self.name, "pending", {"task_id": ended.child_id, "text": text})
            return
        data = {"task_id": ended.child_id}
        message = user(text, origin="system")
        if view.post is None:
            view.record(self.name, "noticed", data, message=message)
            return
        await view.post(Say(message=message, when="now"),
                        dedupe_key=f"subtasks:notice:{ended.child_id}")
        view.record(self.name, "noticed", data)


def _hands_back(config: Mapping[str, Any]) -> bool:
    """Whether a task is an agent that delivers its report with
    ``SubagentHandback`` (older logs: every agent)."""
    return TYPE_KEY in config and bool(config.get(HANDBACK_KEY, True))


def _auto_mode(owner: Extension, view: TaskView) -> bool:
    """Whether agents this task starts run in auto mode, as the permission
    mode they inherit says (Claude Code offers ``SubagentHandback`` only
    there); no mode inherited (no permission plugin) counts as auto."""
    inherited: dict[str, Any] = {}
    for ext in view.extensions:
        if ext is not owner:
            inherited.update(ext.inherit(view.state))
    return bool(inherited.get("permission_mode", AUTO_MODE) == AUTO_MODE)


def _definition_config(agent: Any) -> dict[str, Any]:
    """Child config an agent's definition asks for: its own model and effort
    (so the parent's are not inherited over them) and permission mode."""
    extra = getattr(agent, "extra", None) or {}
    out: dict[str, Any] = {}
    if extra.get("model"):
        out["model"] = models.resolve_alias(str(extra["model"]))
    if getattr(agent, "effort", None):
        out["effort"] = agent.effort
    if extra.get("permission_mode"):
        out["permission_mode"] = extra["permission_mode"]
    if extra.get("isolation") == "worktree":
        out["isolation"] = "worktree"
    return out


def _freeze(value: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(value.items()))
