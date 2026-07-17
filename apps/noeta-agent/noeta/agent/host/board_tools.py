"""Board tools (host side).

`board_list / board_create_card / board_update_card / board_move_card`:
space members have the agent jot down or move cards in passing from a
session / channel topic, and the space agent can therefore answer "what is
still unfinished". Pure host-side sqlite reads/writes; never enters the
container.

Ownership resolution: task → session → space (hard cross-space isolation —
the tools can only operate on the board of the space owning the initiating
session). Cards created inside a channel topic automatically get a back-link
to the topic (resolve_topic_link).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools.decorator import DecoratedTool, tool

from noeta.agent.store.board import BOARD_COLUMNS

_COLUMN_LABELS = {"todo": "To do", "doing": "In progress", "done": "Done"}

_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Card title (required, keep it short)"},
        "description": {"type": "string", "description": "Card description (optional)"},
        "column": {
            "type": "string",
            "enum": list(BOARD_COLUMNS),
            "description": "Column: todo (To do) / doing (In progress) / done (Done); defaults to todo",
        },
        "assignee": {"type": "string", "description": "Assignee username (optional)"},
        "due_date": {"type": "string", "description": "Due date YYYY-MM-DD (optional)"},
    },
    "required": ["title"],
}

_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "card_id": {"type": "string", "description": "Card ID (find it via board_list)"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "assignee": {"type": "string"},
        "due_date": {"type": "string", "description": "YYYY-MM-DD"},
    },
    "required": ["card_id"],
}

_MOVE_SCHEMA = {
    "type": "object",
    "properties": {
        "card_id": {"type": "string", "description": "Card ID (find it via board_list)"},
        "column": {
            "type": "string",
            "enum": list(BOARD_COLUMNS),
            "description": "Target column: todo (To do) / doing (In progress) / done (Done)",
        },
    },
    "required": ["card_id", "column"],
}

_NO_BOARD = "The current task does not belong to any team space; the board tools are unavailable."


def _format_card(c: dict) -> str:
    parts = [f"[{c['id']}] {c['title']}"]
    if c.get("assignee"):
        parts.append(f"assignee:{c['assignee']}")
    if c.get("due_date"):
        parts.append(f"due:{c['due_date']}")
    return " ".join(parts)


def build_board_tools(
    get_board_store: Callable[[], Optional[Any]],
    resolve_space: Callable[[str], Optional[str]],
    resolve_topic_link: Callable[[str], Optional[dict]],
) -> tuple[DecoratedTool, ...]:
    def _resolve(ctx: ToolContext) -> tuple[Optional[Any], Optional[str], str]:
        store = get_board_store()
        task_id = str(ctx.metadata.get("task_id") or "")
        space_id = resolve_space(task_id) if task_id else None
        return store, space_id, task_id

    def _card_in_space(store: Any, space_id: str, card_id: str) -> Optional[dict]:
        card = store.get_card(card_id)
        if card is None or card["space_id"] != space_id:
            return None
        return card

    @tool(
        name="board_list",
        version="1",
        risk_level="low",
        input_schema={"type": "object", "properties": {}},
        description=(
            "View all cards on this space's task board (grouped into the "
            "To do / In progress / Done columns), including card ID, title, "
            "assignee, and due date. Use it first to check current state before "
            "answering task-progress questions or operating on cards."
        ),
    )
    def board_list(arguments: dict, ctx: ToolContext) -> ToolResult:
        store, space_id, _ = _resolve(ctx)
        if store is None or space_id is None:
            return ToolResult(success=False, output=_NO_BOARD)
        cards = store.list_cards(space_id)
        if not cards:
            return ToolResult(success=True, output="(the board has no cards yet)")
        lines: list[str] = []
        for key in BOARD_COLUMNS:
            group = [c for c in cards if c["column_key"] == key]
            lines.append(f"## {_COLUMN_LABELS[key]} ({len(group)})")
            lines += [_format_card(c) for c in group] or ["(empty)"]
        return ToolResult(
            success=True, output="\n".join(lines),
            summary=f"board has {len(cards)} cards",
        )

    @tool(
        name="board_create_card",
        version="1",
        risk_level="low",
        input_schema=_CREATE_SCHEMA,
        description=(
            "Create a card on this space's task board (use when the user says "
            "\"put it on the board / create a task\"). When created inside a "
            "channel topic, a back-link to this topic is attached automatically."
        ),
    )
    def board_create_card(arguments: dict, ctx: ToolContext) -> ToolResult:
        store, space_id, task_id = _resolve(ctx)
        if store is None or space_id is None:
            return ToolResult(success=False, output=_NO_BOARD)
        title = str(arguments.get("title") or "").strip()
        if not title:
            return ToolResult(success=False, output="title must not be empty")
        link = resolve_topic_link(task_id) if task_id else None
        try:
            card = store.create_card(
                space_id,
                title[:100],
                created_by="agent",
                column_key=str(arguments.get("column") or "todo"),
                description=str(arguments.get("description") or ""),
                assignee=arguments.get("assignee") or None,
                due_date=arguments.get("due_date") or None,
                links=[link] if link else [],
            )
        except ValueError as exc:
            return ToolResult(success=False, output=str(exc))
        return ToolResult(
            success=True,
            output=f"created card {_format_card(card)} ({_COLUMN_LABELS[card['column_key']]})",
            summary=f"board: created card {title[:40]}",
        )

    @tool(
        name="board_update_card",
        version="1",
        risk_level="low",
        input_schema=_UPDATE_SCHEMA,
        description="Update a board card's title / description / assignee / due date (to move it between columns use board_move_card).",
    )
    def board_update_card(arguments: dict, ctx: ToolContext) -> ToolResult:
        store, space_id, _ = _resolve(ctx)
        if store is None or space_id is None:
            return ToolResult(success=False, output=_NO_BOARD)
        card_id = str(arguments.get("card_id") or "")
        card = _card_in_space(store, space_id, card_id)
        if card is None:
            return ToolResult(success=False, output=f"card not found: {card_id}")
        fields = {
            k: arguments[k]
            for k in ("title", "description", "assignee", "due_date")
            if arguments.get(k) is not None
        }
        if not fields:
            return ToolResult(success=False, output="no fields to update")
        store.update_card(card_id, **fields)
        return ToolResult(
            success=True,
            output=f"updated card {_format_card(store.get_card(card_id))}",
            summary=f"board: updated card {card_id}",
        )

    @tool(
        name="board_move_card",
        version="1",
        risk_level="low",
        input_schema=_MOVE_SCHEMA,
        description=(
            "Move a board card to the target column (use when the user says "
            "\"move it to in progress / mark it done\"); the card lands at the "
            "end of the target column."
        ),
    )
    def board_move_card(arguments: dict, ctx: ToolContext) -> ToolResult:
        store, space_id, _ = _resolve(ctx)
        if store is None or space_id is None:
            return ToolResult(success=False, output=_NO_BOARD)
        card_id = str(arguments.get("card_id") or "")
        column = str(arguments.get("column") or "")
        if _card_in_space(store, space_id, card_id) is None:
            return ToolResult(success=False, output=f"card not found: {card_id}")
        try:
            card = store.move_to_column_end(card_id, column)
        except ValueError as exc:
            return ToolResult(success=False, output=str(exc))
        return ToolResult(
            success=True,
            output=f"moved card {card['title']} to \"{_COLUMN_LABELS[column]}\"",
            summary=f"board: moved card {card_id} → {column}",
        )

    return board_list, board_create_card, board_update_card, board_move_card


__all__ = ["build_board_tools"]
