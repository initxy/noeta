"""The hooks that gate a tool call: PreToolUse (deny, ask, vouch for or
rewrite it) and PermissionRequest (answer an ask in the person's place)."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from noeta.core.extension import Allow, Ask, Deny, Gate, Rewrite, TaskView
from noeta.core.types import ToolUse
from noeta.plugins.hooks.dispatch import CONTEXT_LABEL, KEEP, NAME, Dispatcher

#: The tool a sub-agent ends its run with (a model-visible name).
HANDBACK_TOOL = "SubagentHandback"

#: Runs Stop / SubagentStop hooks: ``(event, view, last message)`` -> the
#: reason to go on, or ``None``.
StopHooks = Callable[[str, TaskView, str], Awaitable["str | None"]]


class ToolGates:
    """PreToolUse and PermissionRequest over one :class:`Dispatcher`."""

    def __init__(self, dispatch: Dispatcher, stop: StopHooks) -> None:
        self._d = dispatch
        self._stop = stop
        #: (task id, call id) -> (the input a PreToolUse hook rewrote the
        #: call to, its verdict on the call): the rewritten call is gated
        #: again, and the hook is not asked about its own rewrite.
        self._rewrites: OrderedDict[tuple[str, str], tuple[dict[str, Any], Gate | None]] = \
            OrderedDict()

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        rewrite = self._rewrites.get((view.task_id, call.id))
        if rewrite is not None and rewrite[0] == call.input:
            return rewrite[1]
        if call.name == HANDBACK_TOOL and view.state.parent_id is not None:
            message = call.input.get("message")
            reason = await self._stop("SubagentStop", view,
                                      message if isinstance(message, str) else "")
            if reason is not None:
                return Deny(reason=reason)
        reads = await self._d.run("PreToolUse", view, {
            "tool_name": call.name, "tool_input": call.input, "tool_use_id": call.id}, call=call)
        decision: str | None = None
        reason = ""
        updated: dict[str, Any] | None = None
        contexts: list[str] = []
        for r in reads:
            if r.stop is not None:
                await self._d.stop_turn(view, r.stop)
                return Deny(reason=r.stop or "stopped by a hook")
            chosen = r.specific.get("permissionDecision")
            if chosen is None and r.data.get("decision") == "approve":
                chosen = "allow"
            why = str(r.specific.get("permissionDecisionReason") or r.data.get("reason") or "")
            if chosen == "deny":
                return Deny(reason=why or "denied by a hook")
            if r.block is not None:
                return Deny(reason=r.block)
            if chosen == "ask" or (chosen == "allow" and decision != "ask"):
                decision, reason = chosen, why
            if isinstance(r.specific.get("updatedInput"), dict):
                updated = dict(r.specific["updatedInput"])
            contexts.append(r.context)
        self._d.context(view, "context", f"PreToolUse:{call.name}{CONTEXT_LABEL}", contexts)
        verdict: Gate | None = None
        if decision == "ask":
            verdict = Ask(prompt=reason or f"A hook asks to confirm {call.name}", firm=True)
        elif decision == "allow":
            verdict = Allow(approve=True)
        if updated is not None and updated != call.input:
            self._remember((view.task_id, call.id), (updated, verdict))
            return Rewrite(input=updated)
        return verdict

    def _remember(self, key: tuple[str, str], value: tuple[dict[str, Any], Gate | None]) -> None:
        self._rewrites[key] = value
        self._rewrites.move_to_end(key)
        while len(self._rewrites) > KEEP:
            self._rewrites.popitem(last=False)

    async def resolve_ask(self, view: TaskView, call: ToolUse) -> Gate | None:
        if "PermissionRequest" not in self._d.matchers:
            return None
        decided = self._d.state(view).permission.get(call.id)
        if decided is None:
            decided = await self._permission_request(view, call)
            view.record(NAME, "permission", {"call_id": call.id, **decided})
            if decided.get("behavior") == "deny" and decided.get("interrupt"):
                await self._d.stop_turn(view, "")
        behavior = decided.get("behavior")
        if behavior == "deny":
            return Deny(reason=str(decided.get("message") or "denied by a hook"))
        if behavior != "allow":
            return None
        wanted = decided.get("input")
        if isinstance(wanted, dict) and wanted != call.input:
            return Rewrite(input=wanted)
        return Allow()

    async def _permission_request(self, view: TaskView, call: ToolUse) -> dict[str, Any]:
        reads = await self._d.run("PermissionRequest", view, {
            "tool_name": call.name, "tool_input": call.input, "tool_use_id": call.id}, call=call)
        out: dict[str, Any] = {"behavior": None}
        for r in reads:
            if r.block is not None:
                return {"behavior": "deny", "message": r.block}
            answer = r.specific.get("decision")
            if not isinstance(answer, dict):
                continue
            if answer.get("behavior") == "deny":
                return {"behavior": "deny",
                        "message": str(answer.get("message") or "denied by a hook"),
                        "interrupt": bool(answer.get("interrupt"))}
            if answer.get("behavior") == "allow":
                wanted = answer.get("updatedInput")
                out = {"behavior": "allow", "input": wanted if isinstance(wanted, dict)
                       else None}
        return out
