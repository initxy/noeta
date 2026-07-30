"""The neutral control-tool MECHANISM — the kernel's whole remaining share.

A coding agent's model-visible **control** tools (``skill`` / ``run_workflow`` /
``structured_output`` / ``todo_write`` / ``ask_user_question`` /
``spawn_subagent``) are SDK product **material** (mechanism-vs-material; demoted
from the kernel). Control-tool-surface S2/S2b finished emptying every schema,
description, and translate body out of this module into the built-ins that own
them (``noeta.builtins.{todo_write,ask_user_question,delegation}`` for the first
three; ``skills`` for ``skill``; ``react`` for ``run_workflow`` /
``structured_output`` — the last two rode ``workflow_sandbox`` out with them).
The move is byte-preserving (the S0 golden pins every schema byte).

What STAYS here is the neutral mechanism the built-ins build on and the kernel
seams route on — zero schemas, zero descriptions, zero product translate bodies:

* the shared neutral primitives every control tool reuses
  (:func:`validate_required_string` / :func:`enum_roster_prop` /
  :func:`ack_patch_decision`);
* the decision-time :class:`ControlTranslateContext` + the routing
  :class:`ControlToolSpec` + the :func:`translate_control_tool` dispatcher (the
  mechanism the mount loop feeds and the policy iterates);
* the reserved recorded-wire NAMES the drain / resolver route on
  (:data:`SPAWN_SUBAGENT_TOOL` / :data:`RUN_WORKFLOW_TOOL` /
  :data:`WORKFLOW_AGENT_NAME`, D8) and the shared fan-out switch
  :func:`concurrent_fanout_enabled` (read by two different plugins).

The kernel sees only neutral Decisions: a
:class:`~noeta.protocols.decisions.StatePatchDecision` applies caller-built
messages + a typed patch, a
:class:`~noeta.protocols.decisions.YieldForHumanDecision` carries an opaque
:class:`~noeta.protocols.decisions.HitlRequestAnchor`, a
:class:`~noeta.protocols.decisions.SpawnSubtaskDecision` delegates. The
translation seam does NOT participate in stable-prefix schema assembly (the
Composer owns ``control_action_schemas``); it only translates *responses*.

Layering: imports only ``noeta.protocols.*`` — no cross-band edge, no
``ReActPolicy`` / built-ins import (so ``react`` / ``skills`` may depend on this
module without a cycle, and the kernel keeps zero ``noeta.builtins`` edge).
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


__all__ = [
    # shared NEUTRAL control-tool primitives — the migrated built-in control
    # tools import these back across the wheel boundary, so they are the
    # kernel's public contract to the built-ins band (a contract, not an
    # underscore).
    "validate_required_string",
    "enum_roster_prop",
    "ack_patch_decision",
    # translation seam — the mechanism the dispatcher consumes.
    "ControlTranslateContext",
    "ControlToolSpec",
    "translate_control_tool",
    # reserved recorded-wire vocabulary — the tool NAMES the drain / resolver
    # route on (D8), plus the shared fan-out switch two plugins read.
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
    """Build a string property with an enum constraint and a roster description; with empty ``items`` it is just the bare description.

    A NEUTRAL schema helper shared across control tools that render a roster
    (``skill`` in the ``skills`` built-in, ``spawn_subagent`` in the
    ``delegation`` built-in), so it stays kernel-side and is public — the
    built-ins import it back.
    """
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
    """Shared ack builder for control tools: one ToolResultBlock per tool_use, wrapped in a StatePatchDecision.

    A NEUTRAL builder shared across control tools that ack + optionally patch
    (todo_write / ask_user_question / skill / spawn_subagent / run_workflow), so
    it stays kernel-side and is public — the migrated built-ins import it back.
    """
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

    Assembled once by :func:`translate_control_tool`: the LLM ``response``, the
    Policy's assistant :class:`Message`, and the out-of-band ``ThinkingBlock`` s
    extracted once (threaded into every control Decision so a reasoning-model
    turn keeps its signature), plus ``content_store`` for ``ask_user_question``'s
    spilled question body. This context carries NO feature-named field: a
    translate that needs build-time state (the ``skill`` menu) captures it in its
    closure (see the ``skills`` built-in's ``make_skill_translate``), so the
    mechanism stays neutral. A spec ignores the fields it does not need.
    """

    response: LLMResponse
    assistant_message: Message
    assistant_thinking: tuple[ThinkingBlock, ...]
    content_store: Optional[ContentStore]


