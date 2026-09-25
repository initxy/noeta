"""``todo_write`` — the durable-checklist control tool: its provider-visible
schema, its ``todos`` validator, its translation into a neutral state-patch
Decision, and the factory that mounts it.

A ``todo_write`` call replace-alls ``TaskState.todos`` and is never invoked
through the ToolRuntime; the kernel holds none of this vocabulary and reaches
it only through the plugin loader's ``ref`` resolution. Routing and schema
bands are a byte-order contract the control-tool schema goldens pin.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Optional

from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    SPAWN_SUBAGENT_TOOL,
    ControlTranslateContext,
    ack_patch_decision,
)
from noeta.protocols.decisions import (
    Decision,
    SpawnSubtaskDecision,
    SpawnSubtasksDecision,
    StatePatchDecision,
    TaskStatePatch,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.resources import load_markdown


__all__ = [
    "TODO_WRITE_TOOL",
    "TODO_WRITE_ACK",
    "TODO_WRITE_STATUSES",
    "todo_write_tool_schema",
    "validate_todos",
    "translate_todo_write",
    "build_todo_write_control_tool",
]


#: Model-visible **control** tool name for durable checklist updates.
TODO_WRITE_TOOL = "TodoWrite"
#: Allowed ``status`` values for a todo item.
TODO_WRITE_STATUSES = ("pending", "in_progress", "completed")
#: The reference agent's ack text, returned verbatim on a valid update.
TODO_WRITE_ACK = (
    "Todos have been modified successfully. Ensure that you continue to use "
    "the todo list to track your progress. Please proceed with the current "
    "tasks if applicable"
)
#: Input caps. Over-cap → malformed (recoverable, no state write).
_TODO_MAX_ITEMS = 50
_TODO_MAX_CONTENT_LEN = 500
_TODO_MAX_ACTIVE_FORM_LEN = 500


_TODO_WRITE_DESCRIPTION = load_markdown(__package__, "todo_write")


def todo_write_tool_schema() -> dict[str, Any]:
    """Provider-visible schema for :data:`TODO_WRITE_TOOL`.

    Lands in the Composer's ``control_action_schemas`` — and thus in
    ``View.provider_tool_schemas`` and the stable hash — only when
    ``todo_write_enabled``; it is never registered as a ToolRuntime tool."""
    return {
        "type": "function",
        "function": {
            "name": TODO_WRITE_TOOL,
            "description": _TODO_WRITE_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": (
                            "The full checklist (replace-all). Each item: "
                            "{content, status, activeForm}."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": (
                                        "Imperative form of the task, e.g. "
                                        "'Run tests'."
                                    ),
                                },
                                "status": {
                                    "type": "string",
                                    "enum": list(TODO_WRITE_STATUSES),
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": (
                                        "Present-continuous form shown while "
                                        "in_progress, e.g. 'Running tests'."
                                    ),
                                },
                            },
                            "required": ["content", "status", "activeForm"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    }


def validate_todos(
    arguments: Any,
) -> tuple[bool, "list[dict[str, Any]] | str"]:
    """Return ``(True, normalized)`` or ``(False, error)``; never raises,
    because malformed model input is data to ack, not an error to propagate."""
    todos = arguments.get("todos") if isinstance(arguments, dict) else None
    if not isinstance(todos, list):
        return False, "todos must be a list"
    if len(todos) > _TODO_MAX_ITEMS:
        return False, f"too many todos (max {_TODO_MAX_ITEMS})"
    normalized: list[dict[str, Any]] = []
    for item in todos:
        if not isinstance(item, dict):
            return False, "each todo must be an object"
        content = item.get("content")
        status = item.get("status")
        active_form = item.get("activeForm")
        if not isinstance(content, str) or not content:
            return False, "each todo needs non-empty string content"
        if len(content) > _TODO_MAX_CONTENT_LEN:
            return False, f"todo content too long (max {_TODO_MAX_CONTENT_LEN})"
        if status not in TODO_WRITE_STATUSES:
            return False, (
                "todo status must be one of " + ", ".join(TODO_WRITE_STATUSES)
            )
        if not isinstance(active_form, str) or not active_form:
            return False, "each todo needs non-empty string activeForm"
        if len(active_form) > _TODO_MAX_ACTIVE_FORM_LEN:
            return False, (
                f"todo activeForm too long (max {_TODO_MAX_ACTIVE_FORM_LEN})"
            )
        normalized.append(
            {"content": content, "status": status, "activeForm": active_form}
        )
    return True, normalized


def _maybe_todo_write_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
    control_tool_names: frozenset[str] = frozenset(),
    translate_rest: Optional[
        Callable[[frozenset[str]], Optional[Decision]]
    ] = None,
) -> Decision | None:
    """Translate a ``TodoWrite`` call into a neutral Decision, or ``None`` when
    the turn holds no ``TodoWrite``.

    A solo call becomes the classic state-patch ack. A call BATCHED with other
    runtime tool calls — the reference agent's habitual shape — becomes a
    :class:`ToolCallsDecision` carrying the todos patch, the TodoWrite ack as a
    pre-answered result, and the remaining calls for the ToolRuntime, so the
    batching costs no extra round trip. A call batched with ``Task`` calls
    hands the rest of the response to the delegation translate
    (``translate_rest``) and rides the spawn Decision that comes back: the
    patch is saved at spawn time and the ack joins the spawn's one result
    message. A malformed ``todos`` arg, a second TodoWrite in the same turn,
    or any other control tool alongside yields a recoverable error ack
    instead — the task is NOT terminated.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    todo_blocks = [b for b in tool_uses if b.tool_name == TODO_WRITE_TOOL]
    if not todo_blocks:
        return None

    if len(todo_blocks) != 1:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=(
                "Nothing in this response ran: TodoWrite may be called only "
                "once per response. Re-issue it as a single call carrying the "
                "whole checklist."
            ),
            valid=False,
        )
    todo_block = todo_blocks[0]
    ok, result = validate_todos(todo_block.arguments)
    if not ok:
        assert isinstance(result, str)
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=result,
            valid=False,
        )
    assert isinstance(result, list)
    patch = TaskStatePatch(set_todos=list(result))
    others = [b for b in tool_uses if b is not todo_block]
    control_others = [
        b for b in others if b.tool_name in control_tool_names
    ]
    if control_others:
        ack = ToolResultBlock(
            call_id=todo_block.call_id, output=TODO_WRITE_ACK, success=True
        )
        if translate_rest is not None and all(
            b.tool_name == SPAWN_SUBAGENT_TOOL for b in control_others
        ):
            rest = translate_rest(frozenset({todo_block.call_id}))
            if (
                isinstance(rest, (SpawnSubtaskDecision, SpawnSubtasksDecision))
                and rest.state_patch is None
            ):
                # The patch is saved when the Engine applies the spawn
                # Decision; the ack joins the spawn's one result message.
                return replace(
                    rest,
                    state_patch=patch,
                    preacked_results=(ack, *rest.preacked_results),
                )
            refusal = _refusal_text(rest)
            if refusal is not None:
                # The Task calls were refused, so nothing runs: the checklist
                # is not saved either, and every call reads the same reason.
                return ack_patch_decision(
                    tool_uses,
                    assistant_message,
                    assistant_thinking,
                    patch=None,
                    text=refusal,
                    valid=False,
                )
        # Any other CONTROL tool shares the turn (AskUserQuestion, skill, …):
        # the ToolRuntime could never answer it, so the mix stays a
        # recoverable error rather than a half-run batch.
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=(
                "Nothing in this response ran: TodoWrite cannot share a "
                f"response with {control_others[0].tool_name}, and the "
                "checklist was not saved. Re-issue the other calls in a "
                "separate response."
            ),
            valid=False,
        )
    if not others:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=patch,
            text=TODO_WRITE_ACK,
            valid=True,
        )
    # Mixed turn: the todos patch + the TodoWrite ack ride the SAME decision
    # that carries the remaining calls to the ToolRuntime — one turn, one
    # batched result message, every tool_use answered exactly once.
    return ToolCallsDecision(
        calls=[
            ToolCall(
                tool_name=b.tool_name,
                arguments=dict(b.arguments),
                call_id=b.call_id,
            )
            for b in others
        ],
        state_patch=patch,
        assistant_message=assistant_message,
        assistant_thinking=assistant_thinking,
        preacked_results=(
            ToolResultBlock(
                call_id=todo_block.call_id,
                output=TODO_WRITE_ACK,
                success=True,
            ),
        ),
    )


