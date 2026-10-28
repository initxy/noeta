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

import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from noeta.core.errors import UnknownTask
from noeta.core.events import Say
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ToolUse, user
from noeta.plugins.subtasks import texts

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState
    from noeta.plugins.subtasks.extension import SubagentSpec, Subtasks

#: Child config keys this extension sets.
DEPTH_KEY = "subtask_depth"
TYPE_KEY = "subagent_type"
#: The id the caller knows the agent by (a resumed agent runs in a new task).
AGENT_ID_KEY = "agent_id"
FORK_KEY = "fork"
#: An agent definition's default isolation, carried in its roster config.
ISOLATION_KEY = "isolation"
#: Set by hosts on a task no human watches: a call that needs one ends it.
UNATTENDED_KEY = "unattended"
DELEGATION_TOOLS = (texts.AGENT_TOOL, texts.LEGACY_AGENT_TOOL)


def _schema(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"$schema": texts.JSON_SCHEMA, "type": "object",
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


def agent_type(args: Mapping[str, Any]) -> str:
    value = args.get("subagent_type")
    return value if isinstance(value, str) and value.strip() else texts.DEFAULT_TYPE


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


def batch_refusal(
    ext: Subtasks, state: TaskState, calls: Sequence[ToolUse],
    roster: Mapping[str, SubagentSpec],
) -> tuple[str, bool] | None:
    """Why this reply's Agent calls may not run: ``(text, every_call)``. With
    ``every_call`` the text answers every call of the reply (nothing ran);
    otherwise only the Agent calls (the ordinary calls still run)."""
    agents = [u for u in calls if u.name in DELEGATION_TOOLS]
    if not agents:
        return None
    for u in calls:
        if u.name in ext.exclusive_with:
            return texts.EXCLUSIVE.format(name=u.name), True
    for u in agents:
        if not isinstance(u.input.get("prompt"), str) \
                or not isinstance(u.input.get("description", ""), str):
            return texts.MISSING_FIELDS, True
        refusal = argument_refusal(u.input)
        if refusal is not None:
            return texts.NOTHING_RAN.format(reason=refusal), True
    n = len(agents)
    if n > ext.max_fanout:
        return texts.FANOUT_TOO_BIG.format(n=n, limit=ext.max_fanout), False
    if len({u.id for u in agents}) != n:
        return texts.DUPLICATE_CALL_ID, False
    depth = int(state.config.get(DEPTH_KEY) or 0)
    for u in agents:
        name = agent_type(u.input)
        reason = None
        if name == texts.FORK_TYPE:
            if state.config.get(FORK_KEY):
                reason = texts.FORK_IN_FORK
        elif name not in roster:
            available = ", ".join(sorted([*roster, texts.FORK_TYPE]))
            reason = texts.UNKNOWN_AGENT.format(name=name, available=available)
        if reason is None and u.input.get("isolation") == "remote":
            reason = texts.NO_REMOTE
        if reason is None and ext.max_depth is not None and depth >= ext.max_depth:
            reason = texts.DEPTH_REACHED.format(n=ext.max_depth)
        if reason is not None:
            return texts.SPAWN_DENIED.format(reason=reason), False
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
    description = texts.load_description("agent")
    read_only = False
    clearable = False
    #: Spawns after the reply's ordinary calls have run.
    run_last = True

    def __init__(self, ext: Subtasks, state: TaskState,
                 roster: Mapping[str, SubagentSpec], name: str = texts.AGENT_TOOL) -> None:
        self._ext = ext
        self._state = state
        self._roster = roster
        self.name = name
        self.input_schema = agent_schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ext, state = self._ext, self._state
        refusal = batch_refusal(ext, state, [c.use for c in state.calls]
                                or [ToolUse(id=ctx.call_id, name=self.name, input=input)],
                                self._roster)
        if refusal is not None:  # normally denied at the gate already
            return text_output(refusal[0], is_error=True)
        assert ctx.spawn is not None
        kind = agent_type(input)
        prompt = str(input["prompt"])
        description = str(input.get("description") or "")
        fork = kind == texts.FORK_TYPE
        if fork:
            agent, spec_config, queue = state.agent, {FORK_KEY: True}, ext.queue
        else:
            spec = self._roster[kind]
            agent, spec_config = spec.agent or spec.name, dict(spec.config)
            queue = spec.queue or ext.queue
        isolation = input.get("isolation") or spec_config.pop(ISOLATION_KEY, None)
        config = ext.child_config(state, ctx, agent, spec_config, kind, input, fork=fork)
        worktree = None
        if isolation == "worktree":
            worktree = await ext.add_worktree(ctx, config)
            if isinstance(worktree, str):
                return text_output(worktree, is_error=True)
            config["cwd"] = worktree["path"]
        child = ctx.spawn(agent, prompt, config=config, background=True, queue=queue,
                          history_from=state.task_id if fork else None,
                          history_through=state.last_seq if fork else None)
        ctx.record(ext.name, "launched", {
            "task_id": child, "agent_id": child, "type": kind, "agent": agent,
            "description": description, "call_id": ctx.call_id, "config": config,
            "queue": queue, "at": time.time(), "worktree": worktree})
        text = texts.LAUNCHED.format(id=child) + (texts.LAUNCHED_FORK if fork else "")
        return text_output(text, child_id=child)


class HandbackTool:
    """``SubagentHandback``: deliver the final report and end the run."""

    name = texts.HANDBACK_TOOL
    description = texts.load_description("subagent_handback")
    read_only = False
    clearable = False
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
    description = texts.load_description("send_message")
    read_only = False
    clearable = False
    #: Claude Code lists it among the tools loaded on demand.
    deferred = True
    input_schema = _schema({
        "to": {"description": texts.SEND_TO_PROP, "type": "string", "maxLength": 300},
        "message": {"description": texts.SEND_MESSAGE_PROP, "type": "string"},
        "summary": {"description": texts.SEND_SUMMARY_PROP, "type": "string",
                    "maxLength": 200},
    }, ["to", "message"])

    def __init__(self, ext: Subtasks, state: TaskState) -> None:
        self._ext = ext
        self._state = state

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ext, state = self._ext, self._state
        to = str(input.get("to") or "").strip()
        body = input.get("message")
        if not isinstance(body, str) or not body.strip():
            return text_output(texts.EMPTY_MESSAGE, is_error=True)
        assert ctx.post is not None
        me = str(state.config.get(AGENT_ID_KEY) or state.task_id)
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
            child = ctx.spawn(agent["agent"], message, config=config, background=True,
                              queue=agent.get("queue") or ext.queue, history_from=task)
            ctx.record(ext.name, "launched", {
                **agent, "task_id": child, "agent_id": to, "config": config, "at": time.time()})
            return text_output(texts.RESUMED.format(to=to), child_id=child)
        if not to or to in (state.task_id, me):
            return text_output(texts.UNKNOWN_RECIPIENT.format(to=to), is_error=True)
        message = user(texts.OTHER_TASK_MESSAGE.format(sender=me, body=body), origin="agent")
        try:
            await ctx.post(Say(message=message, when="next_turn"), task_id=to)
        except UnknownTask:
            return text_output(texts.UNKNOWN_RECIPIENT.format(to=to), is_error=True)
        return text_output(texts.SENT.format(to=to))


class ListAgentsTool:
    """``ListAgents``: the agents this task started, and the store's other
    open root tasks."""

    name = texts.LIST_TOOL
    description = texts.load_description("list_agents")
    read_only = True
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