@dataclass(frozen=True)
class ControlToolSpec:
    """One control tool's decision-time routing entry: name + translate step.

    The dispatcher :func:`translate_control_tool` consumes an ORDERED tuple of
    these (the builder mounts them in routing-priority order); a spec's position
    IS its routing priority when several control tools co-occur in one turn.
    Mounting a spec IS enablement — there is no ``enabled`` predicate, because a
    disabled tool contributes no mount and therefore no spec. This is the type
    the dispatcher consumes (D9): it lives in ``noeta.policies`` so the mount
    loop (``noeta.execution``) can build it without ``policies`` importing
    ``execution``.
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
) -> Decision | None:
    """Translate a control-tool ``tool_use`` turn into a neutral Decision.

    Walks the caller-supplied ``specs`` in order (the builder mounts them in
    routing-priority order) and returns the first spec whose ``translate`` yields
    a Decision; ``None`` when no mounted control tool is present (the caller then
    falls through to the normal ``tool_calls`` path). Order matters when several
    control tools co-occur in one turn. Mounting IS enablement: a disabled tool
    contributes no spec, so there is no per-spec ``enabled`` gate here — the
    routing order (ask → todo → spawn → skill → workflow) is the mount loop's
    ``routing_priority`` sort, byte-identical to the original nested ``_maybe_*``
    dispatch.

    Extended-thinking end-to-end (Slice B): the LLM's ThinkingBlocks are extracted
    ONCE from ``response.content`` here and threaded (via
    :class:`ControlTranslateContext`) into every control Decision the specs
    build, matching the parallel ``ToolCallsDecision`` path in ``react.py`` so a
    reasoning-model turn that emits thinking + a control tool_use still carries its
    signature.
    """
    ctx = ControlTranslateContext(
        response=response,
        assistant_message=assistant_message,
        # Extract out-of-band thinking once so every spec reuses the same tuple
        # (non-reasoning models → empty tuple, no-op, byte-safe).
        assistant_thinking=tuple(
            b for b in response.content if isinstance(b, ThinkingBlock)
        ),
        content_store=content_store,
    )
    for spec in specs:
        decision = spec.translate(ctx)
        if decision is not None:
            return decision
    return None


# ===========================================================================
# Reserved recorded-wire vocabulary (D8)
#
# These three constants are the kernel's reserved control-tool NAMES: the schema
# + translate that USE them all moved into their built-ins, but the drain
# (``execution.subtask_drain``) and resolver (``execution.resolver``) are
# mechanism that must route on the recorded tool name — a byte written on the
# EventLog long before this migration — so the NAME cannot live in a plugin (the
# kernel would then depend on ``noeta.builtins``). Same acknowledged residue class
# as the ``POLICY_REF ("react", "1")`` pin: the kernel names the default wire
# vocabulary it cannot avoid touching. ``concurrent_fanout_enabled`` stays here
# for the same reason — its two readers live in DIFFERENT plugins (delegation +
# react), so the shared neutral switch is the kernel's, not either plugin's.
# ===========================================================================

#: Phase 4.5 Issue C — the model-visible **control** tool name a coding
#: parent calls to delegate to a named sub-agent. It is NOT an executable
#: workspace tool: the ``delegation`` built-in's translation seam turns a single
#: ``ToolUseBlock(tool_name=SPAWN_SUBAGENT_TOOL)`` into a
#: ``SpawnSubtaskDecision`` and the ToolRuntime never invokes it. The name stays
#: kernel-side because ``execution.subtask_drain`` routes on it (D8): the drain
#: is mechanism that must match the recorded tool name.
SPAWN_SUBAGENT_TOOL = "spawn_subagent"

#: Model-visible **control** tool name: launch a model-authored orchestration
#: script that fans agents out as real subtasks. The ``react`` built-in's
#: ``control_tool`` translate turns it into a ``SpawnSubtaskDecision`` whose child
#: carries the orchestration interpreter Policy — same family / plumbing as
#: ``spawn_subagent``. The NAME stays kernel-side because
#: ``execution.subtask_drain`` routes on it (D8).
RUN_WORKFLOW_TOOL = "run_workflow"

#: Reserved ``agent_name`` carried by the ``run_workflow`` → ``SpawnSubtaskDecision``
#: translation. The host's child-engine builder routes a child with this name to
#: the react built-in's ``OrchestrationPolicy`` (reading script/args from the
#: child's ``TaskCreated.inputs``) instead of resolving a roster agent. It belongs
#: to the PermissionGuard ``allowed_subtask_agents`` allow-list (so the
#: orchestration spawn passes) but NOT to the model-facing ``spawn_subagent``
#: directory — the model reaches a workflow only through ``run_workflow``. Stays
#: kernel-side because ``execution.resolver`` routes on it (D8), and naming it
#: here lets the translation seam reference it without importing ``orchestration``
#: (which would cycle through ``react``).
WORKFLOW_AGENT_NAME = "__workflow__"


#: fan-out v2 master switch — now **default ON**.
#: Both the ``spawn_subagent`` fan-out (in the ``delegation`` built-in's
#: ``_maybe_spawn_decision``) and the workflow ``parallel()``
#: (``orchestration.parallel``) read this one judgment, so a single env var is
#: the escape valve: set ``NOETA_SUBTASK_CONCURRENCY`` to
#: ``0``/``false``/``off``/``no`` to force the legacy sequential drain. Unset (or
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
    whitespace-trimmed) forces the legacy sequential drain. A sequential group's
    ``SubtaskGroupCompleted`` carries no ``concurrent`` field (conditionally
    folded in the Engine), so it stays byte-identical to a pre-v2 recording.
    """
    return os.environ.get(_SUBTASK_CONCURRENCY_ENV, "").strip().lower() not in {
        "0", "false", "off", "no",
    }