def _refusal_text(decision: Optional[Decision]) -> Optional[str]:
    """The error text of a recoverable refusal ack, or ``None`` when
    ``decision`` is not one."""
    if not isinstance(decision, StatePatchDecision) or decision.patch is not None:
        return None
    for message in decision.messages_after:
        for block in message.content:
            if isinstance(block, ToolResultBlock) and not block.success:
                return block.error or ""
    return None


def translate_todo_write(ctx: ControlTranslateContext) -> Optional[Decision]:
    """The ``TodoWrite`` routing seam the mount binds into a ``ControlToolSpec``."""
    return _maybe_todo_write_decision(
        ctx.response,
        ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
        control_tool_names=ctx.control_tool_names,
        translate_rest=ctx.translate_rest,
    )


def build_todo_write_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``control_tool`` contribution factory (manifest ``ref`` target).

    Self-gates on the effective ``todo_write`` capability flag: mounting is
    enablement. Routing band 200 and schema band 200 are the byte-order
    contract the control-tool schema goldens pin — do not renumber.
    """
    if not ctx.flag("todo_write"):
        return None
    return ControlToolMount(
        name=TODO_WRITE_TOOL,
        schema=todo_write_tool_schema(),
        translate=translate_todo_write,
        routing_priority=200,
        schema_priority=200,
    )
