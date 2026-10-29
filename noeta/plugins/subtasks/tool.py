"""The delegation tools: ``Agent`` (start a background agent),
``SubagentHandback`` (a sub-agent's final report), ``SendMessage`` and
``ListAgents``.

``Agent`` spawns its child in the background and answers at once; the
child's report arrives later as a message (``SubagentHandback``) and its end
as a completion notice (see ``Subtasks.on_event``). A finished agent is
continued by a new child task that starts from its conversation
(``TaskCreated.history_from``) under the same agent id.
"""

from __future__ import annotations

import posixpath
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from noeta.core.errors import UnknownTask
from noeta.core.events import External, Say
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ToolUse, user
from noeta.plugins.subtasks import idle, texts
from noeta.plugins.subtasks.worktree import (
    make_agent_worktree,
    remove_unlaunched_worktree,
    worktree_exists,
)
from noeta.toolkit.tools import SCHEMA_DIALECT, load_description

if TYPE_CHECKING:
    from noeta.core.agent import Agent
    from noeta.core.loop.state import TaskState
    from noeta.plugins.subtasks.extension import Subtasks
    from noeta.plugins.subtasks.roster import SubagentSpec

#: Child config keys this extension sets.
DEPTH_KEY = "subtask_depth"
TYPE_KEY = "subagent_type"
#: The id the caller knows the agent by (a resumed agent runs in a new task).
AGENT_ID_KEY = "agent_id"
#: Whether the agent delivers its report with ``SubagentHandback`` (Claude
#: Code offers it in auto mode, to an agent that has the tool; elsewhere the
#: agent's last reply is the result).
HANDBACK_KEY = "subagent_handback"
FORK_KEY = "fork"
#: An agent definition's default isolation, carried in its roster config.
ISOLATION_KEY = "isolation"
#: Set by hosts on a task no human watches: a call that needs one ends it.
UNATTENDED_KEY = "unattended"
#: The directory a background agent keeps its transcript in (the agent
#: writes ``<dir>/<agent id>.output``; see ``transcript_file``).
TRANSCRIPT_KEY = "transcript_dir"
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
DELEGATION_TOOLS = (texts.AGENT_TOOL, texts.LEGACY_AGENT_TOOL)


def _schema(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"$schema": SCHEMA_DIALECT, "type": "object",
                           "properties": properties}
    if required:
        out["required"] = list(required)
    out["additionalProperties"] = False
    return out


def agent_schema() -> dict[str, Any]:
    """Claude Code's ``Agent`` parameters: the agent types are announced in
    the conversation, so ``subagent_type`` is a free string."""
    return _schema({
        "description": {"description": texts.DESCRIPTION_PROP, "type": "string"},
        "prompt": {"description": texts.PROMPT_PROP, "type": "string"},
        "subagent_type": {"description": texts.SUBAGENT_TYPE_PROP, "type": "string"},
        "model": {"description": texts.MODEL_PROP, "type": "string",
                  "enum": list(texts.MODEL_ALIASES)},
        "effort": {"description": texts.EFFORT_PROP, "type": "string",
                   "enum": list(texts.EFFORTS)},
        "isolation": {"description": texts.ISOLATION_PROP, "type": "string",
                      "enum": list(texts.ISOLATIONS)},
    }, ["description", "prompt"])


def transcript_dir(spill_dir: str | None, root_id: str) -> str | None:
    """Where the agents of the task tree ``root_id`` keep their transcripts:
    a ``tasks`` directory beside the tree's spilled tool outputs."""
    if not spill_dir:
        return None
    return posixpath.join(spill_dir, _UNSAFE_NAME.sub("_", root_id) or "task", "tasks")


def transcript_file(directory: str, agent_id: str) -> str:
    """An agent's transcript: one JSON line per message of its conversation."""
    return posixpath.join(directory, f"{_UNSAFE_NAME.sub('_', agent_id) or 'agent'}.output")


def agent_type(args: Mapping[str, Any], roster: Mapping[str, Any] | None = None) -> str:
    """The agent type a call names. A name that is not a type exactly but
    matches one type (or ``fork``) once case, spaces, dashes and underscores
    are ignored means that type, as in Claude Code."""
    value = args.get("subagent_type")
    name = value if isinstance(value, str) and value.strip() else texts.DEFAULT_TYPE
    roster = roster or {}
    if name in roster or name == texts.FORK_TYPE:
        return name
    key = _loose(name)
    if key == _loose(texts.FORK_TYPE):
        return texts.FORK_TYPE
    matches = [k for k in roster if _loose(k) == key]
    return matches[0] if len(matches) == 1 else name


