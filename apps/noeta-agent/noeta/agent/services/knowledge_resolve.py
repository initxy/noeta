"""Knowledge citation path resolution (citations resolve).

Batch-resolves `knowledge/<source name>/<relative path>[#<heading anchor>]`
references appearing in a session into structured citations the frontend can
render: existence verification, frontmatter metadata (title / origin-url),
and excerpt slicing by heading anchor. Read-only derivation, never persisted —
the message text in the EventLog is the single source of truth; this module
recomputes on every call, so excerpts follow the knowledge source as it
re-syncs (drift is acceptable; a stale anchor is expressed as
anchor_found=False).

Path safety: the relative path is validated segment by segment (rejecting
``..`` / absolute paths / backslashes), and the realpath must land inside this
space's knowledge directory — the source name is a name → id symlink
(maintained by knowledge_sync), which still resolves under the space
directory.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

#: Maximum number of paths per resolve call (matches the API body validation)
MAX_PATHS = 50
#: Maximum length of a single path
MAX_PATH_LEN = 512
#: Excerpt truncation limit (characters)
EXCERPT_LIMIT = 800
#: Per-file read limit (enough for frontmatter + anchor lookup; guards
#: against oversized exported markdown files slowing things down)
MAX_READ_BYTES = 4 * 1024 * 1024

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class InvalidPathError(ValueError):
    """Malformed path shape (wrong prefix / traversal segment / too long) —
    the caller should reject the whole batch."""


def _normalize_anchor(text: str) -> str:
    """Lenient normalization of anchors/headings: collapse whitespace, strip
    the ends, drop inline bold markers."""
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def parse_citation_path(raw: str) -> tuple[str, Optional[str]]:
    """Split one citation path into (relative path, anchor). The relative
    path does not include the knowledge/ prefix.

    Raises:
        InvalidPathError: malformed shape (length / prefix / directory
        traversal).
    """
    if len(raw) > MAX_PATH_LEN:
        raise InvalidPathError("citation path too long")
    path_part, _, anchor = raw.partition("#")
    path_part = path_part.strip()
    anchor = anchor.strip()
    if not path_part.startswith("knowledge/"):
        raise InvalidPathError(
            f"citation path must start with knowledge/: {raw[:80]}"
        )
    rel = path_part[len("knowledge/"):]
    if "\\" in rel:
        raise InvalidPathError("citation path contains illegal characters")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if len(parts) < 2:
        # At minimum <source name>/<file> is required
        raise InvalidPathError(
            f"citation path is missing the source name or file: {raw[:80]}"
        )
    if any(p == ".." for p in parts):
        raise InvalidPathError("citation path must not traverse directories")
    return "/".join(parts), anchor or None


def _read_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the YAML frontmatter at the head of an exported md file (a
    narrow inverse of the exporter's frontmatter writer: only flat
    `key: value` lines are recognized — sufficient without pulling in a yaml
    dependency). Returns (meta, body)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        meta[key.strip()] = value.strip()
    body = text[end + len("\n---\n"):]
    # title is a quoted string written via json.dumps; restore the plain text
    title = meta.get("title")
    if title:
        try:
            meta["title"] = str(json.loads(title))
        except ValueError:
            pass
    return meta, body


def _find_excerpt(body: str, anchor: str) -> tuple[bool, Optional[str]]:
    """Slice an excerpt by heading anchor: match the heading line (ignoring
    level and whitespace differences) and take the content after it up to the
    next heading of the same or higher level. Returns (False, None) when not
    found."""
    want = _normalize_anchor(anchor)
    lines = body.splitlines()
    start = level = -1
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and _normalize_anchor(m.group(2)) == want:
            start, level = i + 1, len(m.group(1))
            break
    if start < 0:
        return False, None
    out: list[str] = []
    for line in lines[start:]:
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    excerpt = "\n".join(out).strip()
    if len(excerpt) > EXCERPT_LIMIT:
        excerpt = excerpt[:EXCERPT_LIMIT] + "…"
    return True, excerpt or None


def resolve_paths(
    space_id: str,
    raw_paths: list[str],
    knowledge_root: Path,
    get_source_by_name: Callable[[str, str], Optional[dict]],
    get_source_by_id: Optional[Callable[[str], Optional[dict]]] = None,
) -> list[dict[str, Any]]:
    """Batch-resolve citation paths. Returns one structured result per entry
    (same order as the input):

    ``{path, anchor, exists, anchor_found, source_name, source_type,
       title, origin_url, excerpt}``

    - ``exists=False``: the file does not exist (or the source directory is
      missing) — the frontend degrades to plain text.
    - ``anchor_found``: the lookup result when an anchor was given; None when
      no anchor.
    - Document-style sources read frontmatter for title / origin_url and
      slice an excerpt; git_repo sources degrade to a filename title (no jump
      link, no excerpt — a recorded v1 trade-off).
    - When the first segment does not match a source by name, fall back to a
      lookup by source id (agent retrieval walking the materialized id
      directory cites id paths); ``source_name`` always echoes the source's
      display name.

    Raises:
        InvalidPathError: any malformed path shape (the whole batch is
        rejected; the frontend only sends protocol-matching paths).
    """
    if len(raw_paths) > MAX_PATHS:
        raise InvalidPathError(f"at most {MAX_PATHS} paths per resolve call")
    space_dir = (knowledge_root / space_id).resolve()
    items: list[dict[str, Any]] = []
    for raw in raw_paths:
        rel, anchor = parse_citation_path(raw)
        source_name = rel.split("/", 1)[0]
        source = get_source_by_name(space_id, source_name)
        if source is None and get_source_by_id is not None:
            by_id = get_source_by_id(source_name)
            if by_id and by_id.get("space_id") == space_id:
                source = by_id
                source_name = by_id["name"]
        item: dict[str, Any] = {
            "path": f"knowledge/{rel}",
            "anchor": anchor,
            "exists": False,
            "anchor_found": None,
            "source_name": source_name,
            "source_type": source["type"] if source else None,
            "title": None,
            "origin_url": None,
            "excerpt": None,
        }
        items.append(item)

        file_path = (knowledge_root / space_id / rel).resolve()
        # After resolving the symlink (source name → source id directory) the
        # target must still land inside this space's directory
        if not file_path.is_relative_to(space_dir) or not file_path.is_file():
            continue
        item["exists"] = True
        item["title"] = file_path.stem

        if item["source_type"] == "git_repo":
            # v1 degradation: the chip is visible but not clickable, no excerpt
            continue
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_READ_BYTES)
        except OSError:
            continue
        meta, body = _read_frontmatter(text)
        if meta.get("title"):
            item["title"] = meta["title"]
        if meta.get("origin-url"):
            item["origin_url"] = meta["origin-url"]
        if anchor:
            found, excerpt = _find_excerpt(body, anchor)
            item["anchor_found"] = found
            item["excerpt"] = excerpt
    return items
