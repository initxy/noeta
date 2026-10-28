"""The four memory tools. Each finds its store through the extension's
recorded state (``ctx.state["memory"]["root"]``), so a task keeps the root
it was given at its start, on any worker.

Results read as 0.6 sent them: a success is the output object as JSON
(``ensure_ascii=False``), a failure is ``"<tool>: <message>"``.
"""

from __future__ import annotations

import asyncio
import json
from importlib import resources
from pathlib import Path
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.memory.store import MEMORY_TYPES, MemoryStore, is_valid_name, write_memory
from noeta.toolkit.tools import truncate_bytes

__all__ = [
    "MEMORY_ARCHIVE",
    "MEMORY_READ",
    "MEMORY_SEARCH",
    "MEMORY_WRITE",
    "MemoryArchiveTool",
    "MemoryReadTool",
    "MemorySearchTool",
    "MemoryWriteTool",
    "load_text",
]

MEMORY_WRITE = "memory_write"
MEMORY_READ = "memory_read"
MEMORY_SEARCH = "memory_search"
MEMORY_ARCHIVE = "memory_archive"

#: A ``memory_read`` body past this is cut and the result says so.
INLINE_CONTENT_MAX_BYTES = 1024 * 1024
_SEARCH_MAX_MEMORIES = 10


def load_text(name: str, *, strip: bool = True) -> str:
    text = (resources.files("noeta.plugins.memory").joinpath("descriptions", f"{name}.md")
            .read_text(encoding="utf-8"))
    return text.rstrip("\n") if strip else text


def _ok(output: dict[str, Any]) -> ToolOutput:
    return text_output(json.dumps(output, ensure_ascii=False))


def _err(tool: str, message: str) -> ToolOutput:
    return text_output(f"{tool}: {message}", is_error=True)


class _MemoryTool:
    name = ""
    read_only = False
    clearable = False

    def __init__(self, extension: str) -> None:
        self.extension = extension
        self.description = load_text(self.name)

    def store(self, ctx: ToolContext) -> MemoryStore | None:
        root = (ctx.state.get(self.extension) or {}).get("root")
        return MemoryStore(Path(root)) if root else None


def _name_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Memory name as listed in the memory index."},
        },
        "required": ["name"],
        "additionalProperties": False,
    }


class MemoryWriteTool(_MemoryTool):
    name = MEMORY_WRITE

    def __init__(self, extension: str, *, max_bytes: int | None = None) -> None:
        super().__init__(extension)
        self.max_bytes = max_bytes
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Slug-style memory name (letters, digits, '.', "
                        "'_', '-'); reusing a name updates that memory."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Full memory body (markdown), replacing the body "
                        "the memory has. Without a 'description' "
                        "parameter, the first line becomes the index "
                        "summary. Text that carries only a frontmatter "
                        "fence keeps the existing body."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "One-line summary shown in the memory index "
                        "instead of the first body line. Omit to keep the "
                        "current one, \"\" to remove it."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": [*MEMORY_TYPES, ""],
                    "description": (
                        "Memory category shown in the index. Omit to keep "
                        "the current one, \"\" to remove it."
                    ),
                },
                "keywords": {
                    "type": "string",
                    "description": (
                        "Comma-separated retrieval aliases — synonyms and "
                        "cross-language equivalents (e.g. both English and "
                        "Chinese terms) that should auto-recall this "
                        "memory. Omit to keep the current ones, \"\" to "
                        "remove them."
                    ),
                },
                "related": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Memory names, as the index lists them, that "
                        "recall surfaces alongside this one when this "
                        "memory is recalled. Omit to keep the current "
                        "ones, [] to remove them."
                    ),
                },
            },
            "required": ["name", "text"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        store = self.store(ctx)
        if store is None:
            return _err(self.name, "no memory store is configured for this task")
        result = await asyncio.to_thread(write_memory, store, input, task_id=ctx.task_id,
                                         max_bytes=self.max_bytes, tool=self.name)
        if result.error is not None or result.output is None:
            return text_output(result.error or f"{self.name}: write failed", is_error=True)
        return _ok(result.output)


class MemoryReadTool(_MemoryTool):
    name = MEMORY_READ
    read_only = True

    def __init__(self, extension: str) -> None:
        super().__init__(extension)
        self.input_schema = _name_schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        name = input.get("name")
        if not is_valid_name(name):
            return _err(self.name, f"invalid memory name {name!r}")
        assert isinstance(name, str)
        store = self.store(ctx)
        text = await asyncio.to_thread(store.read, name) if store is not None else None
        if text is None:
            return _err(self.name, f"no memory named {name!r}")
        total = len(text.encode("utf-8"))
        output: dict[str, Any] = {"name": name, "text": text}
        if total > INLINE_CONTENT_MAX_BYTES:
            text = truncate_bytes(text, INLINE_CONTENT_MAX_BYTES)
            shown = len(text.encode("utf-8"))
            output["text"] = text
            output["bytes"] = shown
            output["total_bytes"] = total
            output["note"] = (
                f"Showing the first {shown} of {total} bytes. Use "
                f"'{MEMORY_SEARCH}' to find specific lines in the rest."
            )
        output["truncated"] = total > INLINE_CONTENT_MAX_BYTES
        return _ok(output)


class MemorySearchTool(_MemoryTool):
    name = MEMORY_SEARCH
    read_only = True

    def __init__(self, extension: str) -> None:
        super().__init__(extension)
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Plain substring to look for (case-insensitive).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        query = input.get("query")
        if not isinstance(query, str) or not query:
            return _err(self.name, "requires non-empty string 'query'")
        store = self.store(ctx)
        hits = await asyncio.to_thread(store.search, query) if store is not None else ()
        results = [{"name": n, "lines": list(lines)} for n, lines in hits[:_SEARCH_MAX_MEMORIES]]
        return _ok({"query": query, "results": results, "total": len(hits),
                    "truncated": len(hits) > _SEARCH_MAX_MEMORIES})


class MemoryArchiveTool(_MemoryTool):
    name = MEMORY_ARCHIVE

    def __init__(self, extension: str) -> None:
        super().__init__(extension)
        self.input_schema = _name_schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        name = input.get("name")
        if not is_valid_name(name):
            return _err(self.name, f"invalid memory name {name!r}")
        assert isinstance(name, str)
        store = self.store(ctx)
        if store is None:
            return _err(self.name, f"no memory named {name!r}")
        try:
            dest = await asyncio.to_thread(store.archive, name)
        except OSError as exc:
            return _err(self.name, f"archive failed: {exc}")
        if dest is None:
            return _err(self.name, f"no memory named {name!r}")
        return _ok({"name": name, "archived_to": str(dest.relative_to(store.root))})
