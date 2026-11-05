"""Delegation as an extension: the ``Agent`` tool and its companions, the
whole-reply checks that refuse a bad batch before anything runs, the
sub-agent's hand-back, and the completion notices."""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import time
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
from noeta.core.runner.texts import STEP_LIMIT_HEAD
from noeta.core.tool import Tool, ToolContext
from noeta.core.types import Message, ModelRequest, ToolUse, user
from noeta.plugins.subtasks import idle, texts
from noeta.plugins.subtasks.mentions import mention_reminders
from noeta.plugins.subtasks.roster import (
    MODEL_CAP_KEY,
    Roster,
    SubagentSpec,
    capped_model,
    check_roster,
)
from noeta.plugins.subtasks.roster import agent_list as listing
from noeta.plugins.subtasks.roster import agent_list_delta as roster_delta
from noeta.plugins.subtasks.tool import (
    AGENT_ID_KEY,
    DELEGATION_TOOLS,
    DEPTH_KEY,
    HANDBACK_KEY,
    RESULT_LIMIT_CHARS,
    TRANSCRIPT_KEY,
    TYPE_KEY,
    AgentTool,
    HandbackTool,
    ListAgentsTool,
    SendMessageTool,
    batch_refusal,
    call_refusal,
    handback_flagged,
    handback_sender,
    transcript_file,
)
from noeta.plugins.subtasks.worktree import close_worktree
from noeta.toolkit import notices
from noeta.toolkit.notices import NOTICE_GUARD, STATUS_ORDER, escape, one_line
from noeta.toolkit.tools import large_output_message, mark_spill_dir, spill_path, task_spill_dir

__all__ = ["SubagentSpec", "Subtasks", "task_notification"]

log = logging.getLogger("noeta.plugins.subtasks")

#: Claude Code's progress-label cadence: a sub-agent is asked for a label
#: every 30 seconds while it runs (timed from recorded wall-clock times, so
#: any worker can pick the task up), once it has 3 messages and only when
#: its conversation changed since the last ask; the inbox signal carries
#: the answer.
PROGRESS_EVERY = 30.0
#: The wall clock the cadence reads (shared by workers, unlike a timer).
_now = time.time
PROGRESS_MIN_MESSAGES = 3
PROGRESS_SIGNAL = "subtasks.progress"
#: Tasks whose transcript progress a process remembers at most.
TRANSCRIBED_KEEP = 256
#: The permission mode in which agents hand back with ``SubagentHandback``.
AUTO_MODE = "auto"
#: Agent types whose missing report gets no hint to ask for it (Claude
#: Code's Explore and Plan).
ONE_SHOT_TYPES = frozenset({"Explore", "Plan"})
#: The agent list's place in the first request's joined system turn
#: (``Reminder.order``): after the environment and the deferred tools.
AGENT_LIST_ORDER = 40


def task_notification(*, agent_id: str, call_id: str, status: str, summary: str,
                      result: str, ended: ChildEnded, duration_ms: int,
                      output_file: str = "", worktree: Mapping[str, str] | None = None,
                      saved_to: str | None = None) -> str:
    """A finished agent's ``<task-notification>``, behind the not-the-user
    guard: ``<result>`` only when there is one, ``<worktree>`` when its
    worktree was kept. A result over :data:`RESULT_LIMIT_CHARS` is shown as
    a preview of the file ``saved_to`` holds it in, or without one cut to
    its start and end."""
    fields = [("task-id", agent_id), ("tool-use-id", call_id)]
    if output_file:
        fields.append(("output-file", escape(output_file)))
    body = f"\n<note>{texts.NOTICE_NOTE}</note>"
    if result:
        shown = escape(result)
        if len(result) > RESULT_LIMIT_CHARS:
            shown = large_output_message(shown, RESULT_LIMIT_CHARS,
                                         saved_to=escape(saved_to) if saved_to else None)
        body += f"\n<result>{shown}</result>"
    body += (f"\n<usage><subagent_tokens>{subagent_tokens(ended)}</subagent_tokens><tool_uses>"
             f"{ended.tool_calls}</tool_uses><duration_ms>{duration_ms}</duration_ms></usage>")
    if worktree:
        branch = worktree.get("branch") or ""
        body += (f"\n<worktree><worktreePath>{escape(worktree['path'])}</worktreePath>"
                 + (f"<worktreeBranch>{escape(branch)}</worktreeBranch>" if branch else "")
                 + "</worktree>")
    element = notices.task_notification(
        (*fields, ("status", status), ("summary", escape(summary))), body=body)
    return f"{NOTICE_GUARD}\n\n{element}"


