"""Claude Code's headless protocol (``claude -p --input-format stream-json
--output-format stream-json``), translated into the client API, so a
program written for Claude Code's Agent SDK drives noeta: ``noeta -p`` on
stdin/stdout, and the HTTP host's ``/stream-json`` socket.

What goes out, one JSON object per line: ``system``/``init``; an
``assistant`` message per model reply; a ``user`` message per tool result;
``stream_event`` (Anthropic's streaming events) with partial messages on;
``system`` notes (``compact_boundary``, ``task_started``,
``task_notification``, ``status``, ``local_command_output``,
``informational``); a ``result`` when a prompt's work is done (usage, cost,
duration, turns, permission denials, structured output). The root task's
id is the protocol's ``session_id``.

What comes in: ``user`` messages (text or content blocks, a ``/command``
runs as typed), and ``control_request`` s — ``initialize``, ``interrupt``,
``set_permission_mode``, ``set_model``, ``set_max_thinking_tokens``,
``rewind_files``, ``get_context_usage``, ``get_session_cost``,
``list_models``, ``mcp_status``. Permission prompts go to the client as
``can_use_tool`` requests when it answers them (``--permission-prompt-tool
stdio``); otherwise anything that would ask is refused and the turn goes
on, as in Claude Code's headless mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from noeta.core import models, records
from noeta.core.client import ClientAPI
from noeta.core.events import (
    DEFAULT_TENANT,
    ChildEnded,
    Compacted,
    ExtensionRecorded,
    InputTaken,
    ModelCalled,
    ModelReplied,
    Recorded,
    Say,
    TaskCreated,
    TaskEnded,
    ToolFinished,
    Waiting,
)
from noeta.core.extension import CommandContext
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.core.types import Delta, Message, Text, Usage
from noeta.host._errors import error_text
from noeta.host._stream_json_control import ControlRequests
from noeta.host._stream_json_wire import PartialMessages, block_json, message_from, usage_json
from noeta.host.app import AppAPI
from noeta.host.pending import Pending, pending_tree
from noeta.host.pending import follow_tree as _follow_tree
from noeta.host.pending import tree_busy as _tree_busy
from noeta.host.usage import TreeUsage, add_events, tree_usage
from noeta.toolkit.permission_modes import MODE_ALIASES, MODE_SIGNAL
from noeta.toolkit.signals import RESUMED_SIGNAL

__all__ = ["HeadlessOptions", "StreamJson", "drive", "find_latest_task", "noeta_version"]

log = logging.getLogger("noeta.host.stream_json")

Emit = Callable[[dict[str, Any]], Awaitable[None]]



def noeta_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("noeta")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass
class HeadlessOptions:
    """One conversation's settings, as ``claude -p`` takes them."""

    agent: str = "main"
    model: str = ""
    cwd: str = ""
    permission_mode: str | None = None
    #: ``--max-turns``: model calls per prompt.
    max_turns: int | None = None
    max_budget_usd: float | None = None
    #: ``--json-schema``: the answer is JSON matching it (``structured_output``).
    output_schema: dict[str, Any] | None = None
    include_partial_messages: bool = False
    replay_user_messages: bool = False
    #: ``True`` (``--permission-prompt-tool stdio``): the client answers
    #: permission prompts through ``can_use_tool``; otherwise they are refused.
    ask_client: bool = False
    #: ``--resume`` / ``--continue``: the task to go on with; with ``fork`` a
    #: new task continues its conversation (``--fork-session``).
    resume: str | None = None
    fork: bool = False
    #: ``--session-id``: the id a new task gets.
    task_id: str | None = None
    #: More task config (``prompts`` for ``--system-prompt``).
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Turn:
    """A prompt's work, from its message until the task tree is idle."""

    started: float
    after: int
    api_calls: int = 0
    #: The task has taken the prompt (a wait for input before that is not
    #: this prompt's end).
    taken: bool = False
    stopped: bool = False
    over_budget: bool = False
    denials: list[dict[str, Any]] = field(default_factory=list)


