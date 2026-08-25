"""Memory v1 tools — writing a memory / reading its full text on demand are ORDINARY tools.

One file per memory under a single host-chosen directory (a fixed global
dir, default :data:`DEFAULT_GLOBAL_MEMORY_DIR` below; never per-session
workspace).
Writing a memory is a routine act — the whole point of the ``evolving``
drift policy its index recordings carry — so these are plain SDK tools
with zero new mechanisms: results travel the ordinary tool-result
channel, the runtime is untouched.

* :class:`MemoryStore` — the file-per-memory store. Names are strict
  slugs (no separators, no leading dot), so a model-supplied name can
  never escape the memory directory. ``entries()`` is the deterministic
  (sorted) index source the index renderer consumes;
  ``search()`` / ``archive()`` are the store halves of the v2 tools.
* :class:`MemoryWriteTool` / :class:`MemoryReadTool` /
  :class:`MemorySearchTool` / :class:`MemoryArchiveTool` — the same
  dataclass shape as the fs tool pack.

Memory files may open with an optional frontmatter fence (``---`` lines
around ``key: value`` pairs — parsed by a minimal in-module parser, NO
yaml dependency). ``description`` overrides the first-line index summary,
``type`` tags the entry, and ``keywords`` carries comma-separated retrieval
aliases (matcher-only — the cross-lingual recall surface); a file without
(or with a malformed) fence keeps the v1 first-line behavior byte-for-byte.
``memory_write`` additionally stamps ``created`` / ``updated`` dates and a
``source_task`` ledger receipt, so every tool-written memory records when
it was true and which task's history backs it. A rewrite merges per-field
over the fence already on disk: fields the new text and parameters do not
mention survive, and a key written with an empty value is dropped.

Layering note: this module deliberately knows nothing about the content
channel — the store hands over plain ``(name, summary, type, keywords)``
tuples; the pure index pieces live beside it in
``noeta.builtins.memory.impl.index`` and the recall glue in
``noeta.builtins.memory.impl.recall``. The one sibling it imports is
``matching`` (pure token primitives, no channel deps) so the write tool's
near-duplicate check speaks the exact recall vocabulary.
"""

from __future__ import annotations

import datetime
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from noeta.builtins.memory.impl.matching import (
    SUMMARY_MIN_OVERLAP,
    match_tokens,
)
from noeta.protocols.resources import load_markdown
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools.limits import INLINE_CONTENT_MAX_BYTES, truncate_bytes


