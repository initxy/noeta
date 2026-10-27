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
import os
from collections.abc import Iterable
from typing import Any

import acp
from acp import schema

from noeta.apps.local import LocalApp, UnknownAgent
from noeta.core.commands import expand_command, list_commands, parse_command
from noeta.core.events import (
    ExtensionRecorded,
    InputTaken,
    ModelCalled,
    ModelReplied,
    Recorded,
    Say,
    TaskEnded,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.core.extension import CommandContext
from noeta.core.loop.state import CORE
from noeta.core.pending import Pending, follow_tree, pending_tree
from noeta.core.types import Delta, Image, Message, Text, ToolUse
from noeta.plugins.ask_user import next_question, typed_answer

log = logging.getLogger("noeta.apps.acp")

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


def question_ids(waiting: Waiting) -> list[str]:
    """The calls a ``Waiting`` asks the user about (its detail is keyed by
    call id, each entry with the parked call's ``on``)."""
    return [k for k, v in waiting.detail.items()
            if isinstance(v, dict) and v.get("on") == "question"]


def question_text(detail: dict[str, Any], only: dict[str, Any] | None = None) -> str:
    """A parked question as plain text: each question (or ``only`` that
    one) and its options."""
    lines: list[str] = []
    for q in [only] if only is not None else detail.get("questions") or []:
        lines.append(str(q.get("question") or ""))
        for opt in q.get("options") or []:
            desc = opt.get("description")
            lines.append(f"  - {opt.get('label', '')}" + (f": {desc}" if desc else ""))
    return "\n".join(lines) or str(detail.get("prompt") or "")


class _Turn:
    """One prompt's view of the task: which inbox item started it, and the
    live output that belongs to it."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.out: asyncio.Queue[Any] = asyncio.Queue()
        #: The inbox seq of this prompt's message (or answer).
        self.seq = -1
        #: The log seq of the ``InputTaken`` that took it; ``None`` until seen.
        self.input_seq: int | None = None
        self.called = 0
        self.done = False
        self._early: list[Delta] = []
        #: Per model call (its ``ModelCalled`` seq): the live attempt and the
        #: text streamed for it.
        self._attempt: dict[int, int] = {}
        self._text: dict[int, str] = {}
        #: Calls whose failed attempt already showed text (the retry's text
        #: is set apart from it).
        self._shown: set[int] = set()

    def on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id != self.session_id or not delta.text or self.done:
            return
        if self.input_seq is None:
            self._early.append(delta)  # sorted out once the turn's start is known
            return
        self._forward(delta)

    def start(self, input_seq: int) -> None:
        self.input_seq = input_seq
        early, self._early = self._early, []
        for d in early:
            self._forward(d)

    def _forward(self, delta: Delta) -> None:
        assert self.input_seq is not None
        if delta.call_seq and delta.call_seq <= self.input_seq:
            return  # an earlier turn's call
        key = delta.call_seq
        live = self._attempt.get(key, 1)
        if delta.attempt < live:
            return  # a failed attempt's tail
        if delta.attempt > live:
            self.retried(delta.attempt - 1, key)
        if delta.kind == "text":
            if not self._text.get(key) and key in self._shown:
                self._shown.discard(key)
                self.out.put_nowait(acp.update_agent_message_text("\n\n"))
            self._text[key] = self._text.get(key, "") + delta.text
            self.out.put_nowait(acp.update_agent_message_text(delta.text))
        else:
            self.out.put_nowait(acp.update_agent_thought_text(delta.text))

    def retried(self, failed_attempt: int, key: int | None = None) -> None:
        """Attempt ``failed_attempt`` of a call failed: its fragments are
        dropped and the next attempt's text is reconciled on its own."""
        key = self.called if key is None else key
        if failed_attempt + 1 <= self._attempt.get(key, 1):
            return
        self._attempt[key] = failed_attempt + 1
        if self._text.get(key):
            self._shown.add(key)
        self._text[key] = ""

    def unsent_text(self, full: str) -> str:
        """What of a recorded reply's text the editor has not been sent."""
        sent = self._text.pop(self.called, "")
        if not full or sent == full:
            return ""
        if full.startswith(sent):
            return full[len(sent):]
        return f"\n\n{full}"


class EditorServers:
    """MCP servers an editor named for each session. Kept in memory only:
    their env and headers are credentials and never enter the task log. Pass
    it as the ``mcp`` plugin's ``servers``."""

    def __init__(self) -> None:
        self.by_task: dict[str, list[Any]] = {}

    def __call__(self, view: Any) -> list[Any]:
        return self.by_task.get(view.state.root_id or view.task_id, [])

    def add(self, task_id: str, servers: Iterable[Any]) -> None:
        from noeta.plugins.mcp import McpServer
        from noeta.plugins.mcp.settings import alias_for

        converted = []
        for s in servers:
            alias = alias_for(s.name)
            given = getattr(s, "env", None) or getattr(s, "headers", None) or []
            pairs = {p.name: p.value for p in given}
            if getattr(s, "command", None):
                converted.append(McpServer(alias=alias, command=[s.command, *(s.args or [])],
                                           env=pairs))
            elif getattr(s, "url", None):
                converted.append(McpServer(alias=alias, url=s.url, headers=pairs))
        self.by_task[task_id] = converted


class NoetaAcpAgent:
    """The ACP agent side. ``agent`` names the noeta agent each session runs."""

    def __init__(self, app: LocalApp, *, agent: str = "main", model: str = "",
                 servers: EditorServers | None = None) -> None:
        self.app = app
        self.servers = servers
        self.agent = agent
        self.model = model
        self.conn: Any = None
        self._locks: dict[str, asyncio.Lock] = {}
        #: Answers typed so far to a question with several parts, by
        #: (task id, request id).
        self._partial: dict[tuple[str, str], dict[str, Any]] = {}
        #: Sub-agent approvals already put to the editor.
        self._asked: set[tuple[str, str]] = set()
        #: Each session's working directory, for plugin commands.
        self._cwd: dict[str, str] = {}
        self._background: set[asyncio.Task[None]] = set()

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
        task_id = await self.app.client.create(self.agent, None, config=config)
        if mcp_servers and self.servers is not None:
            self.servers.add(task_id, mcp_servers)
        await self.app.client.wait(task_id, timeout=30)
        self._cwd[task_id] = cwd
        # After the response, so the editor knows the session it names.
        announce = asyncio.create_task(self._announce_commands(task_id))
        self._background.add(announce)
        announce.add_done_callback(self._background.discard)
        return schema.NewSessionResponse(session_id=task_id)

    def _command_context(self, session_id: str) -> CommandContext:
        return CommandContext(cwd=self._cwd.get(session_id) or os.getcwd(), task_id=session_id)

    async def _announce_commands(self, session_id: str) -> None:
        """The agent's plugin commands, as the editor's slash commands."""
        agent = self.app.runtime.agents.get(self.agent)
        if agent is None or self.conn is None:
            return
        try:
            commands = await list_commands(agent, self._command_context(session_id))
            if not commands:
                return
            await self.conn.session_update(
                session_id=session_id, update=schema.AvailableCommandsUpdate(
                    session_update="available_commands_update",
                    available_commands=[
                        schema.AvailableCommand(
                            name=c.name, description=c.description or c.name,
                            input=schema.AvailableCommandInput(
                                root=schema.UnstructuredCommandInput(hint=c.argument_hint))
                            if c.argument_hint else None)
                        for c in commands]))
        except Exception:
            log.exception("acp: announcing the commands of %s failed", session_id)

    async def _adopt(self, task_id: str) -> None:
        try:
            await self.app.adopt(task_id)
        except UnknownAgent as exc:
            raise acp.RequestError(-32602, str(exc)) from exc

    async def load_session(self, cwd: str, session_id: str, **kw: Any) -> None:
        await self._adopt(session_id)
        self._cwd[session_id] = cwd
        for rec in await self.app.client.events(session_id):
            for update in self._replay(rec):
                await self.conn.session_update(session_id=session_id, update=update)
        await self._announce_commands(session_id)

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
        # One prompt at a time per session: a prompt sent right after a
        # cancel waits until the stopped turn has answered "cancelled".
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._prompt(session_id, prompt_message(prompt))

    async def _prompt(self, session_id: str, message: Message) -> schema.PromptResponse:
        client = self.app.client
        await self._adopt(session_id)
        row = await client.get(session_id)
        after = row.last_seq
        asked = [p for p in await pending_tree(client, session_id) if p.kind == "question"]
        turn = _Turn(session_id)
        unsubscribe = self.app.deltas(turn.on_delta)
        try:
            if asked:
                q = asked[0]
                key = (q.task_id, q.request_id)
                answers = typed_answer(q.detail, message.text, self._partial.pop(key, None))
                nxt = next_question(q.detail, answers)
                if nxt is not None:  # one line per question: ask the next one
                    self._partial[key] = answers
                    await self.conn.session_update(
                        session_id=session_id, update=acp.update_agent_message_text(
                            f"{question_text(q.detail, nxt)}\n\n(reply with your answer)"))
                    return schema.PromptResponse(stop_reason="end_turn")
                turn.seq = await client.answer(q.task_id, q.request_id,
                                               data={"answers": answers})
                if q.task_id != session_id:
                    turn.start(after)  # a sub-agent took it; follow the root from here
            else:
                text = message.text if len(message.content) == 1 else ""
                agent = self.app.runtime.agents.get(self.agent)
                if agent is not None and parse_command(text) is not None:
                    try:
                        expanded = await expand_command(
                            agent, self._command_context(session_id), text)
                    except Exception as exc:  # noqa: BLE001 — shown to the user
                        failed = f"{text.split()[0]} failed: {exc}"
                        await self.conn.session_update(
                            session_id=session_id, update=acp.update_agent_message_text(failed))
                        return schema.PromptResponse(stop_reason="end_turn")
                    if expanded is not None:
                        message = Message(role="user", content=[Text(text=expanded)],
                                          origin="human")
                turn.seq = await client.send(session_id, message)
            pumping = asyncio.create_task(self._pump(session_id, turn.out))
            try:
                stop = await self._follow(session_id, after, turn)
                await turn.out.join()
            finally:
                pumping.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pumping
        finally:
            turn.done = True
            unsubscribe()
        return schema.PromptResponse(stop_reason=stop)  # type: ignore[arg-type]

    async def _pump(self, session_id: str, out: asyncio.Queue[Any]) -> None:
        while True:
            update = await out.get()
            try:
                await self.conn.session_update(session_id=session_id, update=update)
            except Exception:
                log.exception("session update failed")
            finally:
                out.task_done()

    async def _follow(self, session_id: str, after: int, turn: _Turn) -> str:
        """Translate this prompt's turn into session updates; returns the
        stop reason. The turn starts at the ``InputTaken`` that took this
        prompt's message (the inbox seq ``send`` returned); everything
        before it (the previous turn's tail, unrelated input) is skipped."""
        out = turn.out
        async for rec in follow_tree(self.app.client, session_id, after):
            if isinstance(rec, list):  # what sub-agents wait on the user for
                if await self._child_requests(session_id, rec, out):
                    return "end_turn"
                continue
            ev = rec.event
            if turn.input_seq is None:
                if isinstance(ev, InputTaken) and any(i.seq == turn.seq for i in ev.items):
                    turn.start(rec.seq)
                continue
            if isinstance(ev, ModelCalled):
                turn.called = rec.seq
            elif isinstance(ev, ModelReplied):
                rest = turn.unsent_text(ev.response.message.text)
                if rest:
                    out.put_nowait(acp.update_agent_message_text(rest))
                for u in start_updates(ev, streamed_text=True):
                    out.put_nowait(u)
            elif isinstance(ev, ToolStarted):
                out.put_nowait(acp.update_tool_call(ev.call.id, status="in_progress"))
            elif isinstance(ev, ToolFinished):
                out.put_nowait(finish_update(ev))
            elif isinstance(ev, ExtensionRecorded) and ev.extension == CORE:
                if ev.kind == "retry":
                    turn.retried(int((ev.data or {}).get("attempt") or 1))
                elif ev.kind == "turn_failed":
                    data = ev.data if isinstance(ev.data, dict) else {}
                    reason = ": ".join(str(x) for x in (data.get("reason"), data.get("detail"))
                                       if x)
                    out.put_nowait(acp.update_agent_message_text(f"\n\n[turn failed: {reason}]"))
                elif ev.kind == "turn_stopped":
                    return "cancelled"
            elif isinstance(ev, Waiting):
                if ev.on == "approval":
                    await out.join()
                    await self._ask_permissions(session_id, ev)
                elif ev.on == "input":
                    return "end_turn"
                elif ev.on == "question":
                    first = question_ids(ev)[:1]
                    detail = (ev.detail.get(first[0]) or {}) if first else {}
                    nxt = next_question(detail)
                    text = question_text(detail, nxt) if nxt is not None else ""
                    out.put_nowait(acp.update_agent_message_text(
                        f"\n\n{text or 'The agent is asking a question.'}"
                        "\n\n(reply with your answer)"))
                    return "end_turn"
            elif isinstance(ev, TaskEnded):
                if ev.error and ev.status == "failed":
                    out.put_nowait(acp.update_agent_message_text(f"\n\n[failed: {ev.error}]"))
                return "cancelled" if ev.status == "cancelled" else "end_turn"
        return "end_turn"

    async def _child_requests(self, session_id: str, found: list[Pending],
                              out: asyncio.Queue[Any]) -> bool:
        """Put sub-agents' approvals to the editor; a sub-agent's question
        ends the prompt (the next one answers it). ``True`` when it did."""
        for p in found:
            if p.kind == "question":
                nxt = next_question(p.detail, self._partial.get((p.task_id, p.request_id)))
                text = question_text(p.detail, nxt) if nxt is not None else ""
                out.put_nowait(acp.update_agent_message_text(
                    f"\n\n[{p.agent or 'sub-agent'}] {text or 'A sub-agent asks a question.'}"
                    "\n\n(reply with your answer)"))
                return True
        for p in found:
            if (p.task_id, p.request_id) in self._asked:
                continue
            self._asked.add((p.task_id, p.request_id))
            await out.join()
            await self._ask_one(session_id, p.task_id, p.request_id,
                                f"[{p.agent or 'sub-agent'}] {p.prompt or p.tool or p.request_id}")
        return False

    async def _ask_permissions(self, session_id: str, waiting: Waiting) -> None:
        for call_id in waiting.ids:
            detail = waiting.detail.get(call_id) or {}
            await self._ask_one(session_id, session_id, call_id,
                                str(detail.get("prompt") or call_id))

    async def _ask_one(self, session_id: str, task_id: str, call_id: str, title: str) -> None:
        options = [
            schema.PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
            schema.PermissionOption(option_id="always", name="Always allow", kind="allow_always"),
            schema.PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
        ]
        resp = await self.conn.request_permission(
            session_id=session_id, options=options,
            tool_call=schema.ToolCallUpdate(tool_call_id=call_id, title=title))
        chosen = getattr(resp.outcome, "option_id", None)
        if chosen in ("allow", "always"):
            await self.app.client.answer(task_id, call_id, "allow", always=chosen == "always")
        else:
            await self.app.client.answer(task_id, call_id, "deny")


async def serve_stdio(app: LocalApp, *, agent: str = "main", model: str = "",
                      servers: EditorServers | None = None) -> None:
    await acp.run_agent(NoetaAcpAgent(app, agent=agent, model=model,  # type: ignore[arg-type]
                                      servers=servers))

