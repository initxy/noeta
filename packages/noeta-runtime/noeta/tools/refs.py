"""The single canonical ``ContentRef`` → JSON encoder for tools.

The ToolRuntime encodes ``ToolResult.output`` with stdlib ``json.dumps``
(``noeta.runtime.tool._encode_output``), which cannot serialise a
:class:`~noeta.protocols.values.ContentRef` dataclass — so a tool puts the
``{hash, size, media_type}`` form into ``output`` and keeps the real
``ContentRef`` in ``ToolResult.artifacts`` or restores it before a store read.
"""

from __future__ import annotations

from typing import Any


__all__ = ["ref_json"]


def ref_json(ref: Any) -> dict[str, Any]:
    """JSON-native form of a ``ContentRef`` (no dataclass)."""
    return {"hash": ref.hash, "size": ref.size, "media_type": ref.media_type}
