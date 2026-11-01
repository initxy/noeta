"""``noeta acp``: drive noeta from any Agent Client Protocol front end (Zed,
JetBrains, Neovim, Toad). One ACP session is one noeta task; what the task
records is translated into ACP session updates as it lands.

Needs the ``acp`` extra (``pip install 'noeta[acp] @ git+https://github.com/initxy/noeta'``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterable
from typing import Any

import acp
from acp import schema

from noeta.core import models, records
from noeta.core.commands import parse_command
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
from noeta.core.loop.state import CONFIG_SIGNAL, fold
from noeta.core.types import Message, Text
from noeta.host._acp_updates import RESULT_PREVIEW_CHARS as RESULT_PREVIEW_CHARS
from noeta.host._acp_updates import TOOL_KINDS as TOOL_KINDS
from noeta.host._acp_updates import (
    PromptTurn,
    SessionLog,
    change_content,
    finish_update,
    prompt_message,
    question_ids,
    question_text,
    start_updates,
)
from noeta.host.app import AppAPI
from noeta.host.feed import failure_text
from noeta.host.local import UnknownAgent
from noeta.host.pending import (
    Pending,
    answer_choice,
    card_prompt,
    card_title,
    choices,
    follow_tree,
    pending,
    pending_tree,
)
from noeta.host.questions import next_question, typed_answer
from noeta.host.rewind import rewind, rewind_points
from noeta.host.stream_json import noeta_version
from noeta.host.usage import context_text, cost_text, tree_usage
from noeta.toolkit.interactive import FOLLOWED_CONFIG
from noeta.toolkit.local_env import local_timezone
from noeta.toolkit.permission_modes import (
    MODE_SIGNAL,
    PERMISSION_MODES,
    Modes,
    current_mode,
    published_modes,
)
from noeta.toolkit.signals import RESUMED_SIGNAL

log = logging.getLogger("noeta.host.acp")


class EditorServers:
    """MCP servers an editor named for each session, in Claude Code's
    ``mcpServers`` shape. Kept in memory only: their env and headers are
    credentials and never enter the task log. Pass it as the ``mcp``
    plugin's ``servers``."""

    def __init__(self) -> None:
        self.by_task: dict[str, dict[str, dict[str, Any]]] = {}

    def __call__(self, view: Any) -> dict[str, dict[str, Any]]:
        return self.by_task.get(view.state.root_id or view.task_id, {})

    def add(self, task_id: str, servers: Iterable[Any]) -> None:
        table: dict[str, dict[str, Any]] = {}
        for s in servers:
            given = getattr(s, "env", None) or getattr(s, "headers", None) or []
            pairs = {p.name: p.value for p in given}
            if getattr(s, "command", None):
                table[s.name] = {"command": s.command, "args": list(s.args or []),
                                 "env": pairs}
            elif getattr(s, "url", None):
                table[s.name] = {"type": "http", "url": s.url, "headers": pairs}
        self.by_task[task_id] = table


#: The model option's value for the agent's own model.
DEFAULT_MODEL = "default"


def model_option(model: str) -> schema.SessionConfigOptionSelect:
    """The model as an ACP config option: the catalog's models (and the
    session's own, when it is not one of them)."""
    aliases = {real: alias for alias, real in models.ALIASES.items()}
    known = list(models.catalog_models())
    if model != DEFAULT_MODEL and model not in known:
        known.insert(0, model)
    return schema.SessionConfigOptionSelect(
        type="select", id="model", name="Model", category="model", current_value=model,
        options=[schema.SessionConfigSelectOption(value=DEFAULT_MODEL, name="Agent default"),
                 *(schema.SessionConfigSelectOption(
                     value=m, name=f"{m} ({aliases[m]})" if m in aliases else m)
                   for m in known)])


#: Commands the adapter answers itself (name, hint, description).
HOST_COMMANDS = (
    ("model", "[name]", "Show or switch the model"),
    ("context", "", "What fills the next request"),
    ("cost", "", "Tokens and cost of this session and its sub-agents"),
    ("rewind", "[n] [both|conversation|code]",
     "Go back to before a prompt: files here, the conversation in a new session"),
)


