"""Control-tool semantics, collocated **per control tool**.

A coding agent's model-visible **control** tools — ``skill`` /
``run_workflow`` / ``structured_output`` (still here), plus ``todo_write`` /
``ask_user_question`` / ``spawn_subagent`` (migrated out — see below) — are SDK
product **material** (mechanism-vs-material; demoted from the kernel). The kernel
sees only neutral mechanism: a
:class:`~noeta.protocols.decisions.StatePatchDecision` applies caller-built
messages + a typed patch, a
:class:`~noeta.protocols.decisions.YieldForHumanDecision` carries an opaque
:class:`~noeta.protocols.decisions.HitlRequestAnchor`, a
:class:`~noeta.protocols.decisions.SpawnSubtaskDecision` delegates.

For each control tool still here, this module is the **single home** for its
whole story — its provider-visible schema, its argument validators / codec, and
its response→neutral-Decision translation seam, collocated in one per-tool
section so a maintainer reads one concept in one place (locality).

Control-tool-surface S2 began emptying this module: ``todo_write`` /
``ask_user_question`` / ``spawn_subagent`` moved schema + translate + description
into their own built-ins (``noeta.builtins.{todo_write,ask_user_question,
delegation}``) — a control tool is now a plugin contribution, not kernel
material. The move is byte-preserving (the S0 golden pins every schema byte). What
STAYS kernel-side is the neutral MECHANISM the built-ins build on: the shared
schema/ack helpers (:func:`enum_roster_prop` / :func:`ack_patch_decision` /
:func:`validate_required_string`), the decision-time
:class:`ControlTranslateContext` + :class:`ControlToolSpec` + the
:func:`translate_control_tool` dispatcher, the reserved ``spawn_subagent`` tool
name (the drain routes on it) and the shared ``concurrent_fanout_enabled``
switch. ``skill`` / ``run_workflow`` / ``structured_output`` stay here until S2b
claims them. The thin ``control_tools`` / ``_control_translate`` re-export shims
still re-export what remains here.

The translation seam does NOT participate in stable-prefix schema
assembly (the Composer owns ``control_action_schemas``); it only translates
*responses*. ``plan`` permission mode and its enter/exit_plan_mode control
tools were removed.

Layering: imports only ``noeta.protocols.*`` and the sibling
``noeta.policies.descriptions`` / ``noeta.policies.workflow_sandbox`` — no
cross-band edge, no ``ReActPolicy`` import (so ``react`` may depend on this
module without a cycle).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from noeta.policies.descriptions import load_control_tool_description
from noeta.policies.workflow_sandbox import check_workflow_script
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import (
    Decision,
    SpawnSubtaskDecision,
    StatePatchDecision,
    TaskStatePatch,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


__all__ = [
    # shared NEUTRAL control-tool primitives — the migrated built-in control
    # tools (todo_write / ask_user_question / delegation) import these back
    # across the wheel boundary, so they are the kernel's public contract to
    # the built-ins band (a contract, not an underscore).
    "validate_required_string",
    "enum_roster_prop",
    "ack_patch_decision",
    # skill (still kernel-resident; the skills built-in claims it in S2b)
    "SKILL_TOOL",
    "skill_tool_schema",
    # spawn_subagent — the reserved tool NAME stays kernel-side (D8:
    # subtask_drain routes on the recorded name); the schema + translate moved
    # into the ``delegation`` built-in. ``concurrent_fanout_enabled`` is the
    # shared fan-out switch the delegation translate AND the react orchestration
    # both read, so it stays here (used by tools in two different plugins).
    "SPAWN_SUBAGENT_TOOL",
    "concurrent_fanout_enabled",
    # run_workflow (still kernel-resident; the react built-in claims it in S2b)
    "RUN_WORKFLOW_TOOL",
    "WORKFLOW_AGENT_NAME",
    "run_workflow_tool_schema",
    # structured_output (still kernel-resident; react claims it in S2b)
    "STRUCTURED_OUTPUT_TOOL",
    "structured_output_tool_schema",
    # translation seam — mechanism the dispatcher consumes + the per-tool
    # translate adapters that stay kernel-resident (run_workflow / skill).
    "ControlTranslateContext",
    "ControlToolSpec",
    "translate_control_tool",
    "translate_run_workflow",
    "make_skill_translate",
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
    (``skill`` here, ``spawn_subagent`` in the ``delegation`` built-in), so it
    stays kernel-side and is public — the built-ins import it back.
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


@dataclass(frozen=True)
class ControlTranslateContext:
    """The per-turn inputs a control tool's translate step reads.

    Assembled once by :func:`translate_control_tool`: the LLM ``response``, the
    Policy's assistant :class:`Message`, and the out-of-band ``ThinkingBlock`` s
    extracted once (threaded into every control Decision so a reasoning-model
    turn keeps its signature), plus ``content_store`` for ``ask_user_question``'s
    spilled question body. This context carries NO feature-named field: a
    translate that needs build-time state (the ``skill`` menu) captures it in its
    closure (see :func:`make_skill_translate`), so the mechanism stays neutral.
    A spec ignores the fields it does not need.
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