def subagent_tokens(ended: ChildEnded) -> int:
    """Claude Code's count: the newest reply's input (cache included) plus
    what every reply wrote."""
    usage = ended.last_usage
    last_input = usage.input if usage is not None else 0
    last_output = usage.output if usage is not None else 0
    return last_input + max(ended.output_tokens, last_output)


#: The answer a step-limit stop finishes with opens with this (the kernel's
#: ``STEP_LIMIT_HEAD``), which names the limit.
_STEP_LIMIT = re.compile(re.escape(STEP_LIMIT_HEAD).replace(re.escape("{n}"), r"(\d+)"))


def step_limit(answer: str) -> int | None:
    """The step limit an agent's answer says it stopped at, else ``None``."""
    found = _STEP_LIMIT.match(answer)
    return int(found.group(1)) if found else None


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
    - ``exclusive_with``: tool names an Agent call may not share a reply with.
    - ``inherit_config``: parent ``TaskCreated.config`` keys a child copies.
      ``model`` and ``effort`` are copied only when neither the call nor the
      agent's definition sets them.
    - ``queue``: the queue children are created on (default: the caller's).
    - ``progress_labels``: every 30 seconds a running sub-agent asks the
      model, beside one of its calls, for a 3-5 word label of what it is
      doing; front ends get it as a notice, the model never sees it.
    """

    name = "subtasks"

    def __init__(
        self,
        roster: Mapping[str, SubagentSpec] | Sequence[SubagentSpec] = (),
        *,
        agents: Sequence[str] = (),
        max_depth: int | None = 3,
        max_background: int = texts.MAX_BACKGROUND,
        exclusive_with: Collection[str] = (),
        inherit_config: Sequence[str] = ("cwd", "model", "effort"),
        queue: str | None = None,
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
        self.exclusive_with = frozenset(exclusive_with)
        self.inherit_config = tuple(inherit_config)
        self.queue = queue

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"agents": {}, "tasks": {}, "handbacks": {}, "flagged": [], "pending": {},
                "noticed": [], "reminded": False, "listed": False, "held_list": None,
                "held_status": [], "progress": None, "mentions_through": 0, **idle.initial()}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = {**self.initial_state(), **(state or {})}
        if kind == "launched":
            agent_id = data["agent_id"]
            handbacks = {k: v for k, v in st["handbacks"].items() if k != agent_id}
            return {**st, "agents": {**st["agents"], agent_id: data},
                    "tasks": {**st["tasks"], data["task_id"]: agent_id}, "handbacks": handbacks}
        if kind == "handback":
            agent = data["agent_id"]
            flagged = [a for a in st["flagged"] if a != agent]
            if data.get("flagged"):
                flagged.append(agent)
            return {**st, "handbacks": {**st["handbacks"], agent: "taken"},
                    "flagged": flagged}
        if kind == "progress":
            return {**st, "progress": {**(st["progress"] or {}), **data}}
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
            types = data.get("types") if isinstance(data, dict) else None
            return {**st, "listed": True, "held_list": held, "types": types}
        if kind == "agent_delta":
            return {**st, "listed": True, "types": data["types"]}
        if kind == "agent_list_sent":
            return {**st, "held_list": None}
        if kind == "task_status":
            return {**st, "held_status": list(data.get("texts") or ())}
        if kind == "task_status_sent":
            return {**st, "held_status": []}
        if kind == "agent_mention":
            through = data.get("through") if isinstance(data, dict) else None
            if isinstance(through, int):
                return {**st, "mentions_through": max(st["mentions_through"], through)}
            return st
        return idle.reduce(st, kind, data) or st

    def continue_state(self, state: Any) -> Any:
        """A continuing task (a fork, a resumed sub-agent) starts its own
        mention count: ``mentions_through`` is a seq of the source's log,
        and the new log's prompts start below it."""
        if not isinstance(state, dict):
            return state
        return {**state, "mentions_through": 0}

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
                     *, fork: bool, parent_model: str = "") -> dict[str, Any]:
        config = {k: state.config[k] for k in self.inherit_config if k in state.config}
        config.update(spec_config)
        cap = config.pop(MODEL_CAP_KEY, None)
        if not fork:
            if isinstance(args.get("model"), str):
                config["model"] = models.resolve_alias(args["model"])
            elif isinstance(cap, str) and "model" not in spec_config:
                capped = capped_model(str(config.get("model") or parent_model), cap)
                if capped is not None:
                    config["model"] = capped
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
            out.append(AgentTool(self, state, roster, handback=_auto_mode(self, view),
                                 agents=view.agents, model=view.model))
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
        """When a sub-agent's progress label is due, ask for it beside this
        call: the same request with one more user text, answered in the
        background."""
        state = view.state
        if not (self.progress_labels and TYPE_KEY in state.config
                and view.complete is not None and view.post is not None):
            return request
        now = _now()
        progress = self._state(state)["progress"] or {}
        since = progress.get("asked") or progress.get("from")
        if since is None:
            view.record(self.name, "progress", {"from": now})
            return request
        count = len(state.messages)
        if now - float(since) < PROGRESS_EVERY or count < PROGRESS_MIN_MESSAGES \
                or count == progress.get("count"):
            return request
        view.record(self.name, "progress", {"asked": now, "count": count})
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
        out: list[Reminder] = [Reminder(text=t, kind="task_status_sent", data={}, role="system",
                                        order=STATUS_ORDER) for t in st["held_status"]]
        if _hands_back(view.state.config) and not st["reminded"]:
            out.append(Reminder(text=texts.HANDBACK_REMINDER, kind="handback_reminder"))
        if st["held_list"] and (not view.state.parent_id or _replied(view.state.messages)):
            # The first request (a sub-agent's second, as Claude Code's):
            # the agent list joins the environment's system turn, after the
            # deferred tools.
            out.append(Reminder(text=st["held_list"], kind="agent_list_sent", data={},
                                role="system", order=AGENT_LIST_ORDER))
        elif not st["held_list"]:
            out += self._agent_delta(view, st)
        if not view.state.parent_id:
            out += mention_reminders(view, self.roster_for(view), st["mentions_through"])
        return out

    def _agent_delta(self, view: TaskView, st: dict[str, Any]) -> list[Reminder]:
        """A note when agent types joined or left since the list went out
        (an agent file added or removed, a plugin reloaded): Claude Code's
        agent-list delta."""
        roster = self.roster_for(view)
        types = sorted(roster, key=str.lower)
        if not st["listed"]:
            # None to list at the start: the whole list once there is one.
            text = listing(roster)
            return [Reminder(text=text, kind="agent_delta", data={"types": types},
                             role="system", order=AGENT_LIST_ORDER)] if text else []
        if st.get("types") is None:
            # A list recorded before its types were kept: from here on.
            view.record(self.name, "agent_delta", {"types": types})
            return []
        text = roster_delta(st["types"], roster)
        if not text:
            return []
        return [Reminder(text=text, kind="agent_delta", data={"types": types},
                         role="system", order=AGENT_LIST_ORDER)]

    # ------------------------------------------------------------ events

    def _types(self, view: TaskView) -> list[str]:
        return sorted(self.roster_for(view), key=str.lower)

    def agent_list(self, view: TaskView) -> str | None:
        """The agent types this task can start, one line each with its tools."""
        return listing(self.roster_for(view))

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After a compaction the agent list goes out again with the next
        request, in the environment's system turn, and a status line for
        each agent still running (Claude Code's ``task_status``)."""
        text = None if view.state.parent_id else self.agent_list(view)
        if text and view.keeps_tail is not None:
            view.record(self.name, "agent_list", {"text": text, "types": self._types(view)})
        status = self.running_status(view) if view.keeps_tail is not None else []
        if status:
            view.record(self.name, "task_status", {"texts": status})
        return ()

    def running_status(self, view: TaskView) -> list[str]:
        """One status line per agent this task started that is still
        running."""
        agents = self._state(view.state)["agents"]
        send = bool(self.roster_for(view)) or bool(view.state.parent_id)
        out: list[str] = []
        for agent_id in self.running(view.state):
            data = agents[agent_id]
            text = texts.STILL_RUNNING.format(
                description=one_line(str(data.get("description") or "")), id=agent_id)
            if data.get("output_file"):
                text += texts.STILL_RUNNING_FILE.format(path=data["output_file"])
            if send:
                text += texts.STILL_RUNNING_SEND
            out.append(text)
        return out

    async def on_start(self, view: TaskView) -> None:
        st = self._state(view.state)
        text = self.agent_list(view)
        if text and not st["listed"]:
            # Held for the first request's system turn (``reminders``).
            view.record(self.name, "agent_list", {"text": text, "types": self._types(view)})
        # Ends taken before a crash whose notice was never recorded.
        done = set(st["noticed"]) | set(st["pending"])
        for task_id in st["tasks"]:
            ended = view.state.children.get(task_id)
            if ended is not None and task_id not in done:
                await self._ended(view, ended, at=None, handbacks=dict(st["handbacks"]),
                                  flagged=set(st["flagged"]))

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
            flags = set(st["flagged"])
            for item in ev.items:
                body = item.body
                if isinstance(body, External) and body.name == PROGRESS_SIGNAL:
                    view.notify(str((body.data or {}).get("label") or ""))
                elif isinstance(body, Say):
                    sender = handback_sender(body.message)
                    if sender is not None and sender in st["agents"]:
                        handbacks[sender] = "taken"
                        flagged = handback_flagged(body.message)
                        if flagged:
                            flags.add(sender)
                        view.record(self.name, "handback", {
                            "agent_id": sender, **({"flagged": True} if flagged else {})})
                elif isinstance(body, ChildEnded) and body.child_id in st["tasks"]:
                    await self._ended(view, body, at=item.at, handbacks=handbacks,
                                      flagged=flags)
        elif isinstance(ev, TaskEnded):
            await self._close_open_worktrees(view, st)
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
            # The tree's spill directory: swept by the host only once marked.
            await mark_spill_dir(env, posixpath.dirname(posixpath.dirname(path)),
                                 state.root_id or state.task_id)
            return count, last, len(data)
        except OSError:
            log.debug("transcript %s not written", path, exc_info=True)
            return None

    async def _close_open_worktrees(self, view: TaskView, st: Mapping[str, Any]) -> None:
        """This task ended with agents still running (they are cancelled with
        it): their ends reach no one, so their worktrees are closed now, an
        unchanged one removed."""
        for agent in st["agents"].values():
            tree = agent.get("worktree")
            if not tree or view.state.children.get(agent["task_id"]) is not None:
                continue
            try:
                kept = await close_worktree(view, tree)
            except Exception:
                log.exception("closing worktree %s failed", tree.get("path"))
                continue
            if kept:
                log.info("task %s ended: %s", view.task_id, kept)

    def _deliver(self, view: TaskView, task_id: str, text: str) -> None:
        tools_used = texts.TOOLS_BEFORE_SUMMARY.format(tool=texts.HANDBACK_TOOL)
        view.record(self.name, "tools_used", message=user(tools_used, origin="system"))
        view.record(self.name, "noticed", {"task_id": task_id},
                    message=user(text, origin="system"))

    async def _ended(self, view: TaskView, ended: ChildEnded, *, at: float | None,
                     handbacks: Mapping[str, str], flagged: Collection[str] = ()) -> None:
        """Turn an agent's end into its completion notice. After a hand-back
        the notice waits until the model has answered the report and then
        rides along with the next request; otherwise it wakes the task."""
        st = self._state(view.state)
        agent_id = st["tasks"][ended.child_id]
        agent = st["agents"][agent_id]
        status, summary, result = _outcome(agent, agent_id, ended, handbacks, flagged)
        handed_back = ended.status == "done" and agent_id in handbacks
        tree = agent.get("worktree")
        kept_tree: Mapping[str, str] | None = None
        if tree and agent["task_id"] == ended.child_id:
            try:
                kept = await close_worktree(view, tree)
            except Exception:
                log.exception("closing worktree %s failed", tree.get("path"))
                kept = "kept"
            if kept:
                kept_tree = tree
        launched = agent.get("at")
        duration = int(max(0.0, (at - launched) * 1000)) if at and launched else 0
        text = task_notification(agent_id=agent_id, call_id=agent.get("call_id", ""),
                                 status=status, summary=summary, result=result, ended=ended,
                                 duration_ms=duration, output_file=agent.get("output_file") or "",
                                 worktree=kept_tree,
                                 saved_to=await _save_result(view, ended.child_id, result))
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


async def _save_result(view: TaskView, child_id: str, result: str) -> str | None:
    """Save a result over :data:`RESULT_LIMIT_CHARS` whole in this task's
    spill directory, through its env: the path, or ``None`` when it fits or
    cannot be saved. The path is a pure function of the task and the child,
    so a notice made again names the same file."""
    if len(result) <= RESULT_LIMIT_CHARS or not view.spill_dir or view.env is None:
        return None
    path = spill_path(view.spill_dir, view.task_id, f"agent-{child_id}")
    try:
        await view.env.write_bytes(path, result.encode("utf-8"))
    except OSError:
        log.debug("result of %s not saved", child_id, exc_info=True)
        return None
    await mark_spill_dir(view.env, task_spill_dir(view.spill_dir, view.task_id), view.task_id)
    return path


def _outcome(agent: Mapping[str, Any], agent_id: str, ended: ChildEnded,
             handbacks: Mapping[str, str], flagged: Collection[str]) -> tuple[str, str, str]:
    """A finished agent's notice ``(status, summary, result)``; an empty
    result is left out of the notice."""
    description = agent.get("description") or agent.get("type") or agent_id
    if ended.status == "failed":
        error = ended.error or texts.SUBAGENT_FAILED
        return "failed", texts.SUMMARY_FAILED.format(description=description,
                                                     error=error), ended.answer
    if ended.status != "done":
        reason = ended.error
        summary = (texts.SUMMARY_STOPPED_BY_CALLER if reason == notices.STOPPED_BY_CALLER
                   else texts.SUMMARY_STOPPED_BY_USER if reason in ("", "cancelled")
                   else texts.SUMMARY_STOPPED_WHY)
        return "killed", summary.format(description=description, reason=reason), ended.answer
    limit = step_limit(ended.answer)
    summary = (texts.SUMMARY_STEP_LIMIT.format(description=description, n=limit)
               if limit is not None else texts.SUMMARY_FINISHED.format(description=description))
    if agent_id in handbacks:
        pointer = texts.RESULT_POINTER_FLAGGED if agent_id in flagged else texts.RESULT_POINTER
        return "completed", summary, pointer.format(id=agent_id)
    if not _hands_back(agent.get("config") or {}):
        # Offered no hand-back: its last reply is the report.
        return "completed", summary, ended.answer
    # Offered one and did not use it: what it wrote instead is not passed on.
    hint = "" if agent.get("type") in ONE_SHOT_TYPES else texts.RESULT_NO_REPORT_HINT
    result = texts.RESULT_NO_REPORT.format(hint=hint)
    if limit is not None:
        result = f"{texts.STEP_LIMIT_NOTE.format(n=limit)}\n{result}"
    return "completed", summary, result


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
