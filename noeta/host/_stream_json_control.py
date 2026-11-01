"""The control half of Claude Code's headless protocol for
:class:`~noeta.host.stream_json.StreamJson`: the client's ``control_request``
s (``initialize``, ``interrupt``, ``set_permission_mode`` …), and the
permission prompts sent to the client as ``can_use_tool`` requests."""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from noeta.core import models
from noeta.core.client import ClientAPI
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.host._errors import error_text
from noeta.host.app import AppAPI
from noeta.host.pending import Pending, card_title
from noeta.host.questions import typed_answer
from noeta.host.rewind import diff_stats, file_plan, rewind, rewind_points
from noeta.host.usage import cost_text, tree_usage
from noeta.toolkit.json_schema import schema_problem
from noeta.toolkit.permission_modes import MODE_ALIASES, MODE_SIGNAL, PERMISSION_MODES
from noeta.toolkit.structured_output import SCHEMA_KEY

if TYPE_CHECKING:
    from noeta.host.stream_json import HeadlessOptions, _Turn

log = logging.getLogger("noeta.host.stream_json")


class ControlRequests:
    """Control requests both ways, mixed into ``StreamJson``: the client's,
    each answered by its ``_on_<subtype>`` handler, and ours, the
    ``can_use_tool`` prompts for approvals and questions."""

    client: ClientAPI
    app: AppAPI
    options: HeadlessOptions
    task_id: str
    _turn: _Turn | None
    _requests: int
    #: Our ``can_use_tool`` requests waiting for the client: id -> pending.
    _asked: dict[str, Pending]
    _shown: set[tuple[str, str]]
    #: Prompt uuid -> inbox seq of its message (0: the first prompt).
    _uuids: dict[str, int]

    if TYPE_CHECKING:
        def _put(self, message: dict[str, Any]) -> None: ...

        async def _commands(self) -> list[dict[str, Any]]: ...

    async def request(self, subtype: str, **fields: Any) -> str:
        """Send a control request to the client; returns its id."""
        self._requests += 1
        request_id = f"req_{self._requests}_{uuid.uuid4().hex[:8]}"
        self._put({"type": "control_request", "request_id": request_id,
                   "request": {"subtype": subtype, **fields}})
        return request_id

    def _reply(self, request_id: str, response: dict[str, Any] | None = None,
               error: str | None = None) -> None:
        if error is not None:
            body: dict[str, Any] = {"subtype": "error", "request_id": request_id, "error": error}
        else:
            body = {"subtype": "success", "request_id": request_id, "response": response or {}}
        self._put({"type": "control_response", "response": body})

    async def _control(self, request_id: str, request: dict[str, Any]) -> None:
        subtype = request.get("subtype")
        try:
            handler = getattr(self, f"_on_{subtype}", None)
            if handler is None:
                raise ValueError(f"Unsupported control request subtype: {subtype}")
            self._reply(request_id, await handler(request))
        except Exception as exc:  # noqa: BLE001 — the client gets the error, the run goes on
            self._reply(request_id, error=error_text(exc))

    async def _on_initialize(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("hooks"):
            log.warning("hook callbacks from the client are not supported; ignored")
        schema = request.get("jsonSchema")
        if schema is not None:
            # The SDK's ``outputFormat``: structured output from here on.
            problem = schema_problem(schema)
            if problem is not None:
                raise ValueError(f"jsonSchema is not a valid JSON Schema: {problem}")
            await self.client.signal(self.task_id, CONFIG_SIGNAL, {SCHEMA_KEY: schema})
            self.options.output_schema = schema
        return {"commands": await self._commands(), "output_style": "default",
                "available_output_styles": ["default"], "models": self._models(),
                "account": {}, "pid": os.getpid()}

    def _models(self) -> list[dict[str, Any]]:
        out = [{"value": alias, "displayName": alias.capitalize(), "description": real}
               for alias, real in models.ALIASES.items()]
        if self.options.model and self.options.model not in models.ALIASES:
            out.insert(0, {"value": self.options.model, "displayName": self.options.model,
                           "description": "the model this run started with"})
        return out

    async def _on_list_models(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"models": self._models()}

    async def _on_interrupt(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._turn is not None:
            self._turn.stopped = True
            await self.client.cancel(self.task_id, "interrupted by the client", turn_only=True)
        return {}

    async def _on_set_permission_mode(self, request: dict[str, Any]) -> dict[str, Any]:
        mode = str(request.get("mode") or "")
        mode = MODE_ALIASES.get(mode, mode)
        if mode not in PERMISSION_MODES:
            raise ValueError(f"unknown permission mode {mode!r}")
        await self.client.signal(self.task_id, MODE_SIGNAL, {"mode": mode})
        self.options.permission_mode = mode
        return {"mode": mode}

    async def _on_set_model(self, request: dict[str, Any]) -> dict[str, Any]:
        model = request.get("model")
        if model in (None, "", "default"):
            model = self.options.model or None
        await self.client.signal(self.task_id, CONFIG_SIGNAL, {"model": model})
        return {}

    async def _on_set_max_thinking_tokens(self, request: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def _on_mcp_status(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"mcpServers": []}

    async def _on_get_session_cost(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"text": cost_text(await tree_usage(self.client, self.task_id))}

    async def _on_get_context_usage(self, request: dict[str, Any]) -> dict[str, Any]:
        parts = await self.app.context(self.task_id)
        if parts is None:
            raise ValueError("this host cannot preview the task's next request")
        window = max(int(parts["window"]), 1)
        return {"categories": [{"name": k, "tokens": v} for k, v in parts["categories"].items()],
                "totalTokens": parts["total"], "maxTokens": window,
                "rawMaxTokens": window, "percentage": round(100 * parts["total"] / window, 1),
                "model": parts["model"]}

    async def _on_rewind_files(self, request: dict[str, Any]) -> dict[str, Any]:
        wanted = str(request.get("user_message_id") or "")
        if wanted not in self._uuids:
            return {"canRewind": False, "error": f"no prompt with id {wanted!r} in this run"}
        points = await rewind_points(self.client, self.task_id)
        inbox = self._uuids[wanted]
        point = next((p for p in points if p.inbox_seq == inbox and p.task_id == self.task_id),
                     None)
        if point is None and inbox == 0 and points:
            point = points[0]
        if point is None:
            return {"canRewind": False, "error": "that prompt has not been taken yet"}
        plan = await file_plan(self.client, self.task_id, point)
        added, removed = diff_stats(plan)
        if not request.get("dry_run"):
            await rewind(self.client, self.task_id, point, conversation=False)
        return {"canRewind": True, "filesChanged": sorted(plan.files), "insertions": added,
                "deletions": removed}

    # ------------------------------------------------------------ permission prompts

    async def _ask(self, items: list[Pending]) -> None:
        for p in items:
            key = (p.task_id, p.request_id)
            if key in self._shown:
                continue
            self._shown.add(key)
            if not self.options.ask_client:
                await self._refuse(p, "")
                continue
            fields: dict[str, Any] = {"tool_name": p.tool, "input": p.input,
                                      "tool_use_id": p.request_id, "permission_suggestions": []}
            if p.prompt:
                fields["decision_reason"] = p.prompt
            if p.task_id != self.task_id:
                fields["agent_id"] = p.task_id
            fields["title"] = card_title(p, self.task_id)
            self._asked[await self.request("can_use_tool", **fields)] = p

    async def _refuse(self, p: Pending, message: str, *, interrupt: bool = False) -> None:
        if self._turn is not None:
            self._turn.denials.append({"tool_name": p.tool, "tool_use_id": p.request_id,
                                       "tool_input": p.input})
        if p.kind == "question":
            await self.client.answer(p.task_id, p.request_id, "deny")
            return
        reason = message or (p.prompt or f"Permission to use {p.tool} was not granted.")
        data: dict[str, Any] = {"message": reason}
        if not interrupt:
            data["interrupt"] = False
        await self.client.answer(p.task_id, p.request_id, "deny", data=data)

    async def _answered(self, response: dict[str, Any]) -> None:
        p = self._asked.pop(str(response.get("request_id") or ""), None)
        if p is None:
            return
        body = response.get("response") if response.get("subtype") == "success" else None
        if not isinstance(body, dict) or body.get("behavior") != "allow":
            message = str((body or {}).get("message") or response.get("error") or "")
            await self._refuse(p, message, interrupt=bool((body or {}).get("interrupt")))
            return
        if p.kind == "question":
            given = (body.get("updatedInput") or {}).get("answers")
            answers: dict[str, Any] = {}
            questions = [q for q in p.detail.get("questions") or [] if isinstance(q, dict)]
            for q in questions:
                said = given.get(q.get("question")) if isinstance(given, dict) else None
                answers = typed_answer(p.detail, str(said or ""), answers)
            await self.client.answer(p.task_id, p.request_id, data={"answers": answers})
            return
        # An approval may change the call (Claude Code's ``updatedInput``);
        # the permissions plugin runs the call with that input.
        updated = body.get("updatedInput")
        data = {"updated_input": updated} \
            if isinstance(updated, dict) and updated != p.input else None
        await self.client.answer(p.task_id, p.request_id, "allow",
                                 always=bool(body.get("updatedPermissions")), data=data)

    async def refuse_open_asks(self) -> None:
        """The client can no longer answer (its input ended): what it was
        asked is refused, so the turn can finish."""
        asked, self._asked = list(self._asked.values()), {}
        for p in asked:
            await self._refuse(p, "")
