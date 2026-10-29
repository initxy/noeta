"""``NotebookEdit``: replace, insert or delete one cell of a Jupyter notebook
the model has read. Deferred, as in Claude Code.

A cell is named by the id ``Read`` shows (its ``id``, or ``cell-<index>``
for a notebook without ids). Replacing a code cell clears its outputs and
execution count. The notebook is written back with Jupyter's one-space
indent.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.fs._mutate import (
    WriteRoots,
    authorized_roots,
    read_precondition_error,
    record_written,
)
from noeta.toolkit.tools import (
    SCHEMA_DIALECT,
    PathError,
    Workspace,
    load_description,
    require_str,
    tool_error,
)

__all__ = ["NotebookEditTool"]

def _schema() -> dict[str, Any]:
    return {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string",
                              "description": "Absolute path of the notebook to edit."},
            "cell_id": {"type": "string",
                        "description": ("The cell to edit. With edit_mode insert, the new cell "
                                        "goes after this one, or first when it is left out.")},
            "new_source": {"type": "string", "description": "The cell's new source."},
            "cell_type": {"type": "string", "enum": ["code", "markdown"],
                          "description": ("code or markdown. Defaults to the cell's current "
                                          "type; required with edit_mode insert.")},
            "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"],
                          "description": "replace (the default), insert or delete."},
        },
        "required": ["notebook_path", "new_source"],
        "additionalProperties": False,
    }


def _find(cells: list[Any], cell_id: str) -> int | str:
    """The index of the cell ``cell_id`` names: its ``id``, or else the
    ``cell-<index>`` form (Claude Code accepts it for any notebook); or why
    there is none."""
    for i, cell in enumerate(cells):
        if isinstance(cell, dict) and cell.get("id") == cell_id:
            return i
    m = re.fullmatch(r"cell-(\d+)", cell_id)
    if m is None:
        return f"no cell with id {cell_id!r}"
    index = int(m.group(1))
    if index >= len(cells):
        return f"no cell at index {index} in the notebook"
    if not isinstance(cells[index], dict):
        return f"the cell at index {index} is not a cell object"
    return index


def _source(text: str) -> list[str]:
    """Notebook source form: lines keeping their newlines."""
    return text.splitlines(keepends=True)


def _input_error(input: dict[str, Any], path: str) -> str | None:
    """Why the call's arguments cannot edit a notebook, if they cannot."""
    if not isinstance(input.get("new_source"), str):
        return "requires string 'new_source'"
    mode = input.get("edit_mode") or "replace"
    if mode not in ("replace", "insert", "delete"):
        return "edit_mode must be replace, insert or delete"
    cell_type = input.get("cell_type")
    if cell_type not in (None, "code", "markdown"):
        return "cell_type must be code or markdown"
    cell_id = input.get("cell_id")
    if cell_id is not None and not isinstance(cell_id, str):
        return "cell_id must be a string"
    if mode != "insert" and not cell_id:
        return f"cell_id is required to {mode} a cell"
    if mode == "insert" and cell_type is None:
        return "cell_type is required to insert a cell"
    if not path.endswith(".ipynb"):
        return "only .ipynb notebooks can be edited; use Edit for other files"
    return None


def _has_cell_ids(nb: dict[str, Any]) -> bool:
    """Cell ids exist from nbformat 4.5 on; an older notebook gets none."""
    major, minor = nb.get("nbformat"), nb.get("nbformat_minor")
    if not isinstance(major, int) or not isinstance(minor, int):
        return False
    return major > 4 or (major == 4 and minor >= 5)


def _apply(cells: list[Any], mode: str, index: int | None, cell_id: str | None,
           cell_type: str | None, source: str, *, ids: bool) -> str:
    """Make the edit in ``cells``; the result line (Claude Code's: the cell
    and, for a replace or insert, the source now in it)."""
    if mode == "delete":
        assert index is not None
        cells.pop(index)
        return f"Deleted cell {cell_id}"
    if mode == "insert":
        at = 0 if index is None else index + 1
        cell: dict[str, Any] = {"cell_type": cell_type, "metadata": {},
                                "source": _source(source)}
        # Without ids the cell is named the way Read shows it.
        new_id = secrets.token_hex(4) if ids else f"cell-{at}"
        if ids:
            cell["id"] = new_id
        if cell_type == "code":
            cell.update(execution_count=None, outputs=[])
        cells.insert(at, cell)
        return f"Inserted cell {new_id} with {source}"
    assert index is not None
    cell = cells[index]
    cell["source"] = _source(source)
    if cell_type is not None and cell_type != cell.get("cell_type"):
        cell["cell_type"] = cell_type
        if cell_type == "markdown":
            cell.pop("outputs", None)
            cell.pop("execution_count", None)
    if cell.get("cell_type") == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return f"Updated cell {cell_id} with {source}"


class NotebookEditTool:
    name = "NotebookEdit"
    description = load_description("noeta.plugins.fs", "notebook_edit")
    read_only = False
    clearable = False
    deferred = True
    input_schema: dict[str, Any]

    def __init__(self, *, write_roots: WriteRoots | None = None) -> None:
        self.input_schema = _schema()
        self.write_roots = write_roots

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        path = require_str(input, "notebook_path")
        if path is None:
            return tool_error(self.name, "requires non-empty 'notebook_path'")
        wrong = _input_error(input, path)
        if wrong is not None:
            return tool_error(self.name, wrong)
        source: str = input["new_source"]
        mode: str = input.get("edit_mode") or "replace"
        cell_type: str | None = input.get("cell_type")
        cell_id: str | None = input.get("cell_id")

        env = ctx.env
        ws = await Workspace.of(env)
        try:
            resolved = await ws.resolve_write(
                path, await authorized_roots(ws, self.write_roots, ctx))
        except PathError as exc:
            return tool_error(self.name, str(exc))
        try:
            raw = await env.read_bytes(resolved)
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")
        refused = read_precondition_error(ctx, resolved, raw, tool_name=self.name,
                                          verb="editing")
        if refused is not None:
            return refused
        try:
            nb = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return tool_error(self.name, f"the notebook is not valid JSON: {exc}")
        cells = nb.get("cells") if isinstance(nb, dict) else None
        if not isinstance(cells, list):
            return tool_error(self.name, "the notebook has no cells array")

        found = _find(cells, cell_id) if cell_id else None
        if isinstance(found, str):
            return tool_error(self.name, found)
        index = found
        replaced = cells[index].get("source") if index is not None and mode != "insert" else None
        old_source = "".join(replaced) if isinstance(replaced, list) else replaced
        done = _apply(cells, mode, index, cell_id, cell_type, source, ids=_has_cell_ids(nb))
        body = (json.dumps(nb, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            await env.write_bytes(resolved, body)
        except OSError as exc:
            return tool_error(self.name, f"write failed: {exc}")
        await record_written(ctx, resolved, body)
        return text_output(done, path=ws.relative(resolved), mode=mode, old_source=old_source)
