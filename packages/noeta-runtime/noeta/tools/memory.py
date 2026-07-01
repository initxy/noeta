"""Memory v1 tools — writing a memory / reading its full text on demand are ORDINARY tools.

One file per memory under a single host-chosen directory (a fixed global
dir, default ``~/.noeta/memories``, wired by ``noeta.execution.memory``;
never per-session workspace).
Writing a memory is a routine act — the whole point of the ``evolving``
drift policy its index recordings carry — so these are plain SDK tools
with zero new mechanisms: results travel the ordinary tool-result
channel, the runtime is untouched.

* :class:`MemoryStore` — the file-per-memory store. Names are strict
  slugs (no separators, no leading dot), so a model-supplied name can
  never escape the memory directory. ``entries()`` is the deterministic
  (sorted) index source consumed by ``noeta.context.memory``.
* :class:`MemoryWriteTool` / :class:`MemoryReadTool` — the same
  dataclass shape as the fs tool pack.

Layering note: this module deliberately knows nothing about the content
channel — ``noeta.tools`` and ``noeta.context`` are independent siblings,
so the store hands over plain ``(name, summary)`` tuples and the glue
lives one band up in ``noeta.execution.memory``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools._limits import INLINE_CONTENT_MAX_BYTES, truncate_bytes


__all__ = [
    "MEMORY_FILE_SUFFIX",
    "MEMORY_READ_TOOL_NAME",
    "MEMORY_WRITE_TOOL_NAME",
    "MemoryReadTool",
    "MemoryStore",
    "MemoryWriteTool",
    "build_memory_tools",
]


MEMORY_WRITE_TOOL_NAME = "memory_write"
MEMORY_READ_TOOL_NAME = "memory_read"
MEMORY_FILE_SUFFIX = ".md"

#: Strict slug: starts alphanumeric, then alphanumerics / ``.`` / ``_`` /
#: ``-`` only — no path separators, no leading dot, bounded length. A
#: valid name can never traverse out of the memory directory.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Summary display cap for index entries (first non-empty line).
_SUMMARY_MAX_CHARS = 200


def _is_valid_name(name: object) -> bool:
    return isinstance(name, str) and _NAME_RE.match(name) is not None


def _first_line_summary(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:_SUMMARY_MAX_CHARS]
    return ""


@dataclass(frozen=True)
class MemoryStore:
    """File-per-memory store rooted at one directory.

    A missing root is a valid empty store (a workspace without memories
    configures nothing and pays nothing). ``write`` creates the root on
    first use; ``entries()`` lists ``(name, first-line summary)`` pairs
    sorted by name — the deterministic index shape the content channel
    renders.
    """

    root: Path

    def path_for(self, name: str) -> Path:
        if not _is_valid_name(name):
            raise ValueError(f"invalid memory name {name!r}")
        return self.root / f"{name}{MEMORY_FILE_SUFFIX}"

    def write(self, name: str, text: str) -> Path:
        path = self.path_for(name)
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read(self, name: str) -> Optional[str]:
        if not _is_valid_name(name):
            return None
        path = self.root / f"{name}{MEMORY_FILE_SUFFIX}"
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def entries(self) -> tuple[tuple[str, str], ...]:
        try:
            paths = sorted(self.root.glob(f"*{MEMORY_FILE_SUFFIX}"))
        except OSError:
            return ()
        out: list[tuple[str, str]] = []
        for path in paths:
            name = path.name[: -len(MEMORY_FILE_SUFFIX)]
            if not _is_valid_name(name) or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.append((name, _first_line_summary(text)))
        return tuple(out)


def _err(tool_name: str, message: str) -> ToolResult:
    return ToolResult(success=False, summary=f"{tool_name}: {message}")


@dataclass
class MemoryWriteTool:
    """Persist one memory as a file — writing memories is routine.

    ``risk_level="medium"``: it mutates durable cross-session state, but
    only inside the slug-confined memory directory (never the workspace),
    so it does not rank with arbitrary fs writes.
    """

    store: MemoryStore
    name: str = MEMORY_WRITE_TOOL_NAME
    description: str = (
        "Persist a named memory as a markdown file (reusing a name overwrites "
        "it). Names must be slugs (letters, digits, '.', '_', '-'; no path "
        "separators; max 128 chars). The first non-empty body line becomes the "
        "index summary. Writes are confined to the memory directory."
    )
    risk_level: str = "medium"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
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
                        "Full memory body (markdown). The first line "
                        "becomes the index summary."
                    ),
                },
            },
            "required": ["name", "text"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = arguments.get("name")
        text = arguments.get("text")
        if not _is_valid_name(name):
            return _err(self.name, f"invalid memory name {name!r}")
        if not isinstance(text, str) or not text:
            return _err(self.name, "requires non-empty string 'text'")
        try:
            self.store.write(name, text)  # type: ignore[arg-type]
        except (OSError, ValueError) as exc:
            return _err(self.name, f"write failed: {exc}")
        return ToolResult(
            success=True,
            output={"name": name, "bytes": len(text.encode("utf-8"))},
            summary=f"{self.name}: stored {name!r}",
        )


@dataclass
class MemoryReadTool:
    """Load one memory's full text on demand.

    The result rides the ordinary tool-result channel; an oversized body
    is bounded to the inline budget (``truncated=True`` tells the model
    the file on disk holds more).
    """

    store: MemoryStore
    name: str = MEMORY_READ_TOOL_NAME
    description: str = (
        "Load the full markdown body of a named memory on demand. If the body "
        "exceeds the inline byte budget, truncated=True is returned with only a "
        "prefix inline."
    )
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Memory name as listed in the memory index.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = arguments.get("name")
        if not _is_valid_name(name):
            return _err(self.name, f"invalid memory name {name!r}")
        text = self.store.read(name)  # type: ignore[arg-type]
        if text is None:
            return _err(self.name, f"no memory named {name!r}")
        truncated = len(text.encode("utf-8")) > INLINE_CONTENT_MAX_BYTES
        if truncated:
            text = truncate_bytes(text, INLINE_CONTENT_MAX_BYTES)
        return ToolResult(
            success=True,
            output={"name": name, "text": text, "truncated": truncated},
            summary=f"{self.name}: loaded {name!r}",
        )


def build_memory_tools(store: MemoryStore) -> dict[str, Tool]:
    """The memory tool pack — mirrors ``build_fs_tools``' dict shape."""
    return {
        MEMORY_WRITE_TOOL_NAME: MemoryWriteTool(store=store),
        MEMORY_READ_TOOL_NAME: MemoryReadTool(store=store),
    }
