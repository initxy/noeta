"""The ``Task`` tool: its schema, argument checks, and the spawn itself.

A foreground call spawns its child and parks on it; the runner resumes the
call with the child's end. Several Task calls in one reply each park, so the
children run concurrently and the results land in the reply's (spawn) order.
A lone call with ``background=true`` spawns and answers at once; the child's
end later arrives as a notice (see ``Subtasks.on_event``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from noeta.events import ChildEnded
from noeta.ext.subtasks import texts
from noeta.runner import texts as core_texts
from noeta.tools.base import Park, ToolContext, ToolOutput, text_output
from noeta.types import ToolUse

if TYPE_CHECKING:
    from noeta.ext.subtasks.extension import SubagentSpec, Subtasks
    from noeta.loop.state import TaskState

#: Child config keys this extension sets.
DEPTH_KEY = "subtask_depth"
TYPE_KEY = "subagent_type"
#: Set on a background child and inherited by everything below it: no human
#: watches this task, so a call that needs one ends it instead of waiting.
UNATTENDED_KEY = "unattended"


def task_schema(roster: Mapping[str, SubagentSpec]) -> dict[str, Any]:
    """0.6's ``spawn_subagent_tool_schema`` parameters: the roster (sorted by
    name) becomes the ``subagent_type`` enum and is listed in its
    description."""
    items = sorted((s.name, s.description) for s in roster.values())
    agent_prop: dict[str, Any] = {"type": "string", "description": texts.SUBAGENT_TYPE_PROP}
    if items:
        agent_prop["enum"] = [name for name, _ in items]
        listing = "; ".join(f"{name} — {desc}" if desc else name for name, desc in items)
        agent_prop["description"] = texts.SUBAGENT_TYPE_PROP + " Available: " + listing
    return {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": texts.DESCRIPTION_PROP},
            "prompt": {"type": "string", "description": texts.PROMPT_PROP},
            "subagent_type": agent_prop,
            "background": {
                "type": "boolean",
                "description": texts.load_description("spawn_subagent_background"),
            },
        },
        "required": ["description", "prompt", "subagent_type"],
    }


def call_member(args: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(subagent_type, prompt)`` of one call; the legacy ``{agent, goal}``
    shape is still accepted. ``None`` when neither pair is usable."""
    if "subagent_type" in args or "prompt" in args:
        agent, prompt = args.get("subagent_type"), args.get("prompt")
        if not isinstance(agent, str) or not isinstance(prompt, str):
            return None
        return agent, prompt
    if "agent" in args or "goal" in args:
        return str(args.get("agent", "")), str(args.get("goal", ""))
    return None


def argument_refusal(args: Mapping[str, Any]) -> str | None:
    """Values a type check lets through but that would spawn the wrong
    thing: a non-boolean ``background`` (``"false"`` is truthy) and an empty
    ``prompt``."""
    background = args.get("background")
    if background is not None and not isinstance(background, bool):
        return texts.BAD_BACKGROUND.format(value=background)
    prompt = args.get("prompt")
    if isinstance(prompt, str) and not prompt.strip():
        return texts.EMPTY_PROMPT
    return None


def batch_refusal(
    ext: Subtasks, state: TaskState, calls: Sequence[ToolUse]
) -> tuple[str, bool] | None:
    """Why this reply's Task calls may not run: ``(text, every_call)``. With
    ``every_call`` the text answers every call of the reply (nothing ran);
    otherwise only the Task calls (the ordinary calls still run)."""
    tasks = [u for u in calls if u.name == texts.TASK_TOOL]
    if not tasks:
        return None
    for u in calls:
        if u.name in ext.exclusive_with:
            return texts.EXCLUSIVE.format(name=u.name), True
    for u in tasks:
        refusal = argument_refusal(u.input)
        if refusal is not None:
            return texts.NOTHING_RAN.format(reason=refusal), True
        if call_member(u.input) is None:
            return texts.MISSING_FIELDS, True
    n = len(tasks)
    if n > ext.max_fanout:
        return texts.FANOUT_TOO_BIG.format(n=n, limit=ext.max_fanout), False
    if len({u.id for u in tasks}) != n:
        return texts.DUPLICATE_CALL_ID, False
    depth = int(state.config.get(DEPTH_KEY) or 0)
    for u in tasks:
        member = call_member(u.input)
        assert member is not None
        name = member[0]
        if name not in ext.roster:
            available = ", ".join(sorted(ext.roster))
            reason = texts.UNKNOWN_AGENT.format(name=name, available=available or "none")
            return texts.SPAWN_DENIED.format(reason=reason), False
        if ext.max_depth is not None and depth >= ext.max_depth:
            reason = texts.DEPTH_REACHED.format(n=ext.max_depth)
            return texts.SPAWN_DENIED.format(reason=reason), False
    return None


def render_child_result(ended: ChildEnded) -> ToolOutput:
    """A child's end as the Task call's result: its answer, or its error
    leading whatever partial answer it had (0.6's adapter rendering)."""
    if ended.status == "done":
        return text_output(ended.answer)
    error = ended.error or texts.SUBAGENT_FAILED
    return text_output(f"{error}\n{ended.answer}" if ended.answer else error, is_error=True)


class TaskTool:
    """``Task``: spawn one sub-agent from the roster and return its result."""

    name = texts.TASK_TOOL
    description = texts.load_description("spawn_subagent")
    read_only = False
    clearable = False
    #: Spawns after the reply's ordinary calls have run (0.6's order).
    run_last = True

    def __init__(self, ext: Subtasks, state: TaskState) -> None:
        self._ext = ext
        #: The live task state (the reply's other calls, depth, children).
        self._state = state
        self.input_schema = task_schema(ext.roster)

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ext, state = self._ext, self._state
        refusal = batch_refusal(ext, state, [c.use for c in state.calls] or [
            _use(ctx.call_id, input)])
        if refusal is not None:  # normally denied at the gate already
            return text_output(refusal[0], is_error=True)
        member = call_member(input)
        assert member is not None and ctx.spawn is not None
        name, prompt = member
        spec = ext.roster[name]
        tasks = [c for c in state.calls if c.use.name == texts.TASK_TOOL]
        background = input.get("background") is True and len(tasks) <= 1
        if background:
            running = ext.running_background(state)
            if len(running) >= ext.max_background:
                reason = texts.TOO_MANY_BACKGROUND.format(running=len(running),
                                                          limit=ext.max_background)
                return text_output(texts.BACKGROUND_NOT_STARTED.format(reason=reason),
                                   is_error=True)
        config = ext.child_config(state, spec, background=background)
        child = ctx.spawn(spec.agent or spec.name, prompt, config=config,
                          background=background, queue=spec.queue or ext.queue)
        if background:
            ctx.record(ext.name, "background",
                       {"child_id": child, "agent": spec.name, "call_id": ctx.call_id})
            return text_output(texts.BACKGROUND_STARTED.format(agent=spec.name, id=child),
                               child_id=child)
        detail = {"subagent_type": spec.name}
        if isinstance(input.get("description"), str):
            detail["description"] = input["description"]
        return ToolOutput(park=Park(on="children", keys=[child], detail=detail))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        if outcome is None:
            return text_output(core_texts.STOPPED_CALL_ERROR, is_error=True)
        [ended] = outcome.values()
        out = render_child_result(ended)
        return ToolOutput(content=out.content, is_error=out.is_error,
                          meta={"child_id": ended.child_id, "status": ended.status})


def _use(call_id: str, input: dict[str, Any]) -> ToolUse:
    return ToolUse(id=call_id, name=texts.TASK_TOOL, input=input)
