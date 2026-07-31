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
from noeta.protocols.decisions import Decision, TaskStatePatch
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolUseBlock,
)
from noeta.protocols.resources import load_markdown


__all__ = [
    "TODO_WRITE_TOOL",
    "TODO_WRITE_STATUSES",
    "todo_write_tool_schema",
    "validate_todos",
    "translate_todo_write",
    "build_todo_write_control_tool",
]


#: Model-visible **control** tool name for durable checklist updates.
TODO_WRITE_TOOL = "todo_write"
#: Allowed ``status`` values for a todo item.
TODO_WRITE_STATUSES = ("pending", "in_progress", "completed")
#: Input caps. Over-cap → malformed (recoverable, no state write).
_TODO_MAX_ITEMS = 50
_TODO_MAX_ID_LEN = 64
_TODO_MAX_CONTENT_LEN = 500


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
                            "{id, content, status}."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": list(TODO_WRITE_STATUSES),
                                },
                            },
                            "required": ["id", "content", "status"],
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
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in todos:
        if not isinstance(item, dict):
            return False, "each todo must be an object"
        tid = item.get("id")
        content = item.get("content")
        status = item.get("status")
        if not isinstance(tid, str) or not tid:
            return False, "each todo needs a non-empty string id"
        if len(tid) > _TODO_MAX_ID_LEN:
            return False, f"todo id too long (max {_TODO_MAX_ID_LEN})"
        if tid in seen_ids:
            return False, f"duplicate todo id: {tid!r}"
        seen_ids.add(tid)
        if not isinstance(content, str) or not content:
            return False, "each todo needs non-empty string content"
        if len(content) > _TODO_MAX_CONTENT_LEN:
            return False, f"todo content too long (max {_TODO_MAX_CONTENT_LEN})"
        if status not in TODO_WRITE_STATUSES:
            return False, (
                "todo status must be one of " + ", ".join(TODO_WRITE_STATUSES)
            )
        normalized.append({"id": tid, "content": content, "status": status})
    return True, normalized


def _maybe_todo_write_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Translate a ``todo_write`` call into a neutral state-patch Decision, or
    ``None`` when the turn holds no ``todo_write``.

    ``todo_write`` must be the **sole** tool call in the turn; mixing it with
    any other call, or a malformed ``todos`` arg, yields a patch-free ack
    carrying the error so the model can retry — the task is NOT terminated.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    todo_blocks = [b for b in tool_uses if b.tool_name == TODO_WRITE_TOOL]
    if not todo_blocks:
        return None

    if len(todo_blocks) != len(tool_uses) or len(todo_blocks) != 1:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="todo_write must be the only tool call in the turn",
            valid=False,
        )
    ok, result = validate_todos(todo_blocks[0].arguments)
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
    return ack_patch_decision(
        tool_uses,
        assistant_message,
        assistant_thinking,
        patch=TaskStatePatch(set_todos=list(result)),
        text=f"todos updated: {len(result)} item(s)",
        valid=True,
    )


def translate_todo_write(ctx: ControlTranslateContext) -> Optional[Decision]:
    """The ``todo_write`` routing seam the mount binds into a ``ControlToolSpec``."""
    return _maybe_todo_write_decision(
        ctx.response,
        ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
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