def _loose(name: str) -> str:
    return re.sub(r"[\s\-_\u2010-\u2015]+", "", unicodedata.normalize("NFKC", name).lower())


def argument_refusal(args: Mapping[str, Any]) -> str | None:
    """Values a type check lets through but that would start the wrong thing."""
    prompt = args.get("prompt")
    if isinstance(prompt, str) and not prompt.strip():
        return texts.EMPTY_PROMPT
    for name, choices in (("model", texts.MODEL_ALIASES), ("effort", texts.EFFORTS),
                          ("isolation", texts.ISOLATIONS)):
        value = args.get(name)
        if value is not None and value not in choices:
            return texts.BAD_CHOICE.format(field=name, choices=", ".join(choices), value=value)
    return None


def call_refusal(ext: Subtasks, state: TaskState, call: ToolUse,
                 roster: Mapping[str, SubagentSpec]) -> str | None:
    """Why this one Agent call may not start its agent; the reply's other
    calls are not affected (Claude Code answers each call by itself)."""
    args = call.input
    if not isinstance(args.get("prompt"), str) or not isinstance(args.get("description", ""), str):
        return texts.MISSING_FIELDS
    refusal = argument_refusal(args)
    if refusal is not None:
        return refusal
    name = agent_type(args, roster)
    reason = None
    if name == texts.FORK_TYPE:
        if state.config.get(FORK_KEY):
            reason = texts.FORK_IN_FORK
    elif name not in roster:
        available = ", ".join(sorted([*roster, texts.FORK_TYPE]))
        alike = sorted(k for k in roster if _loose(k) == _loose(name))
        reason = texts.AMBIGUOUS_AGENT.format(name=name, matches=" or ".join(alike)) \
            if alike else texts.UNKNOWN_AGENT.format(name=name, available=available)
    if reason is None and args.get("isolation") == "remote":
        reason = texts.NO_REMOTE
    depth = int(state.config.get(DEPTH_KEY) or 0)
    if reason is None and ext.max_depth is not None and depth >= ext.max_depth:
        reason = texts.DEPTH_REACHED.format(depth=depth, n=ext.max_depth)
    return texts.SPAWN_DENIED.format(reason=reason) if reason is not None else None


def batch_refusal(ext: Subtasks, state: TaskState, calls: Sequence[ToolUse]
                  ) -> tuple[str, bool] | None:
    """Limits on this reply's Agent calls taken together: ``(text,
    every_call)``. With ``every_call`` the text answers every call of the
    reply (nothing ran); otherwise only the Agent calls (the ordinary calls
    still run)."""
    agents = [u for u in calls if u.name in DELEGATION_TOOLS]
    if not agents:
        return None
    for u in calls:
        if u.name in ext.exclusive_with:
            return texts.EXCLUSIVE.format(name=u.name), True
    n = len(agents)
    if n > ext.max_fanout:
        return texts.FANOUT_TOO_BIG.format(n=n, limit=ext.max_fanout), False
    if len({u.id for u in agents}) != n:
        return texts.DUPLICATE_CALL_ID, False
    running = len(ext.running(state))
    if running + n > ext.max_background:
        return texts.TOO_MANY_BACKGROUND.format(limit=ext.max_background), False
    return None


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.split("\n"))


def agent_message(sender: str, body: str, *, frame: str = texts.AGENT_MESSAGE_FRAME) -> Message:
    """An agent's message as its receiver gets it: a user-role text that
    frames itself (origin ``agent``); the body indented so no line of it can
    pass for the frame."""
    text = (f"{texts.AGENT_MESSAGE_LEAD}\n<agent-message from=\"{sender}\">\n{frame}\n"
            f"{_indent(body)}\n</agent-message>\n\n{texts.AGENT_MESSAGE_CLOSE}")
    return user(text, origin="agent")


def handback_sender(message: Message) -> str | None:
    """The agent id of a hand-back message this extension framed, else ``None``."""
    if message.origin != "agent":
        return None
    lines = message.text.split("\n", 3)
    if len(lines) < 3 or lines[0] != texts.AGENT_MESSAGE_LEAD \
            or lines[2] != texts.AGENT_MESSAGE_FRAME:
        return None
    head = lines[1]
    prefix = '<agent-message from="'
    if not head.startswith(prefix) or not head.endswith('">'):
        return None
    return head[len(prefix):-2]