class NoetaAcpAgent:
    """The ACP agent side. ``agent`` names the noeta agent each session runs;
    ``app`` runs here (``LocalApp``) or is a ``noeta serve`` server
    (``RemoteApp``). ``editor_cwd``: a new session's task runs in the
    directory the editor names; without it (a server, where that path means
    nothing) in ``cwd``, or where the server puts it when that is empty."""

    def __init__(self, app: AppAPI, *, agent: str = "main", model: str = "",
                 servers: EditorServers | None = None, editor_cwd: bool = True,
                 cwd: str = "") -> None:
        self.app = app
        self.editor_cwd = editor_cwd
        self.cwd = cwd
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
        self._logs: dict[str, SessionLog] = {}
        #: Each session's model and mode as last picked or recorded (a pick
        #: reaches the log only when the task takes it).
        self._model: dict[str, str] = {}
        self._mode: dict[str, str] = {}
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
            agent_info=schema.Implementation(name="noeta", title="Noeta",
                                             version=noeta_version()),
        )

    async def new_session(self, cwd: str, mcp_servers: list[Any] | None = None,
                          **kw: Any) -> schema.NewSessionResponse:
        config: dict[str, Any] = {}
        if task_cwd := (cwd if self.editor_cwd else self.cwd):
            config["cwd"] = task_cwd
        if self.model:
            config["model"] = self.model
        # The editor that starts this agent runs on the person's machine.
        if zone := local_timezone():
            config["timezone"] = zone
        task_id = await self.app.client.create(self.agent, None, config=config)
        if mcp_servers and self.servers is not None:
            self.servers.add(task_id, mcp_servers)
        await self.app.client.wait(task_id, timeout=30)
        self._cwd[task_id] = cwd
        modes, options = await self._config(task_id)
        # After the response, so the editor knows the session it names.
        announce = asyncio.create_task(self._announce_commands(task_id))
        self._background.add(announce)
        announce.add_done_callback(self._background.discard)
        return schema.NewSessionResponse(session_id=task_id, modes=modes,
                                         config_options=options)

    async def _config(self, task_id: str) -> tuple[schema.SessionModeState | None,
                                                   list[Any]]:
        """The permission modes as ACP session modes (none when the agent
        records no mode: it has no permissions plugin), and the session's
        config options: the model, and the mode again (an editor that
        shows config options prefers them to modes)."""
        events = await self.app.client.events(task_id)
        if task_id not in self._model:
            st = fold(events, {})
            called = [r.event.model for r in events if isinstance(r.event, ModelCalled)]
            self._model[task_id] = str(st.config.get("model") or "") \
                or (called[-1] if called else DEFAULT_MODEL)
        options: list[Any] = [model_option(self._model[task_id])]
        mode = self._mode.get(task_id) or current_mode(events)
        if mode is None:
            return None, options
        offered = published_modes(events) or Modes()
        named = [(m, offered.names.get(m, m)) for m in offered.modes]
        options.append(schema.SessionConfigOptionSelect(
            type="select", id="mode", name="Mode", category="mode", current_value=mode,
            options=[schema.SessionConfigSelectOption(value=m, name=n) for m, n in named]))
        return schema.SessionModeState(current_mode_id=mode, available_modes=[
            schema.SessionMode(id=m, name=n) for m, n in named]), options

    async def _config_changed(self, session_id: str) -> Any:
        _modes, options = await self._config(session_id)
        return schema.ConfigOptionUpdate(session_update="config_option_update",
                                         config_options=options)

    async def set_session_mode(self, session_id: str, mode_id: str,
                               **kw: Any) -> schema.SetSessionModeResponse:
        await self._switch_mode(session_id, mode_id)
        return schema.SetSessionModeResponse()

    async def _switch_mode(self, session_id: str, mode_id: str) -> None:
        if mode_id not in PERMISSION_MODES:
            raise acp.RequestError(-32602, f"unknown mode {mode_id!r}")
        await self.app.client.signal(session_id, MODE_SIGNAL, {"mode": mode_id})
        self._mode[session_id] = mode_id

    async def _switch_model(self, session_id: str, model: str) -> str:
        """Switch the session's model from its next request on (``default``:
        back to the agent's own); the model now picked."""
        real = models.resolve_alias(model)
        await self.app.client.signal(session_id, CONFIG_SIGNAL,
                                     {"model": None if real == DEFAULT_MODEL else real})
        self._model[session_id] = real
        return real

    async def set_config_option(self, config_id: str, session_id: str, value: str | bool,
                                **kw: Any) -> schema.SetSessionConfigOptionResponse:
        if not isinstance(value, str) or config_id not in ("model", "mode"):
            raise acp.RequestError(-32602, f"no config option {config_id!r} takes {value!r}")
        if config_id == "model":
            await self._switch_model(session_id, value)
        else:
            await self._switch_mode(session_id, value)
        _modes, options = await self._config(session_id)
        return schema.SetSessionConfigOptionResponse(config_options=options)

    def _command_context(self, session_id: str) -> CommandContext:
        return CommandContext(cwd=self._cwd.get(session_id) or os.getcwd(), task_id=session_id)

    async def _announce_commands(self, session_id: str) -> None:
        """The agent's plugin commands, as the editor's slash commands."""
        if self.conn is None:
            return
        try:
            commands = [(c.name, c.argument_hint, c.description or c.name)
                        for c in await self.app.commands(self.agent,
                                                         self._command_context(session_id))]
            own = {name for name, _h, _d in commands}
            commands += [c for c in HOST_COMMANDS if c[0] not in own]
            await self.conn.session_update(
                session_id=session_id, update=schema.AvailableCommandsUpdate(
                    session_update="available_commands_update",
                    available_commands=[
                        schema.AvailableCommand(
                            name=name, description=description,
                            input=schema.AvailableCommandInput(
                                root=schema.UnstructuredCommandInput(hint=hint))
                            if hint else None)
                        for name, hint, description in commands]))
        except Exception:
            log.exception("acp: announcing the commands of %s failed", session_id)

    async def _adopt(self, task_id: str) -> None:
        try:
            await self.app.adopt(task_id)
        except UnknownAgent as exc:
            raise acp.RequestError(-32602, str(exc)) from exc

    async def load_session(self, cwd: str, session_id: str,
                           **kw: Any) -> schema.LoadSessionResponse:
        await self._adopt(session_id)
        # A person follows it now, whatever headless run had it before.
        await self.app.client.signal(session_id, CONFIG_SIGNAL, dict(FOLLOWED_CONFIG))
        await self.app.client.signal(session_id, RESUMED_SIGNAL)
        self._cwd[session_id] = cwd
        log = self._logs[session_id] = SessionLog(cwd)
        for rec in await self.app.client.events(session_id):
            for update in self._replay(rec, log):
                await self.conn.session_update(session_id=session_id, update=update)
        plan = log.plan_update(always=True)
        if plan is not None:
            await self.conn.session_update(session_id=session_id, update=plan)
        await self._announce_commands(session_id)
        modes, options = await self._config(session_id)
        return schema.LoadSessionResponse(modes=modes, config_options=options)

    def _log(self, session_id: str) -> SessionLog:
        log = self._logs.get(session_id)
        if log is None:
            log = self._logs[session_id] = SessionLog(self._cwd.get(session_id) or os.getcwd())
        return log

    async def _catch_up(self, session_id: str, upto: int) -> None:
        """Take in what the session recorded up to ``upto`` that this adapter
        has not followed (a mode switched between prompts)."""
        log = self._log(session_id)
        if log.seq < upto:
            for rec in await self.app.client.events(session_id, after=log.seq):
                if rec.seq <= upto:
                    log.note(rec)

    def _replay(self, rec: Recorded, log: SessionLog) -> list[Any]:
        ev = rec.event
        call = log.note(rec)
        if isinstance(ev, ModelReplied):
            return start_updates(ev, streamed_text=False)
        if isinstance(ev, ToolFinished):
            return [finish_update(ev, call)]
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
        await self._catch_up(session_id, after)
        asked = [p for p in await pending_tree(client, session_id) if p.kind == "question"]
        turn = PromptTurn(session_id)
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
                if parse_command(text) is not None:
                    try:
                        typed = await self.app.run_command(
                            self.agent, self._command_context(session_id), text)
                        shown = typed.text if typed is not None and typed.local else None
                        if typed is None:
                            shown = await self._host_command(session_id, text)
                    except Exception as exc:  # noqa: BLE001 — shown to the user
                        shown = f"{text.split()[0]} failed: {exc}"
                    if shown is not None:
                        await self.conn.session_update(
                            session_id=session_id, update=acp.update_agent_message_text(shown))
                        return schema.PromptResponse(stop_reason="end_turn")
                    if typed is not None:
                        message = Message(role="user", content=[Text(text=typed.text)],
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

    async def _host_command(self, session_id: str, text: str) -> str | None:
        """The adapter's own commands (:data:`HOST_COMMANDS`): the text to
        show, or ``None`` when ``text`` is not one of them."""
        parsed = parse_command(text)
        name, arg = parsed if parsed is not None else ("", "")
        client = self.app.client
        if name == "model":
            if not arg:
                await self._config(session_id)
                shown = self._model[session_id]
                return f"model: {'(the agent default)' if shown == DEFAULT_MODEL else shown}"
            real = await self._switch_model(session_id, arg)
            await self.conn.session_update(session_id=session_id,
                                           update=await self._config_changed(session_id))
            return f"model set to {real} from the next request on"
        if name == "cost":
            return cost_text(await tree_usage(client, session_id))
        if name == "context":
            parts = await self.app.context(session_id)
            return context_text(parts) if parts is not None \
                else "this session's agent is not one this editor adapter runs"
        if name != "rewind":
            return None
        points = await rewind_points(client, session_id)
        words = arg.split()
        if not words or not words[0].isdigit() or not 1 <= int(words[0]) <= len(points):
            return "Rewind to before a prompt with /rewind <n> [both|conversation|code]:\n" + \
                "\n".join(f"{n}. {p.text.splitlines()[0][:80] if p.text else ''}"
                          + (f" ({len(p.files)} file(s) changed since)" if p.files else "")
                          for n, p in enumerate(points, 1))
        what = words[1] if len(words) > 1 else "both"
        done = await rewind(client, session_id, points[int(words[0]) - 1],
                            conversation=what in ("both", "conversation"),
                            files=what in ("both", "code"))
        lines = [f"restored {len(done.files)} file(s)"] if done.files else []
        if done.task_id != session_id:
            await self._adopt(done.task_id)
            lines.append(f"the conversation before that prompt goes on in session "
                         f"{done.task_id}; load it to continue (the prompt was: {done.prompt})")
        return "; ".join(lines) or "nothing to restore"

    async def _pump(self, session_id: str, out: asyncio.Queue[Any]) -> None:
        while True:
            update = await out.get()
            try:
                await self.conn.session_update(session_id=session_id, update=update)
            except Exception:
                log.exception("session update failed")
            finally:
                out.task_done()

    async def _follow(self, session_id: str, after: int, turn: PromptTurn) -> str:
        """Translate this prompt's turn into session updates; returns the
        stop reason. The turn starts at the ``InputTaken`` that took this
        prompt's message (the inbox seq ``send`` returned); everything
        before it (the previous turn's tail, unrelated input) is skipped."""
        out = turn.out
        log = self._log(session_id)
        async for rec in follow_tree(self.app.client, session_id, after):
            if isinstance(rec, list):  # what sub-agents wait on the user for
                if await self._child_requests(session_id, rec, out):
                    return "end_turn"
                continue
            ev = rec.event
            call = log.note(rec)
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
                turn.finished.add(ev.result.call_id)
                out.put_nowait(finish_update(ev, call))
                if (plan := log.plan_update()) is not None:
                    out.put_nowait(plan)
            elif isinstance(ev, ExtensionRecorded) and ev.extension == "permissions" \
                    and (mode := current_mode([rec])):
                self._mode[session_id] = mode
                out.put_nowait(schema.CurrentModeUpdate(
                    session_update="current_mode_update", current_mode_id=mode))
                out.put_nowait(await self._config_changed(session_id))
            elif isinstance(ev, ExtensionRecorded) and (core := records.decode(ev)) is not None:
                if isinstance(core, records.Retry):
                    turn.retried(int(core.attempt or 1))
                elif isinstance(core, records.TurnFailed):
                    reason = failure_text(ev.data)
                    out.put_nowait(acp.update_agent_message_text(f"\n\n[turn failed: {reason}]"))
                elif isinstance(core, records.TurnStopped):
                    return "cancelled"
                elif isinstance(core, records.Notice):
                    # Structured news (a review's findings) goes as markdown.
                    shown = core.text if core.notice_kind else f"[{core.text}]"
                    out.put_nowait(acp.update_agent_message_text(f"\n\n{shown}\n\n"))
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
                                card_title(p, session_id), choices(p.detail), card_prompt(p))
        return False

    async def _ask_permissions(self, session_id: str, waiting: Waiting) -> None:
        for p in pending(await self.app.client.events(session_id), session_id):
            if p.kind == "approval" and p.request_id in waiting.ids:
                call = self._log(session_id).calls.get(p.request_id)
                await self._ask_one(session_id, session_id, p.request_id,
                                    card_title(p, session_id), choices(p.detail),
                                    card_prompt(p),
                                    change_content(call) if call is not None else None)

    async def _ask_one(self, session_id: str, task_id: str, call_id: str, title: str,
                       options: list[dict[str, str]], prompt: str = "",
                       change: list[Any] | None = None) -> None:
        kinds = {"allow": "allow_once", "always": "allow_always", "deny": "reject_once"}
        # The card's content replaces the call's: the diff stays on it.
        body: list[Any] = [*(change or ()),
                           *([acp.tool_content(acp.text_block(prompt))] if prompt else ())]
        resp = await self.conn.request_permission(
            session_id=session_id, options=[
                schema.PermissionOption(option_id=o["id"], name=o["label"],
                                        kind=kinds.get(o["id"], "allow_once"))  # type: ignore[arg-type]
                for o in options],
            tool_call=schema.ToolCallUpdate(tool_call_id=call_id, title=title,
                                            content=body or None))
        chosen = getattr(resp.outcome, "option_id", None)
        known = {o["id"] for o in options}
        await answer_choice(self.app.client, task_id, call_id,
                            chosen if chosen in known else "deny")


async def serve_stdio(app: AppAPI, *, agent: str = "main", model: str = "",
                      servers: EditorServers | None = None, editor_cwd: bool = True,
                      cwd: str = "") -> None:
    await acp.run_agent(NoetaAcpAgent(app, agent=agent, model=model,  # type: ignore[arg-type]
                                      servers=servers, editor_cwd=editor_cwd, cwd=cwd))