# ---------------------------------------------------------------------------
# The per-tool translate seam. Each adapter reads a ``ControlTranslateContext``
# and delegates to the tool's ``_maybe_*`` helper (defined further down; Python
# resolves those names at call time, so declaring these ahead of the helpers is
# fine). The mount loop references these by name to build each
# :class:`ControlToolSpec`. ``skill`` needs its menu, so it is a closure
# FACTORY: the built closure captures the names and the context stays
# feature-neutral (D2). The ``todo_write`` / ``ask_user_question`` /
# ``spawn_subagent`` seams moved into their built-ins alongside the schema +
# translate body they wrap (control-tool-surface S2); only ``run_workflow`` and
# the ``skill`` closure factory stay kernel-resident here (they migrate in S2b).
# ---------------------------------------------------------------------------


def translate_run_workflow(ctx: ControlTranslateContext) -> Optional[Decision]:
    return _maybe_workflow_decision(
        ctx.response,
        ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
    )


def make_skill_translate(
    menu_names: frozenset[str],
) -> Callable[[ControlTranslateContext], Optional[Decision]]:
    """Build the ``skill`` translate closure over its indexed menu names (D2).

    The closure captures ``menu_names`` so :func:`_maybe_skill_decision`
    validates an ordered skill against the same set the schema's enum was built
    from — WITHOUT the neutral :class:`ControlTranslateContext` carrying a
    feature-named ``skill_menu_names`` field.
    """

    def translate(ctx: ControlTranslateContext) -> Optional[Decision]:
        return _maybe_skill_decision(
            ctx.response,
            ctx.assistant_message,
            menu_names=menu_names,
            assistant_thinking=ctx.assistant_thinking,
        )

    return translate


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
# skill (D2 / D4) — model-driven skill invocation control tool
# ===========================================================================

#: Model-visible **control** tool name for model-driven skill menu selection.
SKILL_TOOL = "skill"


_SKILL_DESCRIPTION = load_control_tool_description("skill")


def skill_tool_schema(
    menu: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """Provider-visible schema for :data:`SKILL_TOOL`.

    A **control** tool (never an Engine/ToolRuntime tool): a ``skill`` call
    activates a named skill via a ``StatePatchDecision`` (``activate_skills``
    patch), same channel pre-loop activations use. Added to the Composer's
    ``control_action_schemas`` only when ``skill_invocation_enabled`` AND
    the workspace has at least one indexed skill.

    ``menu`` is a sorted sequence of ``(name, description)`` pairs. The name
    is rendered into the ``skill`` property's ``enum``; the description is
    appended to its description as a human-readable roster, mirroring the
    ``spawn_subagent`` agent_directory pattern. A single required
    ``skill`` string parameter — no ``args``, no ``reason`` (D4).
    """
    skill_prop = enum_roster_prop("Name of the skill to activate.", menu)
    return {
        "type": "function",
        "function": {
            "name": SKILL_TOOL,
            "description": _SKILL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": skill_prop,
                },
                "required": ["skill"],
            },
        },
    }


