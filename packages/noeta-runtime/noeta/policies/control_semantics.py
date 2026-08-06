"""The neutral control-tool mechanism: shared primitives, the translate
dispatcher, and the reserved recorded-wire names.

Control tools themselves are plugin material, so this module carries zero
schema, description, or translate body — the kernel only ever sees the neutral
Decisions a translate returns, and it never assembles the provider schemas (the
Composer owns ``control_action_schemas``; this seam translates *responses*
only). It imports ``noeta.protocols`` alone, which is what lets built-ins depend
on it without a cycle and keeps the kernel free of any ``noeta.builtins`` edge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import (
    Decision,
    StatePatchDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolResultBlock,
)
from noeta.protocols.view import View


# Every name here is imported back by built-in control tools across the wheel
# boundary, so it is the kernel's public contract to the built-ins band.
__all__ = [
    "validate_required_string",
    "enum_roster_prop",
    "ack_patch_decision",
    "ControlTranslateContext",
    "ControlToolSpec",
    "translate_control_tool",
    "SPAWN_SUBAGENT_TOOL",
    "RUN_WORKFLOW_TOOL",
    "WORKFLOW_AGENT_NAME",
    "concurrent_fanout_enabled",
]


# ===========================================================================
# Shared control-tool primitives
# ===========================================================================


def validate_required_string(
    value: Any, name: str, max_len: int
) -> tuple[bool, Optional[str]]:
    if not isinstance(value, str) or not value:
        return False, f"{name} must be a non-empty string"
    if len(value) > max_len:
        return False, f"{name} too long (max {max_len})"
    return True, value


def enum_roster_prop(base_description: str, items) -> dict[str, Any]:
    """Build a string property with an enum constraint and a roster description; with empty ``items`` it is just the bare description."""
    prop: dict[str, Any] = {"type": "string", "description": base_description}
    if items:
        prop["enum"] = [name for name, _ in items]
        roster = "; ".join(
            f"{name} — {desc}" if desc else name for name, desc in items
        )
        prop["description"] = base_description + " Available: " + roster
    return prop


def ack_patch_decision(
    tool_uses,
    assistant_message,
    assistant_thinking,
    *,
    patch,
    text: str,
    valid: bool,
) -> StatePatchDecision:
    """Shared ack builder for control tools: one ToolResultBlock per tool_use, wrapped in a StatePatchDecision."""
    ack = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id=b.call_id,
                output=text,
                success=valid,
                error=None if valid else text,
            )
            for b in tool_uses
        ],
    )
    return StatePatchDecision(
        messages_before=(assistant_message,),
        patch=patch,
        messages_after=(ack,),
        assistant_thinking=assistant_thinking,
    )


# ===========================================================================
# The translation seam — mechanism the dispatcher consumes
# ===========================================================================


@dataclass(frozen=True)
class ControlTranslateContext:
    """The per-turn inputs a control tool's translate step reads.

    Assembled once by :func:`translate_control_tool`, including the out-of-band
    ``ThinkingBlock`` s that must be threaded into every control Decision so a
    reasoning-model turn keeps its signature. The context carries NO
    feature-named field — a translate needing build-time state captures it in
    its closure — so the mechanism stays neutral.
    """

    response: LLMResponse
    assistant_message: Message
    assistant_thinking: tuple[ThinkingBlock, ...]
    content_store: Optional[ContentStore]
    #: Every mounted control tool's model-visible name. Lets a translate that
    #: hands residual calls to the ToolRuntime (TodoWrite's mixed batch) refuse
    #: a co-occurring CONTROL call, which the runtime could never answer.
    control_tool_names: frozenset[str] = frozenset()
    #: The composed View the turn was decided against — the same folded-state
    #: projection the Policy already holds, threaded through so a translate can
    #: answer from folded state (RecallHistory renders the collapsed prefix off
    #: ``view.rolling_history[:view.summary_boundary]``). ``None`` when the
    #: caller predates the field; a translate that needs it must degrade to a
    #: recoverable ack, not raise.
    view: Optional[View] = None


@dataclass(frozen=True)
class ControlToolSpec:
    """One control tool's decision-time routing entry: name + translate step.

    A spec's position in the dispatcher's ORDERED tuple IS its routing priority
    when several control tools co-occur in one turn. Mounting a spec IS
    enablement — there is no ``enabled`` predicate, because a disabled tool
    contributes no mount and therefore no spec. The type lives in
    ``noeta.policies`` so the mount loop in ``noeta.execution`` can build it
    without ``policies`` importing ``execution``.
    """

    #: Model-visible control-tool name (readability / debug; not routing input).
    name: str
    #: Translate the turn into this tool's neutral Decision, or ``None`` when the
    #: turn carries no call to it (the dispatcher then tries the next spec).
    translate: Callable[["ControlTranslateContext"], Optional[Decision]]


def translate_control_tool(
    response: LLMResponse,
    assistant_message: Message,
    *,
    specs: Sequence[ControlToolSpec],
    content_store: Optional[ContentStore] = None,
    view: Optional[View] = None,
) -> Decision | None:
    """Translate a control-tool ``tool_use`` turn into a neutral Decision.

    Returns the first spec whose ``translate`` yields a Decision, or ``None``
    when the turn carries no mounted control tool (the caller then falls through
    to the normal ``tool_calls`` path). Spec order is the mount loop's
    ``routing_priority`` sort, and it decides the winner when several control
    tools co-occur in one turn.
    """
    ctx = ControlTranslateContext(
        response=response,
        assistant_message=assistant_message,
        # Extracted once so every Decision a spec builds carries the same
        # thinking tuple — a reasoning-model turn must keep its signature.
        assistant_thinking=tuple(
            b for b in response.content if isinstance(b, ThinkingBlock)
        ),
        content_store=content_store,
        control_tool_names=frozenset(spec.name for spec in specs),
        view=view,
    )
    for spec in specs:
        decision = spec.translate(ctx)
        if decision is not None:
            return decision
    return None


# ===========================================================================
# Reserved recorded-wire vocabulary
#
# These three constants are the kernel's reserved control-tool NAMES: the schema
# + translate that USE them live in their built-ins, but the drain
# (``execution.subtask_drain``) and resolver (``execution.resolver``) are
# mechanism that must route on the recorded tool name — a byte written on the
# EventLog — so the NAME cannot live in a plugin (the kernel would then depend on
# ``noeta.builtins``). Same residue class as the ``POLICY_REF ("react", "1")``
# pin: the kernel names the default wire vocabulary it cannot avoid touching.
# ``concurrent_fanout_enabled`` stays here for the same reason — its two readers
# live in DIFFERENT plugins (delegation + react), so the shared neutral switch is
# the kernel's, not either plugin's.
# ===========================================================================

#: The model-visible **control** tool name a coding parent calls to delegate to a
#: named sub-agent. It is NOT an executable workspace tool: the ``delegation``
#: built-in's translation seam turns a single
#: ``ToolUseBlock(tool_name=SPAWN_SUBAGENT_TOOL)`` into a ``SpawnSubtaskDecision``
#: and the ToolRuntime never invokes it. The name stays kernel-side because
#: ``execution.subtask_drain`` routes on it: the drain is mechanism that must
#: match the recorded tool name.
SPAWN_SUBAGENT_TOOL = "Task"

#: Model-visible **control** tool name: launch a model-authored orchestration
#: script that fans agents out as real subtasks. The ``react`` built-in's
#: ``control_tool`` translate turns it into a ``SpawnSubtaskDecision`` whose child
#: carries the orchestration interpreter Policy — same family / plumbing as
#: ``spawn_subagent``. The NAME stays kernel-side because
#: ``execution.subtask_drain`` routes on it.
RUN_WORKFLOW_TOOL = "run_workflow"

#: Reserved ``agent_name`` carried by the ``run_workflow`` → ``SpawnSubtaskDecision``
#: translation. The host's child-engine builder routes a child with this name to
#: the react built-in's ``OrchestrationPolicy`` (reading script/args from the
#: child's ``TaskCreated.inputs``) instead of resolving a roster agent. It belongs
#: to the PermissionGuard ``allowed_subtask_agents`` allow-list (so the
#: orchestration spawn passes) but NOT to the model-facing ``spawn_subagent``
#: directory — the model reaches a workflow only through ``run_workflow``. Stays
#: kernel-side because ``execution.resolver`` routes on it, and naming it here
#: lets the translation seam reference it without importing ``orchestration``
#: (which would cycle through ``react``).
WORKFLOW_AGENT_NAME = "__workflow__"


#: fan-out master switch — **default ON**.
#: Both the ``spawn_subagent`` fan-out (in the ``delegation`` built-in's
#: ``_maybe_spawn_decision``) and the workflow ``parallel()``
#: (``orchestration.parallel``) read this one judgment, so a single env var is
#: the escape valve: set ``NOETA_SUBTASK_CONCURRENCY`` to
#: ``0``/``false``/``off``/``no`` to force a sequential drain. Unset (or
#: anything unrecognized) ⇒ concurrent.
#:
#: Stays kernel-side (not in either plugin) precisely because the two consumers
#: live in DIFFERENT plugins (delegation + react); the kernel owns the shared
#: neutral switch. Both import it back across the wheel boundary — which is why
#: :func:`concurrent_fanout_enabled` is a public name: the kernel owes the
#: built-ins band a contract, not an underscore.
_SUBTASK_CONCURRENCY_ENV = "NOETA_SUBTASK_CONCURRENCY"


def concurrent_fanout_enabled() -> bool:
    """True unless the escape valve forces a sequential group drain.

    Default ON: an unset (or unrecognized) ``NOETA_SUBTASK_CONCURRENCY`` means
    concurrent; only an explicit ``0``/``false``/``off``/``no`` (case-insensitive,
    whitespace-trimmed) forces a sequential drain. A sequential group's
    ``SubtaskGroupCompleted`` carries no ``concurrent`` field (conditionally
    folded in the Engine), so it stays byte-identical to a sequential recording.
    """
    return os.environ.get(_SUBTASK_CONCURRENCY_ENV, "").strip().lower() not in {
        "0", "false", "off", "no",
    }
