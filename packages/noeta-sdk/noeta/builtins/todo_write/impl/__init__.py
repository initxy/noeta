"""``todo_write`` — the durable-checklist control tool: its provider-visible
schema, its ``todos`` validator, its translation into a neutral state-patch
Decision, and the factory that mounts it.

A ``todo_write`` call replace-alls ``TaskState.todos`` and is never invoked
through the ToolRuntime; the kernel holds none of this vocabulary and reaches
it only through the plugin loader's ``ref`` resolution. Routing and schema
bands are a byte-order contract the control-tool schema goldens pin.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    ControlTranslateContext,
    ack_patch_decision,
)
from noeta.protocols.decisions import (
    Decision,
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
) -> Decision | None:
    """Translate a ``TodoWrite`` call into a neutral Decision, or ``None`` when
    the turn holds no ``TodoWrite``.

    A solo call becomes the classic state-patch ack. A call BATCHED with other
    runtime tool calls — the reference agent's habitual shape — becomes a
    :class:`ToolCallsDecision` carrying the todos patch, the TodoWrite ack as a
    pre-answered result, and the remaining calls for the ToolRuntime, so the
    batching costs no extra round trip. A malformed ``todos`` arg (or a second
    TodoWrite in the same turn) yields a recoverable error ack instead — the
    task is NOT terminated.
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
            text="TodoWrite may appear at most once per turn",
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
        # Another CONTROL tool shares the turn (Task / AskUserQuestion): the
        # ToolRuntime could never answer it, so the mix stays a recoverable
        # error rather than a half-run batch.
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=(
                "TodoWrite cannot be batched with another control tool "
                f"({control_others[0].tool_name}); issue them in separate turns"
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


def translate_todo_write(ctx: ControlTranslateContext) -> Optional[Decision]:
    """The ``TodoWrite`` routing seam the mount binds into a ``ControlToolSpec``."""
    return _maybe_todo_write_decision(
        ctx.response,
        ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
        control_tool_names=ctx.control_tool_names,
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