# Max length for a skill name string (generous upper bound; the real gate is
# the menu enum, but this catches obviously-malformed payloads before we
# format the error roster).
_SKILL_NAME_MAX_LEN = 200


def _maybe_skill_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    menu_names: frozenset[str],
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """D1/D4: translate a `skill` control-tool call into a neutral
    :class:`StatePatchDecision`, or ``None`` when no `skill` is present.

    Rules mirror the sibling control tools: the `skill` call must be
    the **sole** tool call in the turn (mixed with any other tool →
    recoverable error ack, no state write). The ``skill`` argument is
    validated against the sorted menu set: a known name becomes a
    ``StatePatchDecision(activate_skills=[name])`` whose ack confirms the
    skill is loaded and will appear from the next turn; an unknown name
    becomes an error ack listing the available names so the model can retry.

    Duplicate activation (the name is already in ``active_skills``) is NOT
    special-cased here: the same success ack is returned and the state
    merge de-duplicates (``TaskStatePatch.apply`` unions ``activate_skills``
    with ``state.active_skills`` order-preserving). The kernel emits
    ``messages_before`` → ``TaskStatePatched`` → ``messages_after``, same
    sequence as ``todo_write``.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    skill_blocks = [b for b in tool_uses if b.tool_name == SKILL_TOOL]
    if not skill_blocks:
        return None

    # Sole-call rule — exactly one `skill` block and nothing else.
    if len(skill_blocks) != len(tool_uses) or len(skill_blocks) != 1:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="skill must be the only tool call in the turn",
            valid=False,
        )

    block = skill_blocks[0]
    args = dict(block.arguments)
    ok, name_or_err = validate_required_string(
        args.get("skill"), "skill", _SKILL_NAME_MAX_LEN
    )
    if not ok:
        assert isinstance(name_or_err, str)
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=name_or_err,
            valid=False,
        )
    name = name_or_err
    assert isinstance(name, str)
    if name not in menu_names:
        available = ", ".join(sorted(menu_names)) if menu_names else "(none)"
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=f"unknown skill {name!r}; available: {available}",
            valid=False,
        )
    return ack_patch_decision(
        tool_uses,
        assistant_message,
        assistant_thinking,
        patch=TaskStatePatch(activate_skills=[name]),
        text=f"Skill '{name}' loaded; its instructions will appear in your "
        f"context from the next turn.",
        valid=True,
    )


# ===========================================================================
# spawn_subagent — reserved tool NAME + shared fan-out switch (kernel residue)
#
# The schema (``spawn_subagent_tool_schema``), the translate body
# (``_maybe_spawn_decision`` + ``_spawn_call_members``) and the
# ``translate_spawn_subagent`` seam moved into the ``delegation`` built-in
# (control-tool-surface S2). What stays kernel-side is the vocabulary the
# mechanism routes on: the reserved tool NAME (the subtask drain matches it on
# the recorded call, D8) and the ``concurrent_fanout_enabled`` switch, which the
# delegation translate AND the react orchestration ``parallel()`` both read (a
# helper shared by tools that live in two different plugins).
# ===========================================================================

#: Phase 4.5 Issue C — the model-visible **control** tool name a coding
#: parent calls to delegate to a named sub-agent. It is NOT an executable
#: workspace tool: the ``delegation`` built-in's translation seam turns a single
#: ``ToolUseBlock(tool_name=SPAWN_SUBAGENT_TOOL)`` into a
#: ``SpawnSubtaskDecision`` and the ToolRuntime never invokes it. The name stays
#: kernel-side because ``execution.subtask_drain`` routes on it (D8): the drain
#: is mechanism that must match the recorded tool name.
SPAWN_SUBAGENT_TOOL = "spawn_subagent"


#: fan-out v2 master switch — now
#: **default ON**.
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


# ===========================================================================
# run_workflow — schema + translate
# ===========================================================================

#: Model-visible **control** tool name: launch a model-authored orchestration
#: script that fans agents out as real subtasks. Translated into
#: a ``SpawnSubtaskDecision`` whose child carries the orchestration interpreter
#: Policy — same family / plumbing as ``spawn_subagent``.
RUN_WORKFLOW_TOOL = "run_workflow"

#: Reserved ``agent_name`` carried by the ``run_workflow`` → ``SpawnSubtaskDecision``
#: translation. The host's child-engine builder routes a child with this name to
#: :class:`noeta.policies.orchestration.OrchestrationPolicy` (reading script/args
#: from the child's ``TaskCreated.inputs``) instead of resolving a roster agent.
#: It belongs to the PermissionGuard ``allowed_subtask_agents`` allow-list (so the
#: orchestration spawn passes) but NOT to the model-facing ``spawn_subagent``
#: directory — the model reaches a workflow only through ``run_workflow``.
#: Defined here (not in ``orchestration``) so the translation seam can name it
#: without importing ``orchestration`` (which would cycle through ``react``).
WORKFLOW_AGENT_NAME = "__workflow__"

#: Structured semantics: the model's single source of truth for how
#: to author a workflow script — the available names, the return convention, and
#: the determinism constraint. Lives in an independent text resource
#: (``policies/descriptions/run_workflow.md``, four-section shape), not a Python
#: string. ``run_workflow`` is a **control-layer orchestration
#: tool** — it goes through ``SpawnSubtaskDecision`` (→ ``OrchestrationPolicy``),
#: NOT the ToolRuntime — so its description resource lives beside the control
#: vocabulary in ``noeta.policies`` rather than beside an execution tool's
#: impl (built-in tool descriptions ship in their builtin's package since
#: phase 2c).
_RUN_WORKFLOW_DESCRIPTION = load_control_tool_description("run_workflow")


def run_workflow_tool_schema() -> dict[str, Any]:
    """Provider-visible schema for :data:`RUN_WORKFLOW_TOOL`.

    A **control** tool (never an Engine/ToolRuntime tool): a single
    ``run_workflow`` call is translated into a ``SpawnSubtaskDecision`` whose
    child carries the orchestration interpreter Policy
    (:class:`noeta.policies.orchestration.OrchestrationPolicy`). Added to the
    Composer's ``control_action_schemas`` (so it lands in
    ``View.provider_tool_schemas`` + the stable hash) only when
    ``workflow_enabled`` — never registered as a ToolRuntime tool.
    """
    return {
        "type": "function",
        "function": {
            "name": RUN_WORKFLOW_TOOL,
            "description": _RUN_WORKFLOW_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": (
                            "The orchestration script (Python). Calls "
                            "parallel()/agent()/log(), reads args, and uses "
                            "`return` for the final answer."
                        ),
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Optional arguments exposed to the script as `args`."
                        ),
                    },
                },
                "required": ["script"],
            },
        },
    }


#: cap on the model-authored workflow script (recoverable over-cap,
#: like the other control-tool input caps; keeps the SubtaskSpawned/TaskCreated
#: inputs body bounded).
_WORKFLOW_MAX_SCRIPT_LEN = 16_000

#: Fixed (model-independent) goal seeded onto the orchestration subtask. The
#: OrchestrationPolicy reads the script from ``inputs``, not the goal — a
#: constant keeps the recorded subtask goal stable across resume.
_WORKFLOW_GOAL = "Execute workflow orchestration script."


def _maybe_workflow_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Translate a `run_workflow` control-tool call into a
    :class:`SpawnSubtaskDecision` whose child carries the orchestration
    interpreter Policy.

    Same family / plumbing as ``spawn_subagent`` — it just names the reserved
    :data:`WORKFLOW_AGENT_NAME` and ferries ``{script, args}`` through
    ``inputs`` (→ ``SubtaskSpawned`` → child ``TaskCreated.inputs``), where the
    host's child-engine builder reads them to construct
    :class:`noeta.policies.orchestration.OrchestrationPolicy`.

    Sole-call rule (mirrors the sibling control tools): ``run_workflow`` must be
    the only tool call in the turn; mixed → recoverable error ack (no subtask).
    A missing / non-string / empty / over-cap ``script`` is likewise a
    recoverable error. The script's deterministic-sandbox guard (AST) is applied
    downstream (issue 03); this seam only validates the call shape.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    workflow_blocks = [b for b in tool_uses if b.tool_name == RUN_WORKFLOW_TOOL]
    if not workflow_blocks:
        return None
    if len(workflow_blocks) != len(tool_uses) or len(workflow_blocks) != 1:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="run_workflow must be the only tool call in the turn",
            valid=False,
        )
    args = dict(workflow_blocks[0].arguments)
    ok, script_or_error = validate_required_string(
        args.get("script"), "script", _WORKFLOW_MAX_SCRIPT_LEN
    )
    if not ok:
        assert isinstance(script_or_error, str)
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=script_or_error,
            valid=False,
        )
    assert isinstance(script_or_error, str)
    # issue 03: deterministic-sandbox AST guard runs HERE (startup
    # / translation time) — a non-deterministic or malformed script is rejected
    # before any orchestration subtask is created, so a bad workflow never leaves
    # a half-run subtask behind. The model gets a recoverable ack pointing at the
    # offending line and may retry.
    script_error = check_workflow_script(script_or_error)
    if script_error is not None:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=script_error,
            valid=False,
        )
    raw_args = args.get("args")
    workflow_args = dict(raw_args) if isinstance(raw_args, dict) else {}
    return SpawnSubtaskDecision(
        agent_name=WORKFLOW_AGENT_NAME,
        goal=_WORKFLOW_GOAL,
        inputs={"script": script_or_error, "args": workflow_args},
        assistant_message=assistant_message,
        assistant_thinking=assistant_thinking,
    )


# ===========================================================================
# structured_output — per-helper structured return
# ===========================================================================

#: Model-visible **control** tool name a workflow helper subtask uses to return
#: a structured (JSON-Schema-shaped) result. Injected ONLY into the helper
#: subtask whose ``agent(goal, schema=...)`` declared a schema;
#: the orchestration interpreter's ``StructuredOutputPolicy`` wrapper intercepts
#: the call and finishes that helper with the call's arguments. Distinct from
#: the session-level ``output_schema`` (top-level final-answer shape).
STRUCTURED_OUTPUT_TOOL = "structured_output"

_STRUCTURED_OUTPUT_DESCRIPTION = load_control_tool_description(
    "structured_output"
)


def structured_output_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Provider-visible schema for :data:`STRUCTURED_OUTPUT_TOOL`.

    ``schema`` is the caller-supplied JSON Schema used verbatim as the tool's
    ``parameters`` — so the model's call arguments ARE the structured result.
    A **control** tool: never registered in the ToolRuntime; the helper's
    ``StructuredOutputPolicy`` wrapper turns a call into the helper's final
    answer. Added to the helper's ``control_action_schemas`` only when its
    ``agent()`` declared a schema (per-helper, opt-in)."""
    return {
        "type": "function",
        "function": {
            "name": STRUCTURED_OUTPUT_TOOL,
            "description": _STRUCTURED_OUTPUT_DESCRIPTION,
            "parameters": dict(schema),
        },
    }
