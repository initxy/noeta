"""The ``run_workflow`` and ``structured_output`` control tools.

Both belong to the workflow story the ``react`` built-in owns, so their schemas,
descriptions and the ``run_workflow`` → :class:`SpawnSubtaskDecision`
translation sit beside :class:`OrchestrationPolicy` rather than in the kernel's
neutral control band. Neither is ever registered with the ToolRuntime: a call is
intercepted and turned into a Decision.
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


_RUN_WORKFLOW_DESCRIPTION = load_markdown(__package__, "run_workflow")


def run_workflow_tool_schema() -> dict[str, Any]:
    """Provider-visible schema for :data:`RUN_WORKFLOW_TOOL`; a call becomes a
    ``SpawnSubtaskDecision`` whose child carries the orchestration interpreter."""
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


#: Cap on the model-authored script: an over-cap call is a recoverable error,
#: and it keeps the ``SubtaskSpawned`` / ``TaskCreated`` inputs body bounded.
_WORKFLOW_MAX_SCRIPT_LEN = 16_000

#: ``OrchestrationPolicy`` reads the script from ``inputs``, never from the
#: goal, so a constant goal keeps the recorded subtask stable across resume.
_WORKFLOW_GOAL = "Execute workflow orchestration script."


def _maybe_workflow_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Translate a ``run_workflow`` call into a :class:`SpawnSubtaskDecision`.

    ``{script, args}`` ride through ``inputs`` to the child's ``TaskCreated``,
    where the host's child-engine builder reads them to construct the
    orchestration interpreter Policy. ``run_workflow`` must be the sole tool call
    in the turn; a mixed turn, or a missing / non-string / empty / over-cap
    ``script``, is a recoverable error ack rather than a spawned subtask.
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
    # The determinism guard runs at translation time so a rejected script never
    # leaves a half-run subtask behind.
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
    return _maybe_workflow_decision(
        ctx.response,
        ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
    )


#: How a workflow helper subtask returns a structured result. Injected only into
#: the helper whose ``agent(goal, schema=...)`` declared a schema, and
#: intercepted by ``StructuredOutputPolicy``, which finishes that helper with the
#: call's arguments. Distinct from the session-level ``output_schema``, which
#: shapes the top-level final answer.
STRUCTURED_OUTPUT_TOOL = "structured_output"

_STRUCTURED_OUTPUT_DESCRIPTION = load_markdown(__package__, "structured_output")


def structured_output_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Provider-visible schema for :data:`STRUCTURED_OUTPUT_TOOL`.

    ``schema`` is used verbatim as the tool's ``parameters``, so the model's call
    arguments ARE the structured result the helper's ``StructuredOutputPolicy``
    finishes with."""
    return {
        "type": "function",
        "function": {
            "name": STRUCTURED_OUTPUT_TOOL,
            "description": _STRUCTURED_OUTPUT_DESCRIPTION,
            "parameters": dict(schema),
        },
    }


def build_run_workflow_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``run_workflow`` ``control_tool`` contribution factory.

    Self-gates on the effective ``workflow`` flag — mounting IS enablement. The
    priority bands decide schema order, which a golden pins byte-for-byte:
    changing them changes the prompt.
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

    Gated on data rather than an activation: the per-helper schema being
    present. ``translate=None`` keeps the mount out of the routing order,
    because ``StructuredOutputPolicy`` intercepts the call itself.
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
