"""Delegation as an extension: the ``Agent`` tool and its companions, the
whole-reply checks that refuse a bad batch before anything runs, the
sub-agent's hand-back, and the completion notices."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Mapping, Sequence
from typing import Any

import msgspec

from noeta.core import models
from noeta.core.events import (
    ChildEnded,
    External,
    InputTaken,
    ModelReplied,
    Recorded,
    Say,
    TaskEnded,
    Waiting,
)
from noeta.core.execenv import ExecEnv
from noeta.core.extension import Deny, Extension, Gate, Reminder, Rewrite, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.tool import Tool, ToolContext
from noeta.core.types import Message, ModelRequest, ToolUse, user
from noeta.plugins.subtasks import idle, texts
from noeta.plugins.subtasks.roster import Roster, SubagentSpec, check_roster
from noeta.plugins.subtasks.roster import agent_list as listing
from noeta.plugins.subtasks.tool import (
    AGENT_ID_KEY,
    DELEGATION_TOOLS,
    DEPTH_KEY,
    HANDBACK_KEY,
    TRANSCRIPT_KEY,
    TYPE_KEY,
    AgentTool,
    HandbackTool,
    ListAgentsTool,
    SendMessageTool,
    batch_refusal,
    call_refusal,
    handback_sender,
    transcript_file,
)
from noeta.plugins.subtasks.worktree import close_worktree
from noeta.toolkit import notices
from noeta.toolkit.notices import NOTICE_GUARD, escape

__all__ = ["SubagentSpec", "Subtasks", "task_notification"]

log = logging.getLogger("noeta.plugins.subtasks")

#: The sub-agent's model call that also asks for a progress label (Claude
#: Code's was alongside the third), and the inbox signal carrying it.
PROGRESS_AT_CALL = 3
PROGRESS_SIGNAL = "subtasks.progress"
#: Tasks whose transcript progress a process remembers at most.
TRANSCRIBED_KEEP = 256
#: The permission mode in which agents hand back with ``SubagentHandback``.
AUTO_MODE = "auto"
#: The agent list's place in the first request's joined system turn
#: (``Reminder.order``): after the environment and the deferred tools.
AGENT_LIST_ORDER = 40


def task_notification(*, agent_id: str, call_id: str, status: str, summary: str,
                      result: str, ended: ChildEnded, duration_ms: int,
                      output_file: str = "") -> str:
    """A finished agent's ``<task-notification>``, behind the not-the-user guard."""
    usage = ended.last_usage
    tokens = usage.input + usage.output if usage is not None else 0
    fields = [("task-id", agent_id), ("tool-use-id", call_id)]
    if output_file:
        fields.append(("output-file", escape(output_file)))
    element = notices.task_notification(
        (*fields, ("status", status), ("summary", escape(summary))),
        body=(f"\n<note>{texts.NOTICE_NOTE}</note>\n<result>{escape(result)}\n</result>\n"
              f"<usage><subagent_tokens>{tokens}</subagent_tokens><tool_uses>"
              f"{ended.tool_calls}</tool_uses><duration_ms>{duration_ms}</duration_ms></usage>"))
    return f"{NOTICE_GUARD}\n\n{element}"


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
        self.roster = check_roster(roster)
        self.progress_labels = progress_labels
        self._labels: set[asyncio.Task[None]] = set()
        #: Per task: what of its conversation this process last wrote to its
        #: transcript, ``(messages, last message seq, file size)`` (a new
        #: process writes it whole once, then appends). The oldest beyond
        #: :data:`TRANSCRIBED_KEEP` are forgotten (tasks another worker took).
        self._transcribed: dict[str, tuple[int, int, int]] = {}
        self.agents = tuple(agents)
        self._roster = Roster(self.roster, self.agents)
        self.max_depth = max_depth
        self.max_background = max_background
        self.max_fanout = max_fanout
        self.exclusive_with = frozenset(exclusive_with)
        self.inherit_config = tuple(inherit_config)
        self.queue = queue

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"agents": {}, "tasks": {}, "handbacks": {}, "pending": {}, "noticed": [],
                "reminded": False, "listed": False, "held_list": None, **idle.initial()}

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
        return idle.reduce(st, kind, data) or st

    def _state(self, state: TaskState) -> dict[str, Any]:
        st = state.ext.get(self.name)
        return {**self.initial_state(), **st} if isinstance(st, dict) else self.initial_state()

    def agents_of(self, state: TaskState) -> dict[str, dict[str, Any]]:
        """The agents this task launched, by agent id (the newest task of each)."""
        return dict(self._state(state)["agents"])

    def idle_waits(self, state: TaskState) -> dict[str, dict[str, Any]]:
        """The sessions this task waits to hear are idle, by task id."""
        return dict(self._state(state)["waits"])

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

    # ------------------------------------------------------------ tools

    def roster_for(self, view: TaskView) -> dict[str, SubagentSpec]:
        """The agent types: the given specs, plus each named agent the worker
        has (a name it lacks is left out)."""
        return self._roster.for_view(view)

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
        """``Task`` is the old name of ``Agent``; a bad Agent call is refused
        by itself, and limits on the whole reply before any of it runs."""
        roster = self.roster_for(view)
        if not roster:
            return None
        if call.name == texts.LEGACY_AGENT_TOOL:
            return Rewrite(name=texts.AGENT_TOOL, input=call.input)
        if call.name in DELEGATION_TOOLS:
            refused = call_refusal(self, view.state, call, roster)
            if refused is not None:
                return Deny(reason=refused)
        calls = [c.use for c in view.state.calls] or [call]
        refusal = batch_refusal(self, view.state, calls)
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
        if st["held_list"] and (not view.state.parent_id or _replied(view.state.messages)):
            # The first request (a sub-agent's second, as Claude Code's):
            # the agent list joins the environment's system turn, after the
            # deferred tools.
            out.append(Reminder(text=st["held_list"], kind="agent_list_sent", data={},
                                role="system", order=AGENT_LIST_ORDER))
        return out

    # ------------------------------------------------------------ events

    def agent_list(self, view: TaskView) -> str | None:
        """The agent types this task can start, one line each with its tools."""
        return listing(self.roster_for(view))

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After a compaction the agent list goes out again with the next
        request, in the environment's system turn."""
        text = None if view.state.parent_id else self.agent_list(view)
        if text and view.keeps_tail is not None:
            view.record(self.name, "agent_list", {"text": text})
        return ()

    async def on_start(self, view: TaskView) -> None:
        st = self._state(view.state)
        text = self.agent_list(view)
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
        if view.state.config.get(TRANSCRIPT_KEY):
            await self._transcribe(view, ended=isinstance(ev, TaskEnded))
        st = self._state(view.state)
        if isinstance(ev, InputTaken):
            idle.on_input(self.name, view, st, ev.items)
        elif not isinstance(ev, (Waiting, TaskEnded)):
            # A turn that just closed with nothing queued: the task is idle.
            idle.settle(self.name, view, st["holds"], at=recorded.at)
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

    async def _transcribe(self, view: TaskView, *, ended: bool) -> None:
        """Bring this agent's transcript file up to its conversation when the
        conversation changed: a projection of the log, so a crash or another
        worker loses nothing. New messages are appended; the file is written
        whole when this process has not written it yet, when the
        conversation was rewritten (a compaction), or when the file is not
        what this process left (another worker wrote it)."""
        state, env = view.state, view.env
        count = len(state.messages)
        last = state.message_seqs[-1] if state.message_seqs else 0
        known = self._transcribed.pop(state.task_id, None)
        if env is not None and (known is None or known[:2] != (count, last)):
            agent_id = str(state.config.get(AGENT_ID_KEY) or state.task_id)
            path = transcript_file(str(state.config[TRANSCRIPT_KEY]), agent_id)
            known = await self._write_transcript(env, path, state, known)
        if known is not None and not ended:
            self._transcribed[state.task_id] = known
            while len(self._transcribed) > TRANSCRIBED_KEEP:
                del self._transcribed[next(iter(self._transcribed))]

    @staticmethod
    async def _write_transcript(env: ExecEnv, path: str, state: TaskState,
                                known: tuple[int, int, int] | None
                                ) -> tuple[int, int, int] | None:
        """Append to (or write) the transcript at ``path``; what it now
        holds, or ``None`` when it could not be written."""
        count = len(state.messages)
        last = state.message_seqs[-1] if state.message_seqs else 0

        def lines(start: int) -> bytes:
            return b"".join(msgspec.json.encode({"type": m.role, "message": m}) + b"\n"
                            for m in state.messages[start:])

        try:
            if known is not None and known[0] <= count and (
                    known[0] == 0 or (len(state.message_seqs) >= known[0]
                                      and state.message_seqs[known[0] - 1] == known[1])):
                st = await env.stat(path)
                if st is not None and st.size == known[2]:
                    data = lines(known[0])
                    out = await env.run(["sh", "-c", 'cat >> "$1"', "sh", path],
                                        stdin=data, timeout=30)
                    if out.exit_code == 0:
                        return count, last, known[2] + len(data)
            data = lines(0)
            await env.write_bytes(path, data)
            return count, last, len(data)
        except OSError:
            log.debug("transcript %s not written", path, exc_info=True)
            return None

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
                kept = await close_worktree(view, tree)
            except Exception:
                log.exception("closing worktree %s failed", tree.get("path"))
                kept = texts.WORKTREE_KEPT.format(path=tree["path"], branch=tree["branch"])
            if kept:
                result = f"{result}\n{kept}"
        launched = agent.get("at")
        duration = int(max(0.0, (at - launched) * 1000)) if at and launched else 0
        text = task_notification(agent_id=agent_id, call_id=agent.get("call_id", ""),
                                 status=status, summary=summary, result=result, ended=ended,
                                 duration_ms=duration, output_file=agent.get("output_file") or "")
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


def _replied(messages: Sequence[Message]) -> bool:
    return any(m.role == "assistant" for m in messages)


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
