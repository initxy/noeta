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
  (sorted) index source consumed by ``noeta.context.memory``;
  ``search()`` / ``archive()`` are the store halves of the v2 tools.
* :class:`MemoryWriteTool` / :class:`MemoryReadTool` /
  :class:`MemorySearchTool` / :class:`MemoryArchiveTool` — the same
  dataclass shape as the fs tool pack.

Memory files may open with an optional frontmatter fence (``---`` lines
around ``key: value`` pairs — parsed by a minimal in-module parser, NO
yaml dependency). ``description`` overrides the first-line index summary
and ``type`` tags the entry; a file without (or with a malformed) fence
keeps the v1 first-line behavior byte-for-byte.

Layering note: this module deliberately knows nothing about the content
channel — ``noeta.tools`` and ``noeta.context`` are independent siblings,
so the store hands over plain ``(name, summary, type)`` tuples and the
glue lives one band up in ``noeta.execution.memory``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools._limits import INLINE_CONTENT_MAX_BYTES, truncate_bytes


__all__ = [
    "MEMORY_ARCHIVE_TOOL_NAME",
    "MEMORY_FILE_SUFFIX",
    "MEMORY_READ_TOOL_NAME",
    "MEMORY_SEARCH_TOOL_NAME",
    "MEMORY_TYPES",
    "MEMORY_WRITE_TOOL_NAME",
    "MemoryArchiveTool",
    "MemoryReadTool",
    "MemorySearchTool",
    "MemoryStore",
    "MemoryWriteTool",
    "build_memory_tools",
]


MEMORY_WRITE_TOOL_NAME = "memory_write"
MEMORY_READ_TOOL_NAME = "memory_read"
MEMORY_SEARCH_TOOL_NAME = "memory_search"
MEMORY_ARCHIVE_TOOL_NAME = "memory_archive"
MEMORY_FILE_SUFFIX = ".md"

#: The frontmatter ``type`` vocabulary; anything else is treated as absent.
MEMORY_TYPES = ("user", "project", "procedural", "reference")

#: Strict slug: starts alphanumeric, then alphanumerics / ``.`` / ``_`` /
#: ``-`` only — no path separators, no leading dot, bounded length. A
#: valid name can never traverse out of the memory directory.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Summary display cap for index entries (frontmatter description or
#: first non-empty body line).
_SUMMARY_MAX_CHARS = 200

#: The frontmatter fence line.
_FENCE = "---"

#: Archived memories live in this subdirectory of the store root; the
#: non-recursive ``*.md`` globs of ``entries()`` / ``search()`` never
#: descend into it, so archiving is invisible to index, recall and search.
_ARCHIVE_DIR_NAME = "archive"

#: ``search()`` output caps: memories per query / excerpt lines per
#: memory / chars per excerpt line.
_SEARCH_MAX_MEMORIES = 10
_SEARCH_MAX_LINES = 3
_SEARCH_LINE_MAX_CHARS = 200


def _is_valid_name(name: object) -> bool:
    return isinstance(name, str) and _NAME_RE.match(name) is not None


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split an optional leading frontmatter fence off ``text``.

    Minimal by design (no yaml dependency): the block is a leading
    ``---`` line, ``key: value`` lines, and a closing ``---`` line.
    A malformed block (unclosed fence, a line without a ``key:``)
    degrades to "no frontmatter" — the whole text is the body, exactly
    the v1 reading. Unknown keys are kept here; callers pick what they
    recognize. The body is returned byte-exact (``keepends`` slicing),
    so stripping a fence never mutates the content after it.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FENCE:
        return {}, text
    fields: dict[str, str] = {}
    consumed = len(lines[0])
    for line in lines[1:]:
        consumed += len(line)
        if line.strip() == _FENCE:
            return fields, text[consumed:]
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            return {}, text
        fields[key.strip()] = value.strip()
    return {}, text


def _first_line_summary(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:_SUMMARY_MAX_CHARS]
    return ""


