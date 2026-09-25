"""``Task`` — the delegation control tool: its schema, its member
parsing, and its translation into a spawn Decision.

The reserved ``SPAWN_SUBAGENT_TOOL`` name stays kernel-side because the subtask
drain matches recorded calls on it, and ``concurrent_fanout_enabled`` stays
there because the react orchestration's ``parallel()`` reads the same switch;
everything model-facing lives here. Reached only through the plugin loader's
``ref`` resolution; nothing imports it statically.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    SPAWN_SUBAGENT_TOOL,
    ControlTranslateContext,
    ack_patch_decision,
    concurrent_fanout_enabled,
    enum_roster_prop,
)
from noeta.protocols.decisions import (
    Decision,
    SpawnSubtaskDecision,
    SpawnSubtaskSpec,
    SpawnSubtasksDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolUseBlock,
)
from noeta.protocols.resources import load_markdown


__all__ = [
    "spawn_subagent_tool_schema",
    "translate_spawn_subagent",
    "build_delegation_control_tool",
]


_SPAWN_SUBAGENT_DESCRIPTION = load_markdown(__package__, "spawn_subagent")
#: Property-level prose long enough to iterate on like documentation lives in
#: the same ``.md`` resource package, under ``<tool>_<property>.md``.
_SPAWN_SUBAGENT_BACKGROUND_DESCRIPTION = load_markdown(
    __package__, "spawn_subagent_background"
)


def spawn_subagent_tool_schema(
    agent_directory: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """The provider-visible schema for :data:`SPAWN_SUBAGENT_TOOL`.

    A control schema, never an Engine tool: it lands in the composer's
    ``control_action_schemas`` (hence in ``View.provider_tool_schemas`` and the
    stable hash) only when delegation is enabled. A non-empty
    ``agent_directory`` — sorted ``(name, description)`` pairs — annotates each
    spawn entry's ``agent`` property with the allowed-name ``enum`` and appends
    the human-readable roster to its description.

    The parameters follow the reference agent's shape: ONE sub-agent per
    call — ``{description, prompt, subagent_type}`` — and parallelism is
    several ``Task`` tool_use blocks in one assistant turn (the translation
    flattens the turn's calls into one fan-out). ``description`` is a short
    UI label and does not reach the child; ``prompt`` is the child's whole
    goal, so it must be self-contained.
    """
    agent_prop = enum_roster_prop(
        "The type of specialized agent to use for this task.", agent_directory
    )
    return {
        "type": "function",
        "function": {
            "name": SPAWN_SUBAGENT_TOOL,
            "description": _SPAWN_SUBAGENT_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "A short (3-5 word) description of the task."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The task for the agent to perform.",
                    },
                    "subagent_type": agent_prop,
                    # A single Task with background=true does NOT block this
                    # turn: the parent gets a "started" receipt and keeps going,
                    # and the sub-agent's result arrives as a notice when it
                    # finishes (docs/adr/background-subagent.md).
                    "background": {
                        "type": "boolean",
                        "description": _SPAWN_SUBAGENT_BACKGROUND_DESCRIPTION,
                    },
                },
                "required": ["description", "prompt", "subagent_type"],
            },
        },
    }


def _spawn_call_members(args: dict[str, Any]) -> list[tuple[str, str]] | None:
    """The ``(agent, goal)`` members of ONE ``Task`` call.

    The advertised shape is a single ``{description, prompt, subagent_type}``
    per call; the legacy ``{agent, goal}`` top-level pair (the shape the
    workflow orchestration fabricates) is still accepted. Returns ``None``
    when neither field pair is usable, so the caller acks a recoverable error
    instead of letting empty agent names fail the task later at the
    permission guard.
    """
    if "subagent_type" in args or "prompt" in args:
        agent = args.get("subagent_type")
        prompt = args.get("prompt")
        if not isinstance(agent, str) or not isinstance(prompt, str):
            return None
        return [(agent, prompt)]
    if "agent" in args or "goal" in args:
        return [(str(args.get("agent", "")), str(args.get("goal", "")))]
    return None


def _spawn_argument_refusal(args: dict[str, Any]) -> str | None:
    """Why a ``Task`` call's values are unusable, or ``None``.

    The shape check (:func:`_spawn_call_members`) accepts any string; these
    are the values a type check lets through but that would spawn the wrong
    thing: a ``background`` that is not a JSON boolean (``"false"`` is truthy,
    so it would run in the background) and an empty ``prompt``.
    """
    background = args.get("background")
    if background is not None and not isinstance(background, bool):
        return (
            "'background' must be true or false (a JSON boolean), got "
            f"{background!r}. Re-issue the call with a boolean or without it."
        )
    prompt = args.get("prompt")
    if isinstance(prompt, str) and not prompt.strip():
        return (
            "'prompt' is empty. Re-issue the call with the task the agent "
            "should perform."
        )
    return None


def _maybe_spawn_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Translate ``spawn_subagent`` tool_use(s) into a spawn decision, or fail
    closed on a mixed batch.

    Routing turns on the flattened member total across the turn's Task calls:

    * mixed with any non-Task tool call, or a malformed argument shape →
      a recoverable error ack (``patch=None``, one failed ``ToolResultBlock``
      per call_id). The task is NOT terminated; the model can retry.
    * exactly one member → :class:`SpawnSubtaskDecision`.
    * two or more members → :class:`SpawnSubtasksDecision`, with member order
      = call order then entry order within a call, and members of one call
      sharing its ``call_id`` numbered by ``member_index`` (which is what the
      resume pairing counts on).

    The ``Task`` tool is never invoked through the ToolRuntime.
    """
    tool_uses = [
        b for b in response.content if isinstance(b, ToolUseBlock)
    ]
    spawn_blocks = [
        b for b in tool_uses if b.tool_name == SPAWN_SUBAGENT_TOOL
    ]
    if not spawn_blocks:
        return None
    if len(spawn_blocks) != len(tool_uses):
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            # Stamped on every tool_use of the response, so it must read
            # correctly on a neighbour that never ran too.
            text=(
                "Nothing in this response ran: a response carrying Task may "
                "carry only Task calls. Re-issue the other calls in a "
                "separate response."
            ),
            valid=False,
        )
    members_per_call: list[tuple[ToolUseBlock, list[tuple[str, str]]]] = []
    for block in spawn_blocks:
        refusal = _spawn_argument_refusal(dict(block.arguments))
        if refusal is not None:
            return ack_patch_decision(
                tool_uses,
                assistant_message,
                assistant_thinking,
                patch=None,
                text=f"Nothing in this response ran: {refusal}",
                valid=False,
            )
        members = _spawn_call_members(dict(block.arguments))
        if members is None:
            return ack_patch_decision(
                tool_uses,
                assistant_message,
                assistant_thinking,
                patch=None,
                text=(
                    "Nothing in this response ran: every Task call needs "
                    "string 'subagent_type' and 'prompt' arguments. Re-issue "
                    "them with both set."
                ),
                valid=False,
            )
        members_per_call.append((block, members))
    if sum(len(m) for _, m in members_per_call) == 1:
        block, members = members_per_call[0]
        agent_name, goal = members[0]
        return SpawnSubtaskDecision(
            agent_name=agent_name,
            goal=goal,
            assistant_message=assistant_message,
            assistant_thinking=assistant_thinking,
            # A lone spawn with background=True does not suspend the parent on a
            # barrier: the Engine launches it on the background-subagent driver
            # and the parent keeps its turn. Only this single-spawn path reads
            # the flag — the fan-out below stays foreground and ignores it.
            background=dict(block.arguments).get("background") is True,
        )
    # Members of one batch call share its call_id, contiguously, numbered
    # 0..k-1: the resume pairing expands each assistant tool_use by its member
    # count and the Engine renders one aggregated tool_result per call.
    specs = tuple(
        SpawnSubtaskSpec(
            agent_name=agent_name,
            goal=goal,
            call_id=block.call_id,
            member_index=index,
        )
        for block, members in members_per_call
        for index, (agent_name, goal) in enumerate(members)
    )
    return SpawnSubtasksDecision(
        specs=specs,
        assistant_message=assistant_message,
        assistant_thinking=assistant_thinking,
        # A one-turn fan-out of two or more members IS an explicit "run these in
        # parallel" intent, so it drives wall-clock concurrently by default —
        # same escape valve as the workflow ``parallel()``.
        concurrent=concurrent_fanout_enabled(),
    )


def translate_spawn_subagent(ctx: ControlTranslateContext) -> Optional[Decision]:
    """The ``Task`` routing seam the mount binds into a spec."""
    return _maybe_spawn_decision(
        ctx.response,
        ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
    )


def build_delegation_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``control_tool`` contribution factory (manifest ``ref`` target).

    Self-gates on the effective ``delegation`` capability flag and renders
    ``ctx.subtask_agent_directory`` into the schema's ``agent`` enum and roster.
    Routing band 300 and schema band 100 are the byte-order contract the
    control-tool schema goldens pin — do not renumber.
    """
    if not ctx.flag("delegation"):
        return None
    return ControlToolMount(
        name=SPAWN_SUBAGENT_TOOL,
        schema=spawn_subagent_tool_schema(ctx.subtask_agent_directory),
        translate=translate_spawn_subagent,
        routing_priority=300,
        schema_priority=100,
    )
