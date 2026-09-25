"""Route calls to routing tools onto the tools they name.

A tool may carry an optional ``route_call(arguments) -> (tool_name,
arguments)`` attribute (see the ``Tool`` Protocol docstring). The policy
applies it to the ``ToolCallsDecision`` it is about to return — whichever
translate built it — so the Guard, ``can_use_tool``, the audit observer and the
ToolRuntime all see the real tool. The ``call_id`` is kept, so the result pairs
back to the tool_use the provider saw, and the recorded assistant message is
left as the provider sent it. A ``ValueError`` from ``route_call`` answers that
call with its message; the rest of the batch runs.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from noeta.protocols.decisions import Decision, ToolCall, ToolCallsDecision
from noeta.protocols.messages import ToolResultBlock


__all__ = ["route_tool_calls"]


def route_tool_calls(decision: Decision, tools: Mapping[str, Any]) -> Decision:
    """``decision`` with every call to a routing tool rewritten; any other
    decision, or a batch with no routing call, comes back unchanged."""
    if not isinstance(decision, ToolCallsDecision):
        return decision
    calls: list[ToolCall] = []
    refused: list[ToolResultBlock] = []
    routed_any = False
    for call in decision.calls:
        route = getattr(tools.get(call.tool_name), "route_call", None)
        if route is None:
            calls.append(call)
            continue
        routed_any = True
        try:
            name, arguments = route(dict(call.arguments))
        except ValueError as exc:
            refused.append(
                ToolResultBlock(
                    call_id=call.call_id,
                    output="",
                    success=False,
                    error=str(exc),
                )
            )
            continue
        calls.append(
            ToolCall(tool_name=name, arguments=dict(arguments), call_id=call.call_id)
        )
    if not routed_any:
        return decision
    return replace(
        decision,
        calls=calls,
        preacked_results=(*decision.preacked_results, *refused),
    )
