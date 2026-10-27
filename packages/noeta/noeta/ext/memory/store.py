"""The memory store: one markdown file per memory in a host-chosen
directory (never the workspace), an optional ``---`` frontmatter fence,
``archive/`` instead of deletion.

Names are strict slugs (Unicode letters and digits, ``.``, ``_``, ``-``), so a
model-supplied name can never leave the directory. Frontmatter:
``description`` overrides the first-line index summary, ``type`` tags the
entry, ``keywords`` are recall aliases (matcher-only), ``related`` lists the
pages recall follows one hop. ``memory_write`` stamps ``created`` /
``updated`` and ``source_task``, and merges per field over what is on disk:
absent keeps, empty removes, and a text that is only a fence keeps the body.

Synchronous on purpose; the tools and the extension call it through
``asyncio.to_thread``.
"""

from __future__ import annotations

import datetime
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from noeta.ext.memory.matching import MemoryEntries, rank_memories

__all__ = [
    "DEFAULT_GLOBAL_MEMORY_DIR",
    "MEMORY_TYPES",
    "MemoryStore",
    "WriteResult",
    "first_line_summary",
    "is_valid_name",
    "split_frontmatter",
    "write_memory",
]

MEMORY_FILE_SUFFIX = ".md"
#: The frontmatter ``type`` vocabulary; anything else is treated as absent.
MEMORY_TYPES = ("user", "project", "procedural", "reference")
#: The default store root when the host names none.
DEFAULT_GLOBAL_MEMORY_DIR = Path("~/.noeta/memories").expanduser()

_NAME_RE = re.compile(r"[^\W_][\w.-]{0,127}\Z")
_NAME_MAX_BYTES = 240
_RELATED_SEP_RE = re.compile(r"[,，、;；]")  # noqa: RUF001
_SUMMARY_MAX_CHARS = 200
_FENCE = "---"
_FIELD_KEY_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
_FENCE_KEY_ORDER = (
    "description", "type", "keywords", "related", "created", "updated", "source_task",
)
_SIMILAR_MAX = 3
_ARCHIVE_DIR_NAME = "archive"
_SEARCH_MAX_LINES = 3
_SEARCH_LINE_MAX_CHARS = 200


def is_valid_name(name: object) -> bool:
    return (isinstance(name, str) and _NAME_RE.match(name) is not None
            and len(name.encode("utf-8")) <= _NAME_MAX_BYTES)


def _related_names(value: str) -> tuple[str, ...]:
    out: list[str] = []
    for raw in _RELATED_SEP_RE.split(value):
        name = raw.strip().strip("[]'\"").strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """``(fields, body)``; a malformed fence is no fence (the whole text is
    the body). The body is returned byte-exact."""
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


def first_line_summary(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:_SUMMARY_MAX_CHARS]
    return ""


def _entry_fields_of(fields: dict[str, str], body: str) -> tuple[str, str, str]:
    mem_type = fields.get("type", "")
    if mem_type not in MEMORY_TYPES:
        mem_type = ""
    description = fields.get("description", "")
    summary = description[:_SUMMARY_MAX_CHARS] if description else first_line_summary(body)
    return summary, mem_type, fields.get("keywords", "")


def _compose_frontmatter(fields: dict[str, str]) -> str:
    keys = [k for k in _FENCE_KEY_ORDER if fields.get(k)]
    keys += sorted(k for k in fields if k not in _FENCE_KEY_ORDER and fields[k])
    return "\n".join([_FENCE, *(f"{k}: {fields[k]}" for k in keys), _FENCE]) + "\n"


def _today() -> str:
    """Today as ``YYYY-MM-DD`` — module-level so tests can pin it."""
    return datetime.date.today().isoformat()


