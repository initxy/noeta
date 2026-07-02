"""resource_services — ancillary resource services, split out from the task protocol (T6).

Non-task-event capabilities are
their own clean endpoints, physically separate from the core task protocol
(``/stream`` + ``/tasks/*``). This module lands the **data plane** the SSE
stream's ``ContentRef``s and the file panel depend on:

* ``GET /content/{hash}`` — deref a stored blob by hash (the envelope stream
  carries only a ``ContentRef``; large objects come from here, never the stream).
* ``GET /files?task=<id>`` — the workspace file tree (sandboxed to the
  workspace root; a read_model-style projection, allowed since a tree is not a
  task event).
* ``GET /file?task=<id>&path=...`` — a single file's content (sandboxed).

The other two T6 ancillary services live in sibling modules: the HTML-app preview
gateway is prefix-routed (``/preview/<token>/``) in
:mod:`noeta.agent.backend.app`'s handler dispatch, and MCP connector management
(``/mcp/*``) is :mod:`noeta.agent.backend.mcp_service`. Both are wired to the
engine through ``noeta.sdk``'s :class:`~noeta.sdk.HostConfig` (``app_gateway`` /
``mcp_server_resolver``) in :mod:`noeta.agent.backend.lifecycle`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.agent.backend.app import BackendHandler, Router


# Directories never walked into (noise / huge / vcs internals).
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".import_linter_cache",
    }
)
_MAX_TREE_ENTRIES = 5000
_MAX_FILE_BYTES = 1_000_000

# Magic-byte sniff for the common binary content types the UI renders; anything
# else falls back to octet-stream (callers that know better pass their own).
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)


def _sniff_media_type(body: bytes) -> str:
    for magic, mt in _MAGIC:
        if body.startswith(magic):
            return mt
    if len(body) >= 12 and body[0:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _safe_target(root: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``root``; ``None`` if it escapes the sandbox."""
    root = root.resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        return None
    if target != root and root not in target.parents:
        return None
    return target


def _build_tree(root: Path) -> list[dict[str, Any]]:
    root = root.resolve()
    count = 0

    def walk(directory: Path) -> list[dict[str, Any]]:
        nonlocal count
        try:
            children = sorted(
                directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for child in children:
            if count >= _MAX_TREE_ENTRIES:
                break
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            count += 1
            rel = child.relative_to(root).as_posix()
            if child.is_dir():
                out.append(
                    {
                        "name": child.name,
                        "path": rel,
                        "type": "dir",
                        "children": walk(child),
                    }
                )
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                out.append(
                    {"name": child.name, "path": rel, "type": "file", "size": size}
                )
        return out

    return walk(root)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_content(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.engine_room.get_content(params["hash"])
    if body is None:
        handler.send_json({"error": "content not found"}, status=404)
        return
    handler.send_bytes(body, _sniff_media_type(body))


def _handle_files(handler: BackendHandler, params: dict[str, str]) -> None:
    # Serve the tree of the requested session's workspace (``?task=<id>``), not
    # the host-fixed default — a session bound to a non-default project must not
    # show the wrong file tree.
    task = handler.query_params().get("task")
    root = handler.engine_room.workspace_dir_for(task)
    handler.send_json({"root": str(root), "tree": _build_tree(root)})


def _handle_file(handler: BackendHandler, params: dict[str, str]) -> None:
    rel = handler.query_params().get("path")
    if not rel:
        handler.send_json({"error": "query param 'path' is required"}, status=400)
        return
    task = handler.query_params().get("task")
    target = _safe_target(handler.engine_room.workspace_dir_for(task), rel)
    if target is None or not target.is_file():
        handler.send_json({"error": "file not found", "path": rel}, status=404)
        return
    raw = target.read_bytes()
    truncated = len(raw) > _MAX_FILE_BYTES
    text = raw[:_MAX_FILE_BYTES].decode("utf-8", "replace")
    handler.send_json(
        {
            "path": rel,
            "size": len(raw),
            "truncated": truncated,
            "content": text,
        }
    )


def register_resource_routes(router: Router) -> None:
    """Register the data-plane resource services onto ``router`` (T6 core)."""
    router.add("GET", "/content/{hash}", _handle_content)
    router.add("GET", "/files", _handle_files)
    router.add("GET", "/file", _handle_file)