class AgentTool:
    """``Agent``: start one background agent of the roster (or a fork)."""

    name = texts.AGENT_TOOL
    description = load_description("noeta.plugins.subtasks", "agent")
    read_only = False
    clearable = False
    #: Spawns after the reply's ordinary calls have run.
    run_last = True

    def __init__(self, ext: Subtasks, state: TaskState,
                 roster: Mapping[str, SubagentSpec], name: str = texts.AGENT_TOOL,
                 *, handback: bool = True, agents: Mapping[str, Agent] | None = None) -> None:
        self._handback = handback
        self._agents = agents or {}
        self._ext = ext
        self._state = state
        self._roster = roster
        self.name = name
        self.input_schema = agent_schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ext, state = self._ext, self._state
        use = ToolUse(id=ctx.call_id, name=self.name, input=input)
        batch = batch_refusal(ext, state, [c.use for c in state.calls] or [use])
        refusal = call_refusal(ext, state, use, self._roster) or (batch[0] if batch else None)
        if refusal is not None:  # normally denied at the gate already
            return text_output(refusal, is_error=True)
        assert ctx.spawn is not None
        kind = agent_type(input, self._roster)
        prompt = str(input["prompt"])
        description = str(input.get("description") or "")
        fork = kind == texts.FORK_TYPE
        agent, queue, isolation, config = self._plan(input, ctx, kind)
        config[HANDBACK_KEY] = self._handback and self._can_hand_back(agent)
        transcripts = transcript_dir(ctx.spill_dir, ctx.root_id or state.task_id)
        if transcripts:
            config[TRANSCRIPT_KEY] = transcripts
        worktree = None
        if isolation == "worktree":
            worktree = await make_agent_worktree(ctx, config)
            if isinstance(worktree, str):
                return text_output(worktree, is_error=True)
            config["cwd"] = worktree["path"]
        child = ctx.spawn(agent, prompt, config=config, background=True, queue=queue,
                          history_from=state.task_id if fork else None,
                          history_through=state.last_seq if fork else None)
        output_file = transcript_file(transcripts, child) if transcripts else ""
        ctx.record(ext.name, "launched", {
            "task_id": child, "agent_id": child, "type": kind, "agent": agent,
            "description": description, "call_id": ctx.call_id, "config": config,
            "queue": queue, "at": time.time(), "worktree": worktree,
            "output_file": output_file})
        text = texts.LAUNCHED.format(id=child) + (texts.LAUNCHED_FORK if fork else "")
        if output_file:
            text += texts.LAUNCHED_OUTPUT_FILE.format(path=output_file)
        # A one-block list, as Claude Code sends an Agent result.
        return ToolOutput(content=text_output(text).content, meta={"child_id": child},
                          block_list=True)

    def _can_hand_back(self, agent: str) -> bool:
        """Whether ``agent``, as a sub-agent, is offered ``SubagentHandback``:
        it carries this plugin and its tool list keeps the tool. One that is
        not reports with its final message, as in Claude Code. An agent this
        worker does not know is assumed to."""
        spec = self._agents.get(agent)
        if spec is None:
            return True
        name = texts.HANDBACK_TOOL
        if (spec.allowed_tools is not None and name not in spec.allowed_tools) \
                or name in spec.denied_tools:
            return False
        return any(e.name == self._ext.name for e in spec.extensions)

    def _plan(self, input: Mapping[str, Any], ctx: ToolContext, kind: str
              ) -> tuple[str, str | None, Any, dict[str, Any]]:
        """``(agent, queue, isolation, child config)`` for a call naming ``kind``."""
        ext, state = self._ext, self._state
        fork = kind == texts.FORK_TYPE
        if fork:
            agent, spec_config, queue = state.agent, {FORK_KEY: True}, ext.queue
        else:
            spec = self._roster[kind]
            agent, spec_config = spec.agent or spec.name, dict(spec.config)
            queue = spec.queue or ext.queue
        isolation = input.get("isolation") or spec_config.pop(ISOLATION_KEY, None)
        config = ext.child_config(state, ctx, agent, spec_config, kind, input, fork=fork)
        return agent, queue, isolation, config

    async def recover(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput | None:
        """A call a crash cut short launched no agent (the launch is written
        with the call's result); only a worktree it made is left, and it goes."""
        kind = agent_type(input, self._roster)
        if kind != texts.FORK_TYPE and kind not in self._roster:
            return None
        _, _, isolation, config = self._plan(input, ctx, kind)
        if isolation == "worktree":
            await remove_unlaunched_worktree(ctx, config)
        return None


class HandbackTool:
    """``SubagentHandback``: deliver the final report and end the run."""

    name = texts.HANDBACK_TOOL
    description = load_description("noeta.plugins.subtasks", "subagent_handback")
    read_only = False
    clearable = False
    #: Last in the sub-agent's tool list, after the deferred-tool placeholder.
    listed_last = True
    input_schema = _schema({"message": {"description": texts.HANDBACK_MESSAGE_PROP,
                                        "type": "string"}}, ["message"])

    def __init__(self, state: TaskState) -> None:
        self._state = state

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        report = input.get("message")
        if not isinstance(report, str):
            return text_output("'message' must be a string: your complete report.",
                               is_error=True)
        parent = self._state.parent_id
        sender = str(self._state.config.get(AGENT_ID_KEY) or self._state.task_id)
        if parent and ctx.post is not None:
            await ctx.post(Say(message=agent_message(sender, report), when="now"),
                           task_id=parent)
        return ToolOutput(content=text_output(texts.HANDBACK_DONE).content, end_turn=report)


class SendMessageTool:
    """``SendMessage``: to a launched agent (resuming a finished one), to the
    caller, or to another task of the store."""

    name = texts.SEND_TOOL
    description = load_description("noeta.plugins.subtasks", "send_message")
    read_only = False
    clearable = False
    #: Claude Code lists it among the tools loaded on demand.
    deferred = True
    input_schema = _schema({
        "to": {"description": texts.SEND_TO_PROP, "type": "string", "maxLength": 300},
        "message": {"description": texts.SEND_MESSAGE_PROP, "type": "string"},
        "summary": {"description": texts.SEND_SUMMARY_PROP, "type": "string",
                    "maxLength": 200},
        "notify_when_idle": {"description": texts.SEND_NOTIFY_PROP, "type": "boolean"},
    }, ["to"])

    def __init__(self, ext: Subtasks, state: TaskState) -> None:
        self._ext = ext
        self._state = state

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ext, state = self._ext, self._state
        to = str(input.get("to") or "").strip()
        notify = input.get("notify_when_idle") is True
        body = input.get("message", "" if notify else None)
        if not isinstance(body, str) or not (body.strip() or notify):
            return text_output(texts.EMPTY_MESSAGE, is_error=True)
        assert ctx.post is not None
        if to == "*":
            return text_output(texts.NO_BROADCAST, is_error=True)
        me = str(state.config.get(AGENT_ID_KEY) or state.task_id)
        if notify:
            refusal = await self._idle_refusal(to, me, ctx)
            if refusal is not None:
                return text_output(refusal + (texts.IDLE_NOT_SENT if body.strip() else ""),
                                   is_error=True)
            lines = []
            if body.strip():
                await ctx.post(Say(message=user(texts.OTHER_TASK_MESSAGE.format(
                    sender=me, body=body), origin="agent"), when="next_turn"), task_id=to)
                lines.append(texts.SENT.format(to=to))
            self._subscribe(to, ctx)
            await ctx.post(External(name=idle.SUBSCRIBE_SIGNAL,
                                    data={"from": state.task_id, "id": self._sub_id(ctx)}),
                           task_id=to, dedupe_key=f"subtasks:idle-sub:{self._sub_id(ctx)}")
            return text_output("\n".join([*lines, texts.IDLE_SUBSCRIBED.format(to=to)]))
        if to == texts.MAIN_ADDRESS:
            if not state.parent_id:
                return text_output(texts.NO_CALLER, is_error=True)
            await ctx.post(Say(message=agent_message(me, body, frame=texts.AGENT_NOTE_FRAME),
                               when="now"), task_id=state.parent_id)
            return text_output(texts.SENT.format(to=to))
        agent = ext.agents_of(state).get(to)
        if agent is not None:
            task = agent["task_id"]
            message = user(texts.CALLER_MESSAGE.format(sender=me, body=body), origin="agent")
            if state.children.get(task) is None:
                await ctx.post(Say(message=message, when="now"), task_id=task)
                return text_output(texts.SENT.format(to=to))
            assert ctx.spawn is not None
            config = {**agent["config"], AGENT_ID_KEY: to}
            tree = agent.get("worktree")
            if tree and not await worktree_exists(ctx.env, tree):
                # Removed when it ended unchanged: resume where the caller works.
                tree = None
                config.pop("cwd", None)
                if "cwd" in state.config:
                    config["cwd"] = state.config["cwd"]
            child = ctx.spawn(agent["agent"], message, config=config, background=True,
                              queue=agent.get("queue") or ext.queue, history_from=task)
            ctx.record(ext.name, "launched", {
                **agent, "task_id": child, "agent_id": to, "config": config, "at": time.time(),
                "worktree": tree})
            return text_output(texts.RESUMED.format(to=to), child_id=child)
        if not to or to in (state.task_id, me):
            return text_output(texts.UNKNOWN_RECIPIENT.format(to=to), is_error=True)
        message = user(texts.OTHER_TASK_MESSAGE.format(sender=me, body=body), origin="agent")
        try:
            await ctx.post(Say(message=message, when="next_turn"), task_id=to)
        except UnknownTask:
            return text_output(texts.UNKNOWN_RECIPIENT.format(to=to), is_error=True)
        return text_output(texts.SENT.format(to=to))

    @staticmethod
    def _sub_id(ctx: ToolContext) -> str:
        return f"{ctx.task_id}:{ctx.call_id}"

    async def _idle_refusal(self, to: str, me: str, ctx: ToolContext) -> str | None:
        """Why ``notify_when_idle`` cannot subscribe to ``to``, or ``None``."""
        state = self._state
        if state.parent_id:
            return texts.IDLE_MAIN_ONLY
        if to == texts.MAIN_ADDRESS or to in self._ext.agents_of(state):
            return texts.IDLE_ROOT_TASKS_ONLY
        if to in (state.task_id, me, state.root_id):
            return texts.IDLE_SELF.format(to=to)
        row = await ctx.task(to) if ctx.task is not None else None
        if row is None:
            return texts.UNKNOWN_RECIPIENT.format(to=to)
        if row.parent_id is not None:
            return texts.IDLE_ROOT_TASKS_ONLY
        if row.status == "ended":
            return texts.IDLE_GONE.format(to=to)
        waits = self._ext.idle_waits(state)
        if to not in waits and len(waits) >= idle.MAX_SUBSCRIPTIONS:
            return texts.IDLE_CAP.format(n=idle.MAX_SUBSCRIPTIONS)
        return None

    def _subscribe(self, to: str, ctx: ToolContext) -> None:
        """Record the wait and its expiry; a new subscription to the same
        session replaces the one before."""
        earlier = self._ext.idle_waits(self._state).get(to)
        if earlier is not None:
            ctx.clear_timer(idle.timer_name(earlier["id"]))
        sub_id = self._sub_id(ctx)
        now = time.time()
        ctx.record(self._ext.name, "idle_subscribed", {"target": to, "id": sub_id, "at": now})
        ctx.set_timer(idle.timer_name(sub_id), now + idle.EXPIRY_SECONDS)


class ListAgentsTool:
    """``ListAgents``: the agents this task started, and the store's other
    open root tasks."""

    name = texts.LIST_TOOL
    description = load_description("noeta.plugins.subtasks", "list_agents")
    read_only = True
    retry_safe = True
    clearable = True
    input_schema = _schema({
        "channel": {"description": texts.LIST_FILTER_PROP, "type": "string", "maxLength": 256},
        "q": {"description": texts.LIST_FILTER_PROP, "type": "string", "maxLength": 256},
    })

    def __init__(self, ext: Subtasks, state: TaskState) -> None:
        self._ext = ext
        self._state = state

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        state = self._state
        rows = []
        for agent_id, a in self._ext.agents_of(state).items():
            ended = state.children.get(a["task_id"])
            status = "running" if ended is None else {
                "done": "finished", "failed": "failed", "cancelled": "stopped"}[ended.status]
            label = f'"{a["description"]}"' if a.get("description") else a["type"]
            rows.append(f"- {agent_id} — sub-agent ({a['type']}) {label}, {status}")
        if ctx.tasks is not None:
            root = state.root_id or state.task_id
            for row in await ctx.tasks(limit=200):
                if row.parent_id is None and row.task_id != root and row.status != "ended":
                    state_word = "idle" if row.waiting_on == "input" else "busy"
                    rows.append(f"- {row.task_id} — session (agent {row.agent}), {state_word}")
        if not rows:
            return text_output(texts.LIST_EMPTY)
        return text_output("\n".join([texts.LIST_HEAD, *rows]))