def _entry_summary_and_type(text: str) -> tuple[str, str]:
    """The index fields of one memory file: ``(summary, type)``.

    Frontmatter ``description`` wins the summary; otherwise the first
    non-empty BODY line (the fence never leaks into the fallback). An
    unrecognized ``type`` value is treated as absent (``""``)."""
    fields, body = _split_frontmatter(text)
    mem_type = fields.get("type", "")
    if mem_type not in MEMORY_TYPES:
        mem_type = ""
    description = fields.get("description", "")
    summary = (
        description[:_SUMMARY_MAX_CHARS]
        if description
        else _first_line_summary(body)
    )
    return summary, mem_type


@dataclass(frozen=True)
class MemoryStore:
    """File-per-memory store rooted at one directory.

    A missing root is a valid empty store (a workspace without memories
    configures nothing and pays nothing). ``write`` creates the root on
    first use; ``entries()`` lists ``(name, summary, type)`` triples
    sorted by name — the deterministic index shape the content channel
    renders. ``search()`` grep-scans the same top-level files;
    ``archive()`` retires one into ``archive/`` (never deletes).
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

    def entries(self) -> tuple[tuple[str, str, str], ...]:
        out: list[tuple[str, str, str]] = []
        for name, text in self._iter_memories():
            summary, mem_type = _entry_summary_and_type(text)
            out.append((name, summary, mem_type))
        return tuple(out)

    def search(self, query: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Case-insensitive plain-substring search over names and full text.

        Deterministic grep semantics, no regex: per matching memory up
        to :data:`_SEARCH_MAX_LINES` matching lines (trailing whitespace
        stripped, each capped at :data:`_SEARCH_LINE_MAX_CHARS` chars),
        at most :data:`_SEARCH_MAX_MEMORIES` memories, name-sorted. A
        name-only hit carries an empty excerpt. Top-level files only —
        ``archive/`` is naturally out of scope.
        """
        if not query:
            return ()
        needle = query.lower()
        out: list[tuple[str, tuple[str, ...]]] = []
        for name, text in self._iter_memories():
            lines: list[str] = []
            for line in text.splitlines():
                if needle in line.lower():
                    lines.append(line.rstrip()[:_SEARCH_LINE_MAX_CHARS])
                    if len(lines) >= _SEARCH_MAX_LINES:
                        break
            if lines or needle in name.lower():
                out.append((name, tuple(lines)))
                if len(out) >= _SEARCH_MAX_MEMORIES:
                    break
        return tuple(out)

    def archive(self, name: str) -> Optional[Path]:
        """Move ``<name>.md`` into ``archive/`` — retire, never delete.

        Returns the destination path, or ``None`` when the name is
        invalid or no such memory exists. A destination collision (the
        name was archived before) gets a ``-2`` / ``-3`` / … suffix so
        no archived copy is ever overwritten.
        """
        if not _is_valid_name(name):
            return None
        src = self.root / f"{name}{MEMORY_FILE_SUFFIX}"
        if not src.is_file():
            return None
        archive_dir = self.root / _ARCHIVE_DIR_NAME
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / f"{name}{MEMORY_FILE_SUFFIX}"
        counter = 2
        while dest.exists():
            dest = archive_dir / f"{name}-{counter}{MEMORY_FILE_SUFFIX}"
            counter += 1
        src.rename(dest)
        return dest

    def _iter_memories(self) -> list[tuple[str, str]]:
        """Readable top-level memories as ``(name, text)``, name-sorted.

        The shared walk of ``entries()`` / ``search()``: non-recursive
        (subdirectories like ``archive/`` never surface), invalid slugs
        and unreadable files are skipped rather than crashing."""
        try:
            paths = sorted(self.root.glob(f"*{MEMORY_FILE_SUFFIX}"))
        except OSError:
            return []
        out: list[tuple[str, str]] = []
        for path in paths:
            name = path.name[: -len(MEMORY_FILE_SUFFIX)]
            if not _is_valid_name(name) or not path.is_file():
                continue
            try:
                out.append((name, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
        return out


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
        "separators; max 128 chars). Optional 'description' (a one-line "
        "summary shown in the memory index) and 'type' (one of: user, "
        "project, procedural, reference) are stored as frontmatter — pass "
        "them as parameters instead of writing a frontmatter block yourself. "
        "Without a description the first non-empty body line becomes the "
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
                        "Full memory body (markdown). Without a "
                        "'description' parameter, the first line becomes "
                        "the index summary."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Optional one-line summary shown in the memory "
                        "index instead of the first body line."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": list(MEMORY_TYPES),
                    "description": (
                        "Optional memory category shown in the index."
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
        description = arguments.get("description")
        mem_type = arguments.get("type")
        if description is not None:
            # One line means one ``splitlines`` line — the same rule the
            # frontmatter parser applies on read-back.
            if not isinstance(description, str) or len(
                description.splitlines()
            ) > 1:
                return _err(
                    self.name, "'description' must be a one-line string"
                )
        if mem_type is not None and mem_type not in MEMORY_TYPES:
            return _err(
                self.name,
                f"invalid 'type' {mem_type!r} (one of: "
                f"{', '.join(MEMORY_TYPES)})",
            )
        if description or mem_type:
            # The params win: the tool composes the frontmatter block,
            # replacing any fence the text itself carries. Without params
            # a text-carried fence is accepted as-is.
            _, body = _split_frontmatter(text)
            fence = [_FENCE]
            if description:
                fence.append(
                    f"description: {description[:_SUMMARY_MAX_CHARS]}"
                )
            if mem_type:
                fence.append(f"type: {mem_type}")
            fence.append(_FENCE)
            text = "\n".join(fence) + "\n" + body
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


@dataclass
class MemorySearchTool:
    """Grep-style content search over the store — read-only, bounded.

    ``risk_level="low"``: pure disk reads inside the memory directory;
    zero hits is a successful (empty) result, not an error.
    """

    store: MemoryStore
    name: str = MEMORY_SEARCH_TOOL_NAME
    description: str = (
        "Find stored memories by content: case-insensitive substring match "
        "(no regex) over memory names and full text. Returns up to 10 "
        "memories with up to 3 matching lines each; use memory_read for a "
        "hit's full text. Archived memories are not searched."
    )
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Plain substring to look for (case-insensitive)."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            return _err(self.name, "requires non-empty string 'query'")
        results = [
            {"name": name, "lines": list(lines)}
            for name, lines in self.store.search(query)
        ]
        return ToolResult(
            success=True,
            output={"query": query, "results": results},
            summary=f"{self.name}: {len(results)} hit(s)",
        )


@dataclass
class MemoryArchiveTool:
    """Retire one memory into ``archive/`` — reversible, never deletes.

    ``risk_level="medium"`` like the write tool: it mutates durable
    cross-session state, but only inside the slug-confined memory
    directory, and the move is reversible by a human.
    """

    store: MemoryStore
    name: str = MEMORY_ARCHIVE_TOOL_NAME
    description: str = (
        "Retire an outdated or superseded memory: move it into the memory "
        "directory's archive/ subdirectory, removing it from the index, "
        "recall and search. Nothing is ever deleted — a human can restore "
        "the file from archive/."
    )
    risk_level: str = "medium"
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
        try:
            dest = self.store.archive(name)  # type: ignore[arg-type]
        except OSError as exc:
            return _err(self.name, f"archive failed: {exc}")
        if dest is None:
            return _err(self.name, f"no memory named {name!r}")
        archived_to = str(dest.relative_to(self.store.root))
        return ToolResult(
            success=True,
            output={"name": name, "archived_to": archived_to},
            summary=f"{self.name}: archived {name!r}",
        )


def build_memory_tools(store: MemoryStore) -> dict[str, Tool]:
    """The memory tool pack — mirrors ``build_fs_tools``' dict shape."""
    return {
        MEMORY_WRITE_TOOL_NAME: MemoryWriteTool(store=store),
        MEMORY_READ_TOOL_NAME: MemoryReadTool(store=store),
        MEMORY_SEARCH_TOOL_NAME: MemorySearchTool(store=store),
        MEMORY_ARCHIVE_TOOL_NAME: MemoryArchiveTool(store=store),
    }
