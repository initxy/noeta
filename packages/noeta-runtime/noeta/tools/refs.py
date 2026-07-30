"""Shared ``ContentRef`` → JSON helper for the tool packs.

The ToolRuntime encodes ``ToolResult.output`` with stdlib ``json.dumps``
(``noeta.runtime.tool._encode_output``), which cannot serialise a
:class:`~noeta.protocols.values.ContentRef` dataclass. So tools put the
``{hash, size, media_type}`` JSON form into ``output`` (and accept it as
an argument), keeping the real ``ContentRef`` only in
``ToolResult.artifacts`` or restoring it internally before a store read.

This is the single canonical encoder; the fs / mcp / research packs all
import it instead of each carrying a local copy. It is a ``noeta.tools``
helper, so ``noeta.tools.mcp`` may import it under the
``mcp-tools-boundary`` import-linter contract.
"""

from __future__ import annotations

from typing import Any


__all__ = ["ref_json"]


def ref_json(ref: Any) -> dict[str, Any]:
    """JSON-native form of a ``ContentRef`` (no dataclass)."""
    return {"hash": ref.hash, "size": ref.size, "media_type": ref.media_type}
