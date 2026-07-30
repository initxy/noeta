"""``delegation`` — the ``spawn_subagent`` control tool, as a built-in plugin.

Control-tool-surface S2: ``spawn_subagent``'s schema, its member-parsing +
translate body, its ``translate_spawn_subagent`` seam, and its ``.md``
descriptions moved out of the kernel's control band into this built-in. The move
is byte-preserving (the S0 golden pins the schema bytes, including the roster
enum + property prose).

What stays kernel-side (and this impl imports back) is the vocabulary the
mechanism routes on: the reserved ``SPAWN_SUBAGENT_TOOL`` name (the subtask
drain matches it on the recorded call, D8) and the shared
``concurrent_fanout_enabled`` fan-out switch (read by BOTH this translate and the
react orchestration ``parallel()``, so it stays a neutral kernel helper). Plus
the neutral schema/ack helpers ``enum_roster_prop`` / ``ack_patch_decision`` and
the decision-time ``ControlTranslateContext``.

Reached only through the plugin loader's ``ref`` resolution; nothing imports it
statically.
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
#: Property-level prose long enough to iterate like documentation lives in
#: the same ``.md`` resource package, under ``<tool>_<property>.md``.
_SPAWN_SUBAGENT_SPAWNS_DESCRIPTION = load_markdown(
    __package__, "spawn_subagent_spawns"
)
_SPAWN_SUBAGENT_BACKGROUND_DESCRIPTION = load_markdown(
    __package__, "spawn_subagent_background"
)


def spawn_subagent_tool_schema(
    agent_directory: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """The provider-visible schema for :data:`SPAWN_SUBAGENT_TOOL`.

    Added to ``ThreeSegmentComposer.control_action_schemas`` (so it lands in
    ``View.provider_tool_schemas`` + the stable hash) only when delegation is
    enabled — never registered as an Engine tool. Carries a function-level
    ``description`` (externalized to this built-in's ``spawn_subagent.md``).

    A non-empty ``agent_directory`` — a sorted tuple of ``(name, description)``
    pairs — annotates each spawn entry's ``agent`` property with an ``enum``
    (the list of allowed names, in order) and appends the human-readable roster
    to its description.

    The parameters advertise the **batch form**: a required ``spawns`` array of
    ``{agent, goal}`` entries. One entry = the classic single delegate-and-wait;
    several entries = a one-call concurrent fan-out. The array IS the schema
    because a single call carrying N entries is the only shape gpt-5.x models
    reliably batch — the same models essentially never emit two spawn tool
    calls in one turn, no matter what the description or the user demands
    (probed live, 17/17 single). The translate seam still accepts the legacy
    top-level ``{agent, goal}`` single form (old recordings replay byte-equal,
    and the workflow orchestration fabricates that form).
    """
    agent_prop = enum_roster_prop(
        "Named sub-agent to delegate to.", agent_directory
    )
    return {
        "type": "function",
        "function": {
            "name": SPAWN_SUBAGENT_TOOL,
            "description": _SPAWN_SUBAGENT_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "spawns": {
                        "type": "array",
                        "minItems": 1,
                        "description": _SPAWN_SUBAGENT_SPAWNS_DESCRIPTION,
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": agent_prop,
                                "goal": {
                                    "type": "string",
                                    "description": (
                                        "The focused goal for this sub-agent."
                                    ),
                                },
                            },
                            "required": ["agent", "goal"],
                        },
                    },
                    # background sub-agent (docs/adr/background-subagent.md): a
                    # single spawn with background=true does NOT block this turn —
                    # the parent gets a "started" receipt and keeps going, and the
                    # sub-agent's result is delivered as a notice when it finishes.
                    "background": {
                        "type": "boolean",
                        "description": _SPAWN_SUBAGENT_BACKGROUND_DESCRIPTION,
                    },
                },
                "required": ["spawns"],
            },
        },
    }


def _spawn_call_members(args: dict[str, Any]) -> list[tuple[str, str]] | None:
    """The ``(agent, goal)`` members of ONE ``spawn_subagent`` call.

    Batch form: the ``spawns`` array (each entry ``{agent, goal}``) — the
    shape the advertised schema carries. Legacy single form: top-level
    ``{agent, goal}`` when ``spawns`` is absent — every pre-batch recording
    replays through this branch byte-equal, and the workflow orchestration
    fabricates it. Returns ``None`` when ``spawns`` is present but malformed
    (not a non-empty array of ``{agent, goal}`` objects) — the caller acks a
    recoverable error instead of letting empty names fail the task at the
    permission guard.
    """
    if "spawns" not in args:
        return [(str(args.get("agent", "")), str(args.get("goal", "")))]
    raw = args.get("spawns")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    members: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict) or "agent" not in entry or "goal" not in entry:
            return None
        members.append((str(entry.get("agent", "")), str(entry.get("goal", ""))))
    return members


def _maybe_spawn_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Issue C / SR2: translate `spawn_subagent` tool_use(s) into a
    spawn decision, or fail closed on a mixed batch.

    Returns ``None`` when no `spawn_subagent` is present (normal
    tool-call path). Each call expands to its member list via
    :func:`_spawn_call_members` (the batch ``spawns`` array, or the legacy
    single ``{agent, goal}`` form). Routing on the flattened member total:

    * `spawn_subagent` **mixed with any non-spawn** tool call →
      recoverable error ack (``StatePatchDecision`` with ``patch=None``,
      one ``ToolResultBlock(success=False)`` per call_id). The task is
      NOT terminated — the model can retry in a later turn. This matches
      the sibling control tools' sole-call philosophy (D4).
    * a malformed ``spawns`` argument → the same recoverable error ack.
    * exactly **one** member in total →
      `SpawnSubtaskDecision` (SR1 single-child path, unchanged —
      ``spawns`` with one entry behaves exactly like the legacy form).
    * **≥2** members (one call carrying an array, several calls, or a mix)
      → `SpawnSubtasksDecision` (SR2 fan-out; member order = call order,
      then entry order within a call; members of one call share its
      ``call_id`` and are numbered by ``member_index``). The
      `spawn_subagent` tool is never invoked through the ToolRuntime.
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
        # at least one non-spawn tool_use is present → mixed batch.
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="spawn_subagent cannot be mixed with other tool calls in the same turn",
            valid=False,
        )
    members_per_call: list[tuple[ToolUseBlock, list[tuple[str, str]]]] = []
    for block in spawn_blocks:
        members = _spawn_call_members(dict(block.arguments))
        if members is None:
            return ack_patch_decision(
                tool_uses,
                assistant_message,
                assistant_thinking,
                patch=None,
                text=(
                    "spawn_subagent: 'spawns' must be a non-empty array of "
                    "{agent, goal} objects"
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
            # docs/adr/background-subagent.md: a lone spawn with background=True
            # does NOT suspend the parent on a barrier — the Engine launches it on
            # the background-subagent driver and the parent keeps its turn. Only
            # the single-spawn path reads it; the fan-out below stays foreground
            # (background is documented as single-entry-only and ignored there).
            background=bool(dict(block.arguments).get("background", False)),
        )
    # SR2: ≥2 members, all-spawn turn → fan-out batch. Members of one batch
    # call share its call_id, contiguously, numbered 0..k-1 — the resume
    # pairing expands each assistant tool_use by its member count and the
    # Engine renders one aggregated tool_result per call.
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
        # a one-turn fan-out of ≥2 spawn members IS an explicit "run these
        # in parallel" intent, so it drives wall-clock concurrently by default
        # (same escape valve as the workflow ``parallel()``). The Engine folds
        # this transient opt-in onto the persisted ``SubtaskGroupCompleted``
        # (``concurrent or None``), so a forced-sequential group stays
        # byte-identical to a pre-v2 recording.
        concurrent=concurrent_fanout_enabled(),
    )


def translate_spawn_subagent(ctx: ControlTranslateContext) -> Optional[Decision]:
    """The ``spawn_subagent`` routing seam the mount binds into a spec."""
    return _maybe_spawn_decision(
        ctx.response,
        ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
    )


def build_delegation_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``control_tool`` contribution factory (manifest ``ref`` target).

    Self-gates on the effective ``delegation`` capability flag and reproduces the
    pre-migration internal ``_spawn_subagent_mount`` exactly: it renders the
    spawn directory (``ctx.subtask_agent_directory``) into the schema's ``agent``
    enum + roster, routing band 300, schema band 100 (the S0 golden byte order).
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