__all__ = [
    "DEFAULT_GLOBAL_MEMORY_DIR",
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
    "load_memory_store",
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

#: Tool-composed fence keys, in the order they are written. Unknown keys a
#: memory already carries (on disk, or in the text's own fence) are preserved
#: after these, sorted — the write tool merges per-field and never drops data
#: it does not recognize; only an explicit empty value removes a key.
_FENCE_KEY_ORDER = (
    "description",
    "type",
    "keywords",
    "created",
    "updated",
    "source_task",
)

#: Near-duplicate note cap — the write result names at most this many
#: similar existing memories.
_SIMILAR_MAX = 3

#: Archived memories live in this subdirectory of the store root; the
#: non-recursive ``*.md`` globs of ``entries()`` / ``search()`` never
#: descend into it, so archiving is invisible to index, recall and search.
_ARCHIVE_DIR_NAME = "archive"

#: ``search()`` per-memory excerpt caps: lines per memory / chars per line.
#: The memory-count cap lives on the TOOL (:data:`_SEARCH_MAX_MEMORIES`) so
#: it can report the trim (``truncated``) instead of silently dropping hits.
_SEARCH_MAX_LINES = 3
_SEARCH_LINE_MAX_CHARS = 200

#: Memories per ``memory_search`` result — applied (and reported) by
#: :class:`MemorySearchTool`, not by :meth:`MemoryStore.search`.
_SEARCH_MAX_MEMORIES = 10


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


def _entry_fields(text: str) -> tuple[str, str, str]:
    """The entry fields of one memory file: ``(summary, type, keywords)``.

    Frontmatter ``description`` wins the summary; otherwise the first
    non-empty BODY line (the fence never leaks into the fallback). An
    unrecognized ``type`` value is treated as absent (``""``). ``keywords``
    is passed through raw — it feeds the matcher, never the rendered
    index."""
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
    return summary, mem_type, fields.get("keywords", "")


def _compose_frontmatter(fields: dict[str, str]) -> str:
    """Render a fence block: known keys in fixed order, the rest sorted.

    Deterministic on purpose — the same fields always produce the same
    bytes, so a rewrite that changes nothing moves nothing. Empty values
    are omitted rather than written as blank lines."""
    keys = [k for k in _FENCE_KEY_ORDER if fields.get(k)]
    keys += sorted(
        k for k in fields if k not in _FENCE_KEY_ORDER and fields[k]
    )
    lines = [_FENCE, *(f"{k}: {fields[k]}" for k in keys), _FENCE]
    return "\n".join(lines) + "\n"


def _today() -> str:
    """Today as ``YYYY-MM-DD`` — module-level so tests can pin it."""
    return datetime.date.today().isoformat()


@dataclass(frozen=True)
class MemoryStore:
    """File-per-memory store rooted at one directory.

    A missing root is a valid empty store (a workspace without memories
    configures nothing and pays nothing). ``write`` creates the root on
    first use; ``entries()`` lists ``(name, summary, type, keywords)``
    quadruples sorted by name — the deterministic shape the content
    channel renders (keywords excluded) and the recall matcher consumes.
    ``search()`` grep-scans the same top-level files; ``archive()``
    retires one into ``archive/`` (never deletes).
    """

    root: Path

    def path_for(self, name: str) -> Path:
        if not _is_valid_name(name):
            raise ValueError(f"invalid memory name {name!r}")
        return self.root / f"{name}{MEMORY_FILE_SUFFIX}"

    def write(self, name: str, text: str) -> Path:
        """Replace ``<name>.md`` atomically — write a sibling temp, then rename.

        A plain ``write_text`` truncates first, so a reader that lands in the
        window (``entries()`` on every session build, ``search()``, recall on
        every turn intake) sees a half file and the model reads a memory that
        never existed. The consolidation agent curates the same store a live
        session writes, so that window is real, not theoretical. ``os.replace``
        makes the swap atomic on POSIX and Windows alike; the temp file is a
        dot-name outside the ``*.md`` glob, so a crash mid-write can leave
        litter but can never leave a visible broken memory. The ADR's accepted
        weakness stays exactly what it was — last writer wins — with torn
        reads no longer part of it.

        Permissions: ``mkstemp`` fixes the temp file at 0o600 (umask ignored)
        and ``os.replace`` carries that mode onto the destination — a
        deliberate tightening over ``write_text``'s umask default; memory
        notes are single-user data.
        """
        path = self.path_for(name)
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.root, prefix=f".{name}.", suffix=".tmp"
        )
        try:
            # Text mode with the default newline handling, so the bytes on
            # disk are identical to what ``write_text`` produced.
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return path

    def read(self, name: str) -> Optional[str]:
        if not _is_valid_name(name):
            return None
        path = self.root / f"{name}{MEMORY_FILE_SUFFIX}"
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def entries(self) -> tuple[tuple[str, str, str, str], ...]:
        out: list[tuple[str, str, str, str]] = []
        for name, text in self._iter_memories():
            summary, mem_type, keywords = _entry_fields(text)
            out.append((name, summary, mem_type, keywords))
        return tuple(out)

    def search(self, query: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Case-insensitive plain-substring search over names and full text.

        Deterministic grep semantics, no regex: EVERY matching memory,
        name-sorted, each with up to :data:`_SEARCH_MAX_LINES` matching
        lines (trailing whitespace stripped, capped at
        :data:`_SEARCH_LINE_MAX_CHARS` chars). A name-only hit carries an
        empty excerpt. The memory-count cap is presentation, so it lives on
        :class:`MemorySearchTool` — which reports the trim instead of
        dropping hits silently. Top-level files only — ``archive/`` is
        naturally out of scope.
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

    Every write stamps ``created`` / ``updated`` dates and (when the
    runtime threads one) a ``source_task`` ledger receipt into the
    frontmatter, and a write under a NEW name reports existing memories
    it looks like — see ``invoke`` for why each lives on the write path.
    """

    store: MemoryStore
    name: str = MEMORY_WRITE_TOOL_NAME
    description: str = field(
        default=load_markdown(__package__, "memory_write")
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
                "keywords": {
                    "type": "string",
                    "description": (
                        "Optional comma-separated retrieval aliases — "
                        "synonyms and cross-language equivalents (e.g. "
                        "both English and Chinese terms) that should "
                        "auto-recall this memory."
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
        keywords = arguments.get("keywords")
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
        if keywords is not None and (
            not isinstance(keywords, str)
            or len(keywords.splitlines()) > 1
        ):
            return _err(self.name, "'keywords' must be a one-line string")

        # Per-field merge, lowest to highest: the fence already on disk,
        # then the text's own fence, then the params — each layer
        # overrides only the fields it names, so a body-only rewrite (the
        # common case: the tool description tells the model NOT to write
        # a fence, and a curator's ``keywords`` live only on disk) keeps
        # every field it does not mention, known or not. The only way to
        # remove a field is naming it with an empty value; the composer
        # omits empties. Merging from the text's fence alone looked like
        # this protection but guarded nothing: the model it protected
        # against never sends a fence.
        prior = self.store.read(name)  # type: ignore[arg-type]
        prior_fields = (
            _split_frontmatter(prior)[0] if prior is not None else {}
        )
        text_fields, body = _split_frontmatter(text)
        fields = {**prior_fields, **text_fields}
        if description:
            fields["description"] = description[:_SUMMARY_MAX_CHARS]
        if mem_type:
            fields["type"] = mem_type
        if keywords:
            fields["keywords"] = keywords

        # Timestamps are the tool's, not the model's: ``created`` sticks
        # to the value the memory already holds (on disk first — the text
        # a model resends often predates the file), ``updated`` always
        # moves. Staleness judgement needs dates no one remembered to ask
        # for, so the tool stamps them unconditionally.
        today = _today()
        fields["created"] = (
            prior_fields.get("created") or fields.get("created") or today
        )
        fields["updated"] = today

        # The ledger receipt: which task's history backs this note. Turns
        # a memory from an unverifiable assertion into a pointer INTO the
        # ledger — a doubted memory can be checked against its source
        # session instead of trusted or discarded.
        task_id = ctx.metadata.get("task_id")
        if isinstance(task_id, str) and task_id:
            fields["source_task"] = task_id

        # Near-duplicate probe, NEW names only (rewriting a memory is the
        # cure, not the disease). Symmetric with recall: would this new
        # entry's name-or-summary line recall an existing one? Advisory —
        # the write always proceeds; the note rides the result so the
        # model can merge while the context that caused the write is
        # still live, which a background pass never sees.
        similar: list[str] = []
        if prior is None:
            probe = match_tokens(
                f"{name} {fields.get('description') or _first_line_summary(body)}"
            )
            for other_name, other_summary, _t, _kw in self.store.entries():
                if match_tokens(other_name) & probe or len(
                    match_tokens(other_summary) & probe
                ) >= SUMMARY_MIN_OVERLAP:
                    similar.append(other_name)

        text = _compose_frontmatter(fields) + body
        try:
            self.store.write(name, text)  # type: ignore[arg-type]
        except (OSError, ValueError) as exc:
            return _err(self.name, f"write failed: {exc}")
        output: dict[str, Any] = {
            "name": name,
            "bytes": len(text.encode("utf-8")),
        }
        summary = f"{self.name}: stored {name!r}"
        if similar:
            shown = similar[:_SIMILAR_MAX]
            output["similar"] = shown
            summary += (
                f" — similar existing memor"
                f"{'y' if len(shown) == 1 else 'ies'}: "
                f"{', '.join(shown)}; consider updating instead of "
                f"duplicating"
            )
        return ToolResult(success=True, output=output, summary=summary)


@dataclass
class MemoryReadTool:
    """Load one memory's full text on demand.

    The result rides the ordinary tool-result channel; an oversized body
    is bounded to the inline budget (``truncated=True`` tells the model
    the file on disk holds more).
    """

    store: MemoryStore
    name: str = MEMORY_READ_TOOL_NAME
    description: str = field(
        default=load_markdown(__package__, "memory_read")
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
    description: str = field(
        default=load_markdown(__package__, "memory_search")
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
        hits = self.store.search(query)
        truncated = len(hits) > _SEARCH_MAX_MEMORIES
        results = [
            {"name": name, "lines": list(lines)}
            for name, lines in hits[:_SEARCH_MAX_MEMORIES]
        ]
        summary = f"{self.name}: {len(results)} hit(s)"
        if truncated:
            summary = (
                f"{self.name}: {len(hits)} hit(s), first "
                f"{_SEARCH_MAX_MEMORIES} shown"
            )
        return ToolResult(
            success=True,
            output={"query": query, "results": results, "truncated": truncated},
            summary=summary,
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
    description: str = field(
        default=load_markdown(__package__, "memory_archive")
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


#: Memory is pinned to ONE global directory (never per-session
#: workspace), so memories survive a workspace switch and stay cross-scenario.
#: The agent layer configures the root and falls back to this default
#: (``~/.noeta/memories``) when nothing is set; ``expanduser`` resolves ``~``
#: against the running user's home. Consumers read this LATE off the module
#: (never from-import it) so a test can pin it hermetically.
DEFAULT_GLOBAL_MEMORY_DIR: Path = Path("~/.noeta/memories").expanduser()


def load_memory_store(*, root: Path) -> MemoryStore:
    """Build the global :class:`MemoryStore` at ``root``.

    ``root`` is the **fixed global** memory directory the agent layer
    supplies (default :data:`DEFAULT_GLOBAL_MEMORY_DIR`) — independent of
    the per-session workspace, so reads / writes land in one
    place regardless of which workspace the turn runs in. A missing
    directory is a valid empty store — an unconfigured global dir pays
    nothing (``entries() == ()`` keeps every default flow byte-identical).
    """
    return MemoryStore(root=root)
