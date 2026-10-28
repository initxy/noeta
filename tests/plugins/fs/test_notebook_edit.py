"""``NotebookEdit``: replace, insert and delete a cell of a notebook read first."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from noeta.plugins.fs import NotebookEditTool, ReadTool
from tests.plugins.conftest import Harness, text_of


def _nb(ws: Path) -> Path:
    path = ws / "a.ipynb"
    path.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "code", "id": "c1", "metadata": {}, "source": ["x = 1\n", "x"],
         "execution_count": 3, "outputs": [{"output_type": "stream", "text": "1"}]},
        {"cell_type": "markdown", "id": "m1", "metadata": {}, "source": ["# Title"]},
    ]}))
    return path


def _cells(path: Path) -> list[dict[str, Any]]:
    return list(json.loads(path.read_text())["cells"])


async def test_replace_insert_delete(h: Harness, ws: Path) -> None:
    path = _nb(ws)
    tool = NotebookEditTool()
    refused = await h.call(tool, notebook_path="a.ipynb", cell_id="c1", new_source="y")
    assert refused.is_error and "Read it first" in text_of(refused)
    await h.call(ReadTool(), file_path="a.ipynb")
    out = await h.call(tool, notebook_path="a.ipynb", cell_id="c1", new_source="y = 2\ny")
    assert text_of(out) == f"Updated cell c1 in {path}."
    [code, _md] = _cells(path)
    assert code["source"] == ["y = 2\n", "y"] and code["outputs"] == []
    assert code["execution_count"] is None
    out = await h.call(tool, notebook_path="a.ipynb", cell_id="c1", new_source="note",
                       edit_mode="insert", cell_type="markdown")
    assert text_of(out).startswith("Inserted cell ")
    assert [c["cell_type"] for c in _cells(path)] == ["code", "markdown", "markdown"]
    first = await h.call(tool, notebook_path="a.ipynb", new_source="import os",
                         edit_mode="insert", cell_type="code")
    assert not first.is_error and _cells(path)[0]["source"] == ["import os"]
    gone = await h.call(tool, notebook_path="a.ipynb", cell_id="m1", new_source="",
                        edit_mode="delete")
    assert text_of(gone) == f"Deleted cell m1 in {path}."
    assert "m1" not in [c["id"] for c in _cells(path)]


async def test_bad_calls(h: Harness, ws: Path) -> None:
    _nb(ws)
    await h.call(ReadTool(), file_path="a.ipynb")
    tool = NotebookEditTool()
    nope = await h.call(tool, notebook_path="a.ipynb", cell_id="zz", new_source="x")
    assert nope.is_error and "no cell with id 'zz'" in text_of(nope)
    no_type = await h.call(tool, notebook_path="a.ipynb", new_source="x", edit_mode="insert")
    assert no_type.is_error and "cell_type is required" in text_of(no_type)
    no_id = await h.call(tool, notebook_path="a.ipynb", new_source="x")
    assert no_id.is_error and "cell_id is required" in text_of(no_id)
    other = await h.call(tool, notebook_path="a.txt", cell_id="c1", new_source="x")
    assert other.is_error and "only .ipynb" in text_of(other)


def test_schema_and_deferral() -> None:
    tool = NotebookEditTool()
    assert tool.deferred and tool.input_schema["required"] == ["notebook_path", "new_source"]
    assert tool.input_schema["additionalProperties"] is False
