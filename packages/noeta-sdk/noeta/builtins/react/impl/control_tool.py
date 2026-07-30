"""``run_workflow`` + ``structured_output`` — the react built-in's control tools
(control-tool-surface S2b).

Both control tools belong to the workflow orchestration story the ``react``
built-in owns (``OrchestrationPolicy`` / ``StructuredOutputPolicy``), so their
whole material moved here out of the kernel's control band
(``noeta.policies.control_semantics``): the ``run_workflow`` schema + its
response→``SpawnSubtaskDecision`` translate body, the ``structured_output``
schema, and both ``.md`` descriptions (``run_workflow.md`` /
``structured_output.md``, git-moved beside this impl). The determinism sandbox
(``workflow_sandbox``) moved into this same package. The move is byte-preserving
(the S0 golden pins the schema bytes).

What this module imports back from the kernel is the neutral MECHANISM: the
control-tool mount types (``noeta.execution.control_tool``), the decision-time
``ControlTranslateContext``, the shared ack builder + string validator
(``ack_patch_decision`` / ``validate_required_string``), and the reserved
vocabulary the drain/resolver route on (``RUN_WORKFLOW_TOOL`` /
``WORKFLOW_AGENT_NAME`` — see D8). The determinism check reads from the sibling
``workflow_sandbox`` module.

Reached only through the plugin loader's ``ref`` resolution (the ``react``
manifest's two ``control_tool`` contributions); ``STRUCTURED_OUTPUT_TOOL`` is
imported by the sibling ``orchestration`` module (its ``StructuredOutputPolicy``
intercepts the call), which is why the constant lives here.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    RUN_WORKFLOW_TOOL,
    WORKFLOW_AGENT_NAME,
    ControlTranslateContext,
    ack_patch_decision,
    validate_required_string,
)
from noeta.protocols.decisions import (
    Decision,
    SpawnSubtaskDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolUseBlock,
)
from noeta.protocols.resources import load_markdown

from .workflow_sandbox import check_workflow_script


__all__ = [
    "STRUCTURED_OUTPUT_TOOL",
    "run_workflow_tool_schema",
    "translate_run_workflow",
    "structured_output_tool_schema",
    "build_run_workflow_control_tool",
    "build_structured_output_control_tool",
]


# ===========================================================================
# run_workflow — schema + translate
# ===========================================================================


_RUN_WORKFLOW_DESCRIPTION = load_markdown(__package__, "run_workflow")


def run_workflow_tool_schema() -> dict[str, Any]:
    """Provider-visible schema for :data:`RUN_WORKFLOW_TOOL`.

    A **control** tool (never an Engine/ToolRuntime tool): a single
    ``run_workflow`` call is translated into a ``SpawnSubtaskDecision`` whose
    child carries the orchestration interpreter Policy
    (:class:`noeta.builtins.react.impl.orchestration.OrchestrationPolicy`).
    Added to the Composer's ``control_action_schemas`` (so it lands in
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
    :class:`noeta.builtins.react.impl.orchestration.OrchestrationPolicy`.

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


def translate_run_workflow(ctx: ControlTranslateContext) -> Optional[Decision]:
    """The ``run_workflow`` routing seam the mount binds into a ``ControlToolSpec``."""
    return _maybe_workflow_decision(
        ctx.response,
        ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
    )


# ===========================================================================
# structured_output — per-helper structured return
# ===========================================================================

#: Model-visible **control** tool name a workflow helper subtask uses to return
#: a structured (JSON-Schema-shaped) result. Injected ONLY into the helper
#: subtask whose ``agent(goal, schema=...)`` declared a schema;
#: the orchestration interpreter's ``StructuredOutputPolicy`` wrapper intercepts
#: the call and finishes that helper with the call's arguments. Distinct from
#: the session-level ``output_schema`` (top-level final-answer shape). Lives here
#: (not kernel-side) because ``StructuredOutputPolicy`` in the sibling
#: ``orchestration`` module is the only thing that routes on it.
STRUCTURED_OUTPUT_TOOL = "structured_output"

_STRUCTURED_OUTPUT_DESCRIPTION = load_markdown(__package__, "structured_output")


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


# ===========================================================================
# control_tool contribution factories (manifest ``ref`` targets)
# ===========================================================================


def build_run_workflow_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``run_workflow`` ``control_tool`` contribution factory.

    Self-gates on the effective ``workflow`` capability flag (mounting IS
    enablement) and reproduces the pre-migration internal ``_run_workflow_mount``
    exactly: routing band 500, schema band 500 — the byte order the S0 golden
    pins.
    """
    if not ctx.flag("workflow"):
        return None
    return ControlToolMount(
        name=RUN_WORKFLOW_TOOL,
        schema=run_workflow_tool_schema(),
        translate=translate_run_workflow,
        routing_priority=500,
        schema_priority=500,
    )


def build_structured_output_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``structured_output`` ``control_tool`` contribution factory.

    Self-gates on a data-driven condition, not an activation: the per-helper
    structured-output schema being present (``ctx.structured_output_schema is not
    None``). It reproduces the pre-migration internal ``_structured_output_mount``
    exactly: ``translate=None`` (react's ``StructuredOutputPolicy`` intercepts the
    call, so the mount is excluded from the routing order) and schema band 600 —
    the byte order the S0 golden pins.
    """
    if ctx.structured_output_schema is None:
        return None
    return ControlToolMount(
        name=STRUCTURED_OUTPUT_TOOL,
        schema=structured_output_tool_schema(ctx.structured_output_schema),
        translate=None,
        routing_priority=0,
        schema_priority=600,
    )
