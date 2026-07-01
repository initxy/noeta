"""Argument offload helpers for the tool-call events.

A ``ToolCallStarted`` / ``ToolCallApprovalRequested`` payload captures the
call's arguments verbatim. Arguments small enough to fit the EventLog's 4-KB
payload ceiling stay inline; oversized ones are offloaded to the ContentStore
and referenced by ``arguments_ref`` (large bodies go by reference,
never inline). Exactly one of ``arguments`` / ``arguments_ref`` is populated.

These helpers live at the protocols layer so every layer can share one offload
rule: the runtime ``ToolRuntime`` builds ``ToolCallStarted`` payloads; the core
decision handler builds ``ToolCallApprovalRequested`` payloads; the core fold
reads the arguments back. ``noeta.core`` may only import ``noeta.protocols``, so a
runtime-level home would be off-limits to those callers.
"""

from __future__ import annotations

import json
from typing import Any, Union, cast

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import ToolCall
from noeta.protocols.events import (
    ToolCallApprovalRequestedPayload,
    ToolCallStartedPayload,
)
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES, ContentRef


_ARGS_MEDIA_TYPE = "application/json"

# Either tool-call event that captures arguments — both expose ``arguments``
# and ``arguments_ref`` with identical offload semantics.
_ArgPayload = Union[ToolCallStartedPayload, ToolCallApprovalRequestedPayload]


def _arguments_ref_if_oversized(
    inline_payload: _ArgPayload,
    args: dict[str, Any],
    content_store: ContentStore,
) -> ContentRef | None:
    """Return a ContentRef for ``args`` when the inline payload would breach
    the EventLog's payload ceiling, else ``None`` (keep arguments inline).

    The threshold is measured on the *same* canonical bytes the EventLog caps
    (``to_canonical_bytes`` of the whole payload), so a payload is offloaded
    exactly when — and only when — it would otherwise be rejected. The bytes
    are content-addressed, so the same arguments always yield the same ref.
    """
    if len(to_canonical_bytes(inline_payload)) <= EVENT_PAYLOAD_MAX_BYTES:
        return None
    return content_store.put(
        to_canonical_bytes(args), media_type=_ARGS_MEDIA_TYPE
    )


def build_tool_call_started_payload(
    call: ToolCall, content_store: ContentStore
) -> ToolCallStartedPayload:
    """Build a ``ToolCallStarted`` payload, offloading oversized arguments to
    the ContentStore. Exactly one of ``arguments`` /
    ``arguments_ref`` is populated.

    The offload decision is a pure function of the canonical argument bytes,
    so the live ``ToolRuntime`` reconstructs deterministic payloads from the
    same ``ToolCall``.
    """
    args = dict(call.arguments)
    inline = ToolCallStartedPayload(
        call_id=call.call_id, tool_name=call.tool_name, arguments=args
    )
    ref = _arguments_ref_if_oversized(inline, args, content_store)
    if ref is None:
        return inline
    return ToolCallStartedPayload(
        call_id=call.call_id, tool_name=call.tool_name, arguments_ref=ref
    )


def build_tool_call_approval_requested_payload(
    call: ToolCall, content_store: ContentStore
) -> ToolCallApprovalRequestedPayload:
    """Build a ``ToolCallApprovalRequested`` payload, offloading oversized
    arguments to the ContentStore — the same rule as
    :func:`build_tool_call_started_payload`.

    This event is the durable recovery anchor: on resume the fold
    rebuilds the pending entry from it, dereferencing ``arguments_ref`` back
    out of the (equally durable) ContentStore.
    """
    args = dict(call.arguments)
    inline = ToolCallApprovalRequestedPayload(
        call_id=call.call_id, tool_name=call.tool_name, arguments=args
    )
    ref = _arguments_ref_if_oversized(inline, args, content_store)
    if ref is None:
        return inline
    return ToolCallApprovalRequestedPayload(
        call_id=call.call_id, tool_name=call.tool_name, arguments_ref=ref
    )


def resolve_tool_call_arguments(
    payload: _ArgPayload, content_store: ContentStore
) -> dict[str, Any]:
    """Return a tool-call payload's arguments, dereferencing ``arguments_ref``
    from the ContentStore when the call was offloaded (the large-arguments
    path of the ``build_*`` helpers above). Works for both ``ToolCallStarted``
    and ``ToolCallApprovalRequested`` payloads."""
    if payload.arguments_ref is not None:
        body = content_store.get(payload.arguments_ref)
        return cast(dict[str, Any], json.loads(body.decode("utf-8")))
    return dict(payload.arguments or {})
