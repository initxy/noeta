"""Argument offload for the tool-call events.

Arguments that fit the EventLog's payload ceiling stay inline; oversized ones
go to the ContentStore and are referenced by ``arguments_ref``. Exactly one of
``arguments`` / ``arguments_ref`` is ever populated. The helpers live at the
protocols layer because three different layers must share the *same* offload
rule and ``noeta.core`` may import nothing above ``noeta.protocols``.
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

# Both payloads expose ``arguments`` / ``arguments_ref`` with identical
# offload semantics, so one alias covers every helper below.
_ArgPayload = Union[ToolCallStartedPayload, ToolCallApprovalRequestedPayload]


def _arguments_ref_if_oversized(
    inline_payload: _ArgPayload,
    args: dict[str, Any],
    content_store: ContentStore,
) -> ContentRef | None:
    """Return a ContentRef for ``args`` when the inline payload would breach
    the EventLog's payload ceiling, else ``None`` (keep arguments inline).

    The threshold is measured on the *same* canonical bytes the EventLog caps,
    so a payload is offloaded exactly when — and only when — it would otherwise
    be rejected. The bytes are content-addressed, so identical arguments always
    yield an identical ref.
    """
    if len(to_canonical_bytes(inline_payload)) <= EVENT_PAYLOAD_MAX_BYTES:
        return None
    return content_store.put(
        to_canonical_bytes(args), media_type=_ARGS_MEDIA_TYPE
    )


def build_tool_call_started_payload(
    call: ToolCall, content_store: ContentStore
) -> ToolCallStartedPayload:
    """Build a ``ToolCallStarted`` payload, offloading oversized arguments.

    The offload decision is a pure function of the canonical argument bytes, so
    the same ``ToolCall`` always reconstructs the same payload.
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
    """Build a ``ToolCallApprovalRequested`` payload under the same offload
    rule as :func:`build_tool_call_started_payload`.

    This event is the durable recovery anchor: on resume the fold rebuilds the
    pending entry from it, dereferencing ``arguments_ref`` back out of the
    equally durable ContentStore.
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
    from the ContentStore when the call was offloaded."""
    if payload.arguments_ref is not None:
        body = content_store.get(payload.arguments_ref)
        return cast(dict[str, Any], json.loads(body.decode("utf-8")))
    return dict(payload.arguments or {})