async def find_latest_task(client: ClientAPI, cwd: str, agent: str = "main") -> str | None:
    """``--continue``: the newest root task of ``agent`` started in ``cwd``.
    Tasks come most recently updated first; the window grows until the
    newest match is certain (no task beyond it can have started later) or
    every task has been seen."""
    checked: dict[str, bool] = {}

    async def started_here(task_id: str) -> bool:
        if task_id not in checked:
            events = await client.events(task_id, 0)
            first = events[0].event if events else None
            checked[task_id] = isinstance(first, TaskCreated) \
                and str(first.config.get("cwd") or "") == cwd
        return checked[task_id]

    limit = 200
    while True:
        rows = await client.tasks(limit=limit)
        everything = len(rows) < limit
        # A task not listed was last updated (so started) before this.
        horizon = min((r.updated_at for r in rows), default=0.0)
        roots = sorted((r for r in rows if r.parent_id is None and r.agent == agent),
                       key=lambda r: r.created_at, reverse=True)
        for row in roots:
            if not everything and row.created_at < horizon:
                break
            if await started_here(row.task_id):
                return str(row.task_id)
        if everything:
            return None
        limit *= 10


class StreamJson(ControlRequests):
    """One conversation over the headless protocol. ``app`` is what a front
    end runs on (``noeta.host.app.AppAPI``: in process or a remote server);
    ``emit`` writes one outgoing message. The control requests both ways
    are :class:`~noeta.host._stream_json_control.ControlRequests`.
    ``tenant``: whose conversation it is (its commands run with that
    tenant's servers and files)."""

    def __init__(self, app: AppAPI, options: HeadlessOptions, emit: Emit, *,
                 tenant: str = DEFAULT_TENANT) -> None:
        self.app = app
        self.tenant = tenant
        self.client = app.client
        self.options = options
        self._emit = emit
        self.task_id = ""
        self._out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._turn: _Turn | None = None
        self._done: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._requests = 0
        #: Our ``can_use_tool`` requests waiting for the client: id -> pending.
        self._asked: dict[str, Pending] = {}
        self._shown: set[tuple[str, str]] = set()
        #: Prompt uuid -> inbox seq of its message (0: the first prompt).
        self._uuids: dict[str, int] = {}
        self._called = 0
        #: Prompts waiting for the open one's result: (message, uuid).
        self._queued: list[tuple[Message, str]] = []
        self._partial = PartialMessages(self._stream_event)
        self._writer: asyncio.Task[None] | None = None
        self._follower: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self.ended = asyncio.Event()
        self.last_result: dict[str, Any] | None = None

    # ------------------------------------------------------------ output

    def _put(self, message: dict[str, Any]) -> None:
        self._out.put_nowait(message)

    def _message(self, **fields: Any) -> dict[str, Any]:
        return {**fields, "session_id": self.task_id, "uuid": str(uuid.uuid4())}

    async def _write_loop(self) -> None:
        while True:
            message = await self._out.get()
            if message is None:
                return
            try:
                await self._emit(message)
            except Exception:
                log.debug("emit failed", exc_info=True)
                self.ended.set()
                return

    # ------------------------------------------------------------ lifecycle

    async def start(self, prompt: Message | None = None) -> str:
        """Create or resume the task (with ``prompt`` as its first
        message), announce it (``system``/``init``) and follow it."""
        o = self.options
        self._writer = asyncio.create_task(self._write_loop())
        config: dict[str, Any] = {**o.config, "cwd": o.cwd}
        if o.model:
            config["model"] = o.model
        if o.permission_mode:
            config["permission_mode"] = MODE_ALIASES.get(o.permission_mode, o.permission_mode)
        if o.max_turns:
            config["max_turns"] = o.max_turns
        if o.output_schema is not None:
            config["output_schema"] = o.output_schema
        if o.resume:
            # Through the (tenant-scoped) client first: another tenant's
            # task is unknown before the app serves or names anything of it.
            await self.client.get(o.resume)
        if o.resume and not o.fork:
            await self.app.adopt(o.resume)
            self.task_id = o.resume
            await self.client.signal(self.task_id, RESUMED_SIGNAL)
            changes = {k: config[k] for k in ("model", "max_turns") if k in config}
            if changes:
                await self.client.signal(self.task_id, CONFIG_SIGNAL, changes)
            if "permission_mode" in config:
                await self.client.signal(self.task_id, MODE_SIGNAL,
                                         {"mode": config["permission_mode"]})
        else:
            source = o.resume if o.fork else None
            self.task_id = await self.client.create(
                o.agent, None, task_id=o.task_id, config=config, history_from=source)
        if not o.cwd:
            # A server picks the directory of a task created without one and
            # records it; that is where the task runs, so say so.
            o.cwd = await self._recorded_cwd()
        self._put(await self._init_message())
        if self.options.include_partial_messages:
            self._unsubscribe = self.app.deltas(self._on_delta)
        after = (await self.client.get(self.task_id)).last_seq
        self._follower = asyncio.create_task(self._follow(after))
        if prompt is not None:
            await self.send(prompt, str(uuid.uuid4()))
        return self.task_id

    async def close(self) -> None:
        if self._follower is not None:
            self._follower.cancel()
            with contextlib.suppress(BaseException):
                await self._follower
        if self._unsubscribe is not None:
            self._unsubscribe()
        if self._writer is not None:
            self._out.put_nowait(None)
            with contextlib.suppress(BaseException):
                await self._writer

    async def _recorded_cwd(self) -> str:
        """The ``cwd`` in the task's ``TaskCreated`` config, or ``""``."""
        events = await self.client.events(self.task_id, 0)
        first = events[0].event if events else None
        return str(first.config.get("cwd") or "") if isinstance(first, TaskCreated) else ""

    async def _init_message(self) -> dict[str, Any]:
        tools: list[str] = []
        model = self.options.model
        with contextlib.suppress(Exception):
            parts = await self.app.context(self.task_id)
            if parts is not None:
                tools = list(parts.get("tools") or [])
                model = parts.get("model") or model
        commands = await self._commands()
        agents = await self.app.agent_names()
        mode = MODE_ALIASES.get(self.options.permission_mode or "", self.options.permission_mode)
        return self._message(
            type="system", subtype="init", cwd=self.options.cwd, tools=tools, mcp_servers=[],
            model=model, permissionMode=mode or "auto",
            slash_commands=[c["name"] for c in commands], apiKeySource="none",
            claude_code_version=noeta_version(), output_style="default", agents=agents,
            skills=[], plugins=[])

    async def _commands(self) -> list[dict[str, Any]]:
        ctx = CommandContext(cwd=self.options.cwd, task_id=self.task_id or None,
                             tenant=self.tenant)
        return [{"name": c.name, "description": c.description,
                 "argumentHint": c.argument_hint}
                for c in await self.app.commands(self.options.agent, ctx)]

    # ------------------------------------------------------------ input

    async def feed(self, line: dict[str, Any]) -> None:
        """One incoming message."""
        kind = line.get("type")
        if kind == "user":
            message = line.get("message") or {}
            await self.send(message_from(message.get("content")),
                            str(line.get("uuid") or uuid.uuid4()))
        elif kind == "control_request":
            await self._control(str(line.get("request_id") or ""), line.get("request") or {})
        elif kind == "control_response":
            await self._answered(line.get("response") or {})
        elif kind in ("keep_alive", "update_environment_variables", "control_cancel_request"):
            return
        else:
            raise ValueError(f"unknown message type {kind!r}")

    async def send(self, message: Message, prompt_uuid: str) -> None:
        """A prompt from the client. One that arrives while another's work
        is open waits for its result, so every prompt gets its own (as
        Claude Code queues them)."""
        if self._turn is not None or self._queued:
            self._queued.append((message, prompt_uuid))
            return
        await self._send_now(message, prompt_uuid)

    async def _send_now(self, message: Message, prompt_uuid: str) -> None:
        if self.options.replay_user_messages:
            self._put({**self._message(type="user", parent_tool_use_id=None, isReplay=True,
                                       message={"role": "user", "content": [
                                           b for b in map(block_json, message.content) if b]}),
                       "uuid": prompt_uuid})  # the id rewind_files names it by
        text = message.text
        if text.startswith("/") and len(message.content) == 1:
            ctx = CommandContext(cwd=self.options.cwd, task_id=self.task_id,
                                 tenant=self.tenant)
            typed = await self.app.run_command(self.options.agent, ctx, text)
            if typed is not None and typed.local:
                self._put(self._message(type="system", subtype="local_command_output",
                                        content=typed.text))
                self._finish_local(typed.text)
                return
            if typed is not None:
                message = Message(role="user", content=[Text(text=typed.text)], origin="human")
        row = await self.client.get(self.task_id)
        self._turn = _Turn(started=time.time(), after=row.last_seq)
        seq = await self.client.send(self.task_id, message)
        self._uuids[prompt_uuid] = seq

    def _finish_local(self, text: str) -> None:
        result = self._message(type="result", subtype="success", is_error=False,
                               duration_ms=0, duration_api_ms=0, num_turns=0, result=text,
                               stop_reason=None, total_cost_usd=0.0, usage=usage_json(Usage()),
                               modelUsage={}, permission_denials=[])
        self._deliver(result)

    async def _next_prompt(self) -> None:
        while self._queued and self._turn is None and not self.ended.is_set():
            message, prompt_uuid = self._queued[0]
            await self._send_now(message, prompt_uuid)
            self._queued.pop(0)  # only now: until its turn is open, it keeps ``busy``

    # ------------------------------------------------------------ following

    def _on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id == self.task_id:
            self._partial.delta(delta, self.options.model)

    def _stream_event(self, event: dict[str, Any]) -> None:
        self._put(self._message(type="stream_event", event=event, parent_tool_use_id=None))

    async def _follow(self, after: int) -> None:
        try:
            async for rec in _follow_tree(self.client, self.task_id, after):
                if isinstance(rec, list):
                    await self._ask(rec)
                else:
                    await self._event(rec)
                if self.ended.is_set():
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("following task %s failed", self.task_id)
            await self._finish({"subtype": "error_during_execution", "is_error": True,
                          "errors": [f"lost the task's events: {error_text(exc)}"]})
        self.ended.set()

    async def _event(self, rec: Recorded) -> None:
        ev = rec.event
        if isinstance(ev, ModelCalled):
            self._called = rec.seq
        elif isinstance(ev, ModelReplied):
            await self._replied(ev, rec)
        elif isinstance(ev, ToolFinished):
            result = ev.result
            content: Any = result.text if all(isinstance(b, Text) for b in result.content) \
                else [b for b in map(block_json, result.content) if b]
            self._put(self._message(type="user", parent_tool_use_id=None, message={
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": result.call_id,
                                             "content": content, "is_error": result.is_error}]}))
        elif isinstance(ev, Compacted):
            self._put(self._message(type="system", subtype="compact_boundary",
                                    compact_metadata={"trigger": "auto",
                                                      "pre_tokens": ev.usage.input}))
        elif isinstance(ev, ExtensionRecorded):
            self._recorded(ev)
        elif isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, ChildEnded):
                    body = item.body
                    status = {"done": "completed", "failed": "failed"}.get(body.status, "stopped")
                    self._put(self._message(
                        type="system", subtype="task_notification", task_id=body.child_id,
                        status=status, output_file="", summary=body.answer or body.error,
                        usage=usage_json(body.last_usage or Usage())))
                elif isinstance(item.body, Say) \
                        and item.body.message.origin in (None, "human"):
                    if self._turn is None:
                        self._turn = _Turn(started=rec.at, after=rec.seq - 1)
                    self._turn.taken = True
        elif isinstance(ev, Waiting):
            await self._waiting(ev)
        elif isinstance(ev, TaskEnded):
            if self._turn is not None or self.last_result is None:
                await self._finish({"subtype": "success" if ev.status == "done"
                              else "error_during_execution",
                              "is_error": ev.status != "done", "result": ev.answer,
                              "errors": [ev.error] if ev.error else []})
            self.ended.set()

    async def _replied(self, ev: ModelReplied, rec: Recorded) -> None:
        response = ev.response
        if self._turn is not None:
            self._turn.api_calls += 1
        self._partial.replied(self._called, response.stop_reason, response.usage.output)
        self._put(self._message(type="assistant", parent_tool_use_id=None, message={
            "id": f"msg_{self._called or rec.seq}", "type": "message", "role": "assistant",
            "model": response.model or self.options.model,
            "content": [b for b in map(block_json, response.message.content) if b],
            "stop_reason": response.stop_reason, "stop_sequence": None,
            "usage": usage_json(response.usage)}))
        budget = self.options.max_budget_usd
        if budget is not None and self._turn is not None and not self._turn.over_budget:
            total = await tree_usage(self.client, self.task_id)
            if total.cost_usd >= budget:
                self._turn.over_budget = True
                await self.client.cancel(self.task_id, "over the budget", turn_only=True)

    def _recorded(self, ev: ExtensionRecorded) -> None:
        data = ev.data if isinstance(ev.data, dict) else {}
        core = records.decode(ev)
        if isinstance(core, records.Spawned):
            for child in core.children:
                self._put(self._message(type="system", subtype="task_started",
                                        task_id=child.get("id"), description=child.get("id"),
                                        tool_use_id=child.get("call_id") or None,
                                        task_type="local_agent"))
        elif isinstance(core, records.Notice):
            self._put(self._message(type="system", subtype="informational", content=core.text))
        elif ev.extension == "permissions" and ev.kind == "mode" and data.get("mode"):
            self._put(self._message(type="system", subtype="status", status=None,
                                    permissionMode=data["mode"]))

    async def _waiting(self, ev: Waiting) -> None:
        if ev.on in ("approval", "question"):
            await self._ask(await pending_tree(self.client, self.task_id))
            return
        if ev.on != "input" or self._turn is None or not self._turn.taken:
            return
        failed = ev.detail.get("turn_failed")
        if not failed and await _tree_busy(self.client, self.task_id):
            return  # background agents still run; their reports open another turn
        turn = self._turn
        if failed and str(failed).startswith("max_turns"):
            await self._finish({"subtype": "error_max_turns", "is_error": True,
                          "errors": [str(failed).split(": ", 1)[-1]]})
        elif turn.over_budget:
            await self._finish({"subtype": "error_max_budget_usd", "is_error": True,
                          "errors": [f"Reached maximum budget (${self.options.max_budget_usd})"]})
        elif failed:
            await self._finish({"subtype": "error_during_execution", "is_error": True,
                          "errors": [str(failed)]})
        elif turn.stopped:
            await self._finish({"subtype": "error_during_execution", "is_error": True,
                          "errors": ["[Request interrupted by user]"]})
        else:
            await self._finish({"subtype": "success", "is_error": False,
                          "result": str(ev.detail.get("answer") or "")})

    async def _finish(self, outcome: dict[str, Any]) -> None:
        """Close the current prompt's work with a ``result``."""
        turn, self._turn = self._turn, None
        result = self._message(type="result", **outcome)
        result.setdefault("result", "")
        result["duration_ms"] = int((time.time() - turn.started) * 1000) if turn else 0
        result["stop_reason"] = "end_turn" if outcome.get("subtype") == "success" else None
        result["permission_denials"] = list(turn.denials) if turn else []
        if outcome.get("subtype") == "success" and self.options.output_schema is not None:
            with contextlib.suppress(ValueError):
                result["structured_output"] = json.loads(str(result["result"]))
        await self._complete(result, turn)

    async def _complete(self, result: dict[str, Any], turn: _Turn | None) -> None:
        total = await tree_usage(self.client, self.task_id)
        mine = TreeUsage()
        if turn is not None:
            add_events(mine, await self.client.events(self.task_id), after=turn.after)
        result["duration_api_ms"] = int(mine.api_seconds * 1000)
        result["num_turns"] = sum(m.calls for m in mine.by_model.values())
        result["total_cost_usd"] = round(total.cost_usd, 6)
        result["usage"] = usage_json(mine.usage)
        result["modelUsage"] = {
            name: {"inputTokens": m.usage.uncached, "outputTokens": m.usage.output,
                   "cacheReadInputTokens": m.usage.cache_read,
                   "cacheCreationInputTokens": m.usage.cache_write, "webSearchRequests": 0,
                   "costUSD": round(m.cost_usd, 6),
                   "contextWindow": models.model_limits(name)[0]}
            for name, m in total.by_model.items()}
        self._deliver(result)
        await self._next_prompt()

    def _deliver(self, result: dict[str, Any]) -> None:
        self.last_result = result
        self._put(result)
        self._done.put_nowait(result)

    @property
    def busy(self) -> bool:
        return self._turn is not None or bool(self._asked) or bool(self._queued)

    async def settled(self) -> None:
        """Wait until no prompt's work is open (or the task has ended)."""
        while self.busy and not self.ended.is_set():
            waiters = [asyncio.ensure_future(self._done.get()),
                       asyncio.ensure_future(self.ended.wait())]
            await asyncio.wait(waiters, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            for w in waiters:
                w.cancel()


async def drive(conversation: StreamJson, *, prompt: Message | None = None,
                lines: AsyncIterator[dict[str, Any]] | None = None,
                on_bad_line: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """Run one conversation: ``prompt`` alone (``--input-format text``:
    done at its result), or every message of ``lines`` until they end and
    the work they started is done. Returns the last ``result``."""
    try:
        await conversation.start(prompt)
        if lines is not None:
            async for line in lines:
                try:
                    await conversation.feed(line)
                except (ValueError, KeyError, TypeError) as exc:
                    if on_bad_line is not None:
                        on_bad_line(str(exc))
                if conversation.ended.is_set():
                    break
            await conversation.refuse_open_asks()
        await conversation.settled()
        return conversation.last_result
    finally:
        await conversation.close()