@dataclass(frozen=True)
class MemoryStore:
    """File-per-memory store; a missing root is a valid empty store."""

    root: Path

    def path_for(self, name: str) -> Path:
        if not is_valid_name(name):
            raise ValueError(f"invalid memory name {name!r}")
        return self.root / f"{name}{MEMORY_FILE_SUFFIX}"

    def write(self, name: str, text: str) -> Path:
        """Replace ``<name>.md`` atomically (temp file + rename, mode 0600)."""
        path = self.path_for(name)
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return path

    def read(self, name: str) -> str | None:
        if not is_valid_name(name):
            return None
        try:
            return (self.root / f"{name}{MEMORY_FILE_SUFFIX}").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def entries(self) -> MemoryEntries:
        return self.index_snapshot()[0]

    def index_snapshot(self) -> tuple[MemoryEntries, dict[str, str]]:
        """``(entries, {name: updated date})`` from one walk; entries are
        ``(name, summary, type, keywords)``, name-sorted."""
        out: list[tuple[str, str, str, str]] = []
        updated: dict[str, str] = {}
        for name, text in self._iter_memories():
            fields, body = split_frontmatter(text)
            summary, mem_type, keywords = _entry_fields_of(fields, body)
            out.append((name, summary, mem_type, keywords))
            updated[name] = fields.get("updated", "")
        return tuple(out), updated

    def related(self, name: str) -> tuple[str, ...]:
        text = self.read(name)
        if text is None:
            return ()
        return _related_names(split_frontmatter(text)[0].get("related", ""))

    def search(self, query: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Case-insensitive substring search over names and text, every
        match name-sorted, at most 3 matching lines each."""
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

    def archive(self, name: str) -> Path | None:
        """Move ``<name>.md`` into ``archive/`` (``-2``, ``-3``... on a
        collision). ``None`` when no such memory exists."""
        if not is_valid_name(name):
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
        try:
            paths = sorted(self.root.glob(f"*{MEMORY_FILE_SUFFIX}"))
        except OSError:
            return []
        out: list[tuple[str, str]] = []
        for path in paths:
            name = path.name[: -len(MEMORY_FILE_SUFFIX)]
            if not is_valid_name(name) or not path.is_file():
                continue
            try:
                out.append((name, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
        return out


@dataclass(frozen=True)
class WriteResult:
    """``error`` (the failure text) or ``output`` (the success payload)."""

    output: dict[str, Any] | None = None
    error: str | None = None


def write_memory(store: MemoryStore, arguments: dict[str, Any], *, task_id: str = "",
                 max_bytes: int | None = None, tool: str = "memory_write") -> WriteResult:
    """``memory_write``: validate, merge per field (disk < the text's own
    fence < parameters), stamp dates and the source task, cap, write."""

    def err(message: str) -> WriteResult:
        return WriteResult(error=f"{tool}: {message}")

    name = arguments.get("name")
    text = arguments.get("text")
    if not is_valid_name(name):
        return err(f"invalid memory name {name!r}")
    assert isinstance(name, str)
    if not isinstance(text, str) or not text:
        return err("requires non-empty string 'text'")
    description = arguments.get("description")
    mem_type = arguments.get("type")
    keywords = arguments.get("keywords")
    related = arguments.get("related")
    if description is not None and (
            not isinstance(description, str) or len(description.splitlines()) > 1):
        return err("'description' must be a one-line string; \"\" removes it")
    if mem_type is not None and mem_type not in (*MEMORY_TYPES, ""):
        return err(f"invalid 'type' {mem_type!r} (one of: {', '.join(MEMORY_TYPES)}; "
                   "\"\" removes it)")
    if keywords is not None and (not isinstance(keywords, str)
                                 or len(keywords.splitlines()) > 1):
        return err("'keywords' must be a one-line comma-separated string; \"\" removes them")
    if related is not None:
        if not isinstance(related, (list, tuple)):
            return err("'related' must be a list of memory names; [] removes them")
        invalid = [item for item in related if not is_valid_name(item)]
        if invalid:
            return err(f"invalid 'related' memory name {invalid[0]!r}: use the "
                       f"names the memory index lists")

    prior = store.read(name)
    prior_fields = split_frontmatter(prior)[0] if prior is not None else {}
    text_fields, body = split_frontmatter(text)
    if not all(key in _FENCE_KEY_ORDER or key in prior_fields or _FIELD_KEY_RE.match(key)
               for key in text_fields):
        text_fields, body = {}, text
    if not body.strip():
        if prior is None:
            return err("a new memory needs a body: 'text' carried only a frontmatter fence, "
                       "so nothing was written — send the note itself as 'text'")
        body = split_frontmatter(prior)[1]
        kept_body = True
    else:
        kept_body = False
    fields = {**prior_fields, **text_fields}
    if description is not None:
        fields["description"] = description[:_SUMMARY_MAX_CHARS]
    if mem_type is not None:
        fields["type"] = mem_type
    if keywords is not None:
        fields["keywords"] = keywords
    if related is not None:
        fields["related"] = ", ".join(dict.fromkeys(related))
    today = _today()
    fields["created"] = prior_fields.get("created") or today
    fields["updated"] = today
    if task_id:
        fields["source_task"] = task_id

    similar: list[str] = []
    if prior is None:
        probe = f"{name} {fields.get('description') or first_line_summary(body)}"
        similar = [hit.name for hit in rank_memories(store.entries(), probe)]

    page = _compose_frontmatter(fields) + body
    if max_bytes is not None:
        size = len(page.encode("utf-8"))
        if size > max_bytes:
            return err(f"page is {size} bytes with its fields, over this store's cap of "
                       f"{max_bytes}: tighten it, or split it into two memories")
    try:
        store.write(name, page)
    except (OSError, ValueError) as exc:
        return err(f"write failed: {exc}")
    output: dict[str, Any] = {"name": name, "bytes": len(page.encode("utf-8"))}
    if similar:
        shown = similar[:_SIMILAR_MAX]
        output["similar"] = shown
        output["note"] = (
            f"Similar existing memor{'y' if len(shown) == 1 else 'ies'}: "
            f"{', '.join(shown)}. Update one of those rather than keeping a near-duplicate."
        )
    elif kept_body:
        output["note"] = (
            "'text' carried only a frontmatter fence, so the fields "
            "were updated and the existing body kept. To replace the "
            "body, send it as 'text'."
        )
    return WriteResult(output=output)
