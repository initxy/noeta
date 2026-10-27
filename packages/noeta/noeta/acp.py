"""``noeta acp``: drive noeta from any Agent Client Protocol front end (Zed,
JetBrains, Neovim, Toad). One ACP session is one noeta task; what the task
records is translated into ACP session updates as it lands.

Needs the ``acp`` extra (``pip install 'noeta[acp]'``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Iterable
from typing import Any

import acp
from acp import schema

from noeta.events import (
    ExtensionRecorded,
    ModelReplied,
    Recorded,
    Say,
    TaskEnded,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.local import LocalApp
from noeta.types import Delta, Image, Message, Text, ToolUse

log = logging.getLogger("noeta.acp")

#: Tool name → ACP tool kind (what an editor shows next to the call).
TOOL_KINDS: dict[str, str] = {
    "Read": "read", "Edit": "edit", "Write": "edit", "Bash": "execute",
    "BashOutput": "execute", "KillShell": "execute", "Monitor": "execute",
    "Grep": "search", "Glob": "search", "ToolSearch": "search",
    "WebFetch": "fetch", "WebSearch": "fetch", "Task": "think",
}
#: Characters of a tool result shown in the editor.
RESULT_PREVIEW_CHARS = 4000


def tool_title(call: ToolUse) -> str:
    i = call.input
    target = i.get("file_path") or i.get("path") or i.get("command") or i.get("pattern") \
        or i.get("url") or i.get("query") or i.get("description") or ""
    target = str(target).splitlines()[0][:120] if target else ""
    return f"{call.name} {target}".strip()


def _locations(call: ToolUse) -> list[schema.ToolCallLocation] | None:
    path = call.input.get("file_path") or call.input.get("path")
    return [schema.ToolCallLocation(path=str(path))] if isinstance(path, str) else None


def start_updates(reply: ModelReplied, *, streamed_text: bool) -> list[Any]:
    out: list[Any] = []
    message = reply.response.message
    if message.text and not streamed_text:
        out.append(acp.update_agent_message_text(message.text))
    for call in message.tool_uses:
        out.append(acp.start_tool_call(
            call.id, tool_title(call), kind=TOOL_KINDS.get(call.name, "other"),  # type: ignore[arg-type]
            status="pending", locations=_locations(call), raw_input=call.input))
    return out


def finish_update(ev: ToolFinished) -> Any:
    r = ev.result
    text = r.text
    if len(text) > RESULT_PREVIEW_CHARS:
        text = text[:RESULT_PREVIEW_CHARS] + "\n…"
    content: list[Any] = []
    diff = ev.meta.get("diff")
    if isinstance(diff, str) and diff:
        content.append(acp.tool_content(acp.text_block(diff)))
    elif text:
        content.append(acp.tool_content(acp.text_block(text)))
    return acp.update_tool_call(r.call_id, status="failed" if r.is_error else "completed",
                                content=content or None, raw_output=text)


def prompt_message(blocks: Iterable[Any]) -> Message:
    content: list[Any] = []
    for b in blocks:
        kind = getattr(b, "type", "")
        if kind == "text":
            content.append(Text(text=b.text))
        elif kind == "image":
            content.append(Image(media_type=b.mime_type, data=base64.b64decode(b.data)))
        elif kind == "resource_link":
            content.append(Text(text=f"@{b.uri}"))
        elif kind == "resource":
            res = b.resource
            body = getattr(res, "text", None)
            content.append(Text(text=f'<file uri="{res.uri}">\n{body}\n</file>' if body
                                else f"@{res.uri}"))
    return Message(role="user", content=content or [Text(text="")], origin="human")


class NoetaAcpAgent:
    """The ACP agent side. ``agent`` names the noeta agent each session runs."""

    def __init__(self, app: LocalApp, *, agent: str = "main", model: str = "") -> None:
        self.app = app
        self.agent = agent
        self.model = model
        self.conn: Any = None

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, **kw: Any) -> schema.InitializeResponse:
        await self.app.start()
        return schema.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=schema.AgentCapabilities(
                load_session=True,
                prompt_capabilities=schema.PromptCapabilities(image=True, embedded_context=True)),
            agent_info=schema.Implementation(name="noeta", title="Noeta", version="1.0.0"),
        )

    async def new_session(self, cwd: str, mcp_servers: list[Any] | None = None,
                          **kw: Any) -> schema.NewSessionResponse:
        config: dict[str, Any] = {"cwd": cwd}
        if self.model:
            config["model"] = self.model
        if mcp_servers:
            config["mcp_servers"] = [s.model_dump(mode="json") for s in mcp_servers]
        task_id = await self.app.client.create(self.agent, None, config=config)
        await self.app.client.wait(task_id, timeout=30)
        return schema.NewSessionResponse(session_id=task_id)

    async def load_session(self, cwd: str, session_id: str, **kw: Any) -> None:
        for rec in await self.app.client.events(session_id):
            for update in self._replay(rec):
                await self.conn.session_update(session_id=session_id, update=update)

    def _replay(self, rec: Recorded) -> list[Any]:
        ev = rec.event
        if isinstance(ev, ModelReplied):
            return start_updates(ev, streamed_text=False)
        if isinstance(ev, ToolFinished):
            return [finish_update(ev)]
        if hasattr(ev, "items"):
            return [acp.update_user_message_text(i.body.message.text)
                    for i in ev.items if isinstance(i.body, Say)]
        return []

    async def cancel(self, session_id: str, **kw: Any) -> None:
        await self.app.client.cancel(session_id, "stopped from the editor", turn_only=True)

    async def prompt(self, session_id: str, prompt: list[Any], **kw: Any) -> schema.PromptResponse:
        client = self.app.client
        row = await client.get(session_id)
        message = prompt_message(prompt)
        after = row.last_seq
        events = await client.events(session_id, max(0, after - 1))
        waiting = events[-1].event if events else None
        if isinstance(waiting, Waiting) and waiting.on == "question" and waiting.ids:
            await client.answer(session_id, waiting.ids[0], data=message.text)
        else:
            await client.send(session_id, message)

        out: asyncio.Queue[Any] = asyncio.Queue()
        streamed = {"text": False}

        def on_delta(task_id: str, delta: Delta) -> None:
            if task_id != session_id or not delta.text:
                return
            if delta.kind == "text":
                streamed["text"] = True
                out.put_nowait(acp.update_agent_message_text(delta.text))
            else:
                out.put_nowait(acp.update_agent_thought_text(delta.text))

        async def pump() -> None:
            while True:
                update = await out.get()
                try:
                    await self.conn.session_update(session_id=session_id, update=update)
                finally:
                    out.task_done()

        unsubscribe = self.app.deltas(on_delta)
        pumping = asyncio.create_task(pump())
        stop: str = "end_turn"
        taken = False
        try:
            async for rec in client.watch(session_id, after):
                ev = rec.event
                if hasattr(ev, "through") and hasattr(ev, "items"):
                    taken = True
                    continue
                if not taken:
                    continue  # still the previous turn's tail (or the session's first wait)
                if isinstance(ev, ModelReplied):
                    for u in start_updates(ev, streamed_text=streamed["text"]):
                        out.put_nowait(u)
                    streamed["text"] = False
                elif isinstance(ev, ToolStarted):
                    out.put_nowait(acp.update_tool_call(ev.call.id, status="in_progress"))
                elif isinstance(ev, ToolFinished):
                    out.put_nowait(finish_update(ev))
                elif isinstance(ev, ExtensionRecorded) and ev.kind == "turn_stopped":
                    stop = "cancelled"
                elif isinstance(ev, Waiting):
                    if ev.on == "approval":
                        await out.join()
                        await self._ask_permissions(session_id, ev)
                        continue
                    if ev.on == "input":
                        failed = ev.detail.get("turn_failed")
                        if failed:
                            out.put_nowait(acp.update_agent_message_text(
                                f"\n\n[turn failed: {failed}]"))
                        break
                    if ev.on == "question":
                        q = ev.detail or {}
                        out.put_nowait(acp.update_agent_message_text(
                            f"\n\n{q.get('prompt') or 'The agent is asking a question.'}"
                            " (reply with your answer)"))
                        break
                elif isinstance(ev, TaskEnded):
                    stop = "cancelled" if ev.status == "cancelled" else "end_turn"
                    if ev.error and ev.status == "failed":
                        out.put_nowait(acp.update_agent_message_text(f"\n\n[failed: {ev.error}]"))
                    break
            await out.join()
        finally:
            unsubscribe()
            pumping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pumping
        return schema.PromptResponse(stop_reason=stop)  # type: ignore[arg-type]

    async def _ask_permissions(self, session_id: str, waiting: Waiting) -> None:
        options = [
            schema.PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
            schema.PermissionOption(option_id="always", name="Always allow", kind="allow_always"),
            schema.PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
        ]
        for call_id in waiting.ids:
            detail = waiting.detail.get(call_id) or {}
            resp = await self.conn.request_permission(
                session_id=session_id, options=options,
                tool_call=schema.ToolCallUpdate(tool_call_id=call_id,
                                                title=str(detail.get("prompt") or call_id)))
            outcome = resp.outcome
            chosen = getattr(outcome, "option_id", None)
            if chosen in ("allow", "always"):
                await self.app.client.answer(session_id, call_id, "allow",
                                             always=chosen == "always")
            else:
                await self.app.client.answer(session_id, call_id, "deny")


async def serve_stdio(app: LocalApp, *, agent: str = "main", model: str = "") -> None:
    await acp.run_agent(NoetaAcpAgent(app, agent=agent, model=model))  # type: ignore[arg-type]

