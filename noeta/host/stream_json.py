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
import base64
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from noeta.core import models
from noeta.core.client import ClientAPI
from noeta.core.commands import list_commands, run_typed
from noeta.core.events import (
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
from noeta.core.records import CORE
from noeta.core.types import Delta, Image, Message, Text, Thinking, ToolUse, Usage
from noeta.host.pending import Pending, card_title, pending_tree
from noeta.host.pending import follow_tree as _follow_tree
from noeta.host.pending import tree_busy as _tree_busy
from noeta.host.questions import typed_answer
from noeta.host.rewind import diff_stats, file_plan, rewind, rewind_points
from noeta.host.usage import (
    TreeUsage,
    add_events,
    context_breakdown,
    cost_text,
    tree_usage,
)
from noeta.toolkit.permission_modes import MODE_ALIASES, MODE_SIGNAL, PERMISSION_MODES

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


def _usage_json(u: Usage) -> dict[str, Any]:
    return {"input_tokens": u.uncached, "cache_creation_input_tokens": u.cache_write,
            "cache_read_input_tokens": u.cache_read, "output_tokens": u.output,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard"}


def _block_json(block: Any) -> dict[str, Any] | None:
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, Thinking):
        return {"type": "thinking", "thinking": block.text, "signature": block.signature or ""}
    if isinstance(block, ToolUse):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, Image):
        return {"type": "image", "source": {"type": "base64", "media_type": block.media_type,
                                            "data": base64.b64encode(block.data).decode()}}
    return None


def _message_from(content: Any) -> Message:
    """A ``user`` message's content (a string or Anthropic content blocks)."""
    if isinstance(content, str):
        return Message(role="user", content=[Text(text=content)], origin="human")
    blocks: list[Any] = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            blocks.append(Text(text=b["text"]))
        elif b.get("type") == "image" and isinstance(b.get("source"), dict) \
                and b["source"].get("type") == "base64":
            blocks.append(Image(media_type=str(b["source"].get("media_type") or "image/png"),
                                data=base64.b64decode(b["source"].get("data") or "")))
    if not blocks:
        raise ValueError("a user message needs text or image content")
    return Message(role="user", content=blocks, origin="human")


async def find_latest_task(client: ClientAPI, cwd: str, agent: str = "main") -> str | None:
    """``--continue``: the newest root task of ``agent`` started in ``cwd``."""
    rows = [r for r in await client.tasks(limit=200) if r.parent_id is None and r.agent == agent]
    rows.sort(key=lambda r: r.created_at, reverse=True)
    for row in rows:
        events = await client.events(row.task_id, 0)
        first = events[0].event if events else None
        if isinstance(first, TaskCreated) and str(first.config.get("cwd") or "") == cwd:
            return str(row.task_id)
    return None


class StreamJson:
    """One conversation over the headless protocol. ``app`` is what a front
    end runs on (``LocalApp``: ``client``, ``deltas``, ``adopt``, and
    ``runtime`` for the agents and the request preview); ``emit`` writes
    one outgoing message."""

    def __init__(self, app: Any, options: HeadlessOptions, emit: Emit) -> None:
        self.app = app
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
        self._streaming: dict[int, int] = {}
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

    @property
    def runtime(self) -> Any:
        return getattr(self.app, "runtime", None)

    def _agent(self) -> Any:
        rt = self.runtime
        return rt.agents.get(self.options.agent) if rt is not None else None

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
        if o.resume and not o.fork:
            await self.app.adopt(o.resume)
            self.task_id = o.resume
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
        self._put(await self._init_message())
        if self.options.include_partial_messages and hasattr(self.app, "deltas"):
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

    async def _init_message(self) -> dict[str, Any]:
        tools: list[str] = []
        model = self.options.model
        rt = self.runtime
        if rt is not None:
            with contextlib.suppress(Exception):
                request = await rt.preview(self.task_id)
                if request is not None:
                    tools = [t.name for t in request.tools]
                    model = request.model or model
        commands = await self._commands()
        agents = sorted(getattr(rt, "agents", {}) or {})
        mode = MODE_ALIASES.get(self.options.permission_mode or "", self.options.permission_mode)
        return self._message(
            type="system", subtype="init", cwd=self.options.cwd, tools=tools, mcp_servers=[],
            model=model, permissionMode=mode or "auto",
            slash_commands=[c["name"] for c in commands], apiKeySource="none",
            claude_code_version=noeta_version(), output_style="default", agents=agents,
            skills=[], plugins=[])

    async def _commands(self) -> list[dict[str, Any]]:
        agent = self._agent()
        if agent is None:
            return []
        ctx = CommandContext(cwd=self.options.cwd, task_id=self.task_id or None)
        return [{"name": c.name, "description": c.description,
                 "argumentHint": c.argument_hint} for c in await list_commands(agent, ctx)]

    # ------------------------------------------------------------ input

    async def feed(self, line: dict[str, Any]) -> None:
        """One incoming message."""
        kind = line.get("type")
        if kind == "user":
            message = line.get("message") or {}
            await self.send(_message_from(message.get("content")),
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
                                           b for b in map(_block_json, message.content) if b]}),
                       "uuid": prompt_uuid})  # the id rewind_files names it by
        text = message.text
        if text.startswith("/") and len(message.content) == 1:
            agent = self._agent()
            ctx = CommandContext(cwd=self.options.cwd, task_id=self.task_id)
            typed = await run_typed(agent.all_extensions(), ctx, text) if agent else None
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
                               stop_reason=None, total_cost_usd=0.0, usage=_usage_json(Usage()),
                               modelUsage={}, permission_denials=[])
        self._deliver(result)

    async def _next_prompt(self) -> None:
        while self._queued and self._turn is None and not self.ended.is_set():
            message, prompt_uuid = self._queued[0]
            await self._send_now(message, prompt_uuid)
            self._queued.pop(0)  # only now: until its turn is open, it keeps ``busy``

    # ------------------------------------------------------------ control

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
            self._reply(request_id, error=str(exc) or type(exc).__name__)

    async def _on_initialize(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("hooks"):
            log.warning("hook callbacks from the client are not supported; ignored")
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
        rt = self.runtime
        preview = await rt.preview(self.task_id) if rt is not None else None
        if preview is None:
            raise ValueError("this host cannot preview the task's next request")
        parts = context_breakdown(preview)
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
        await self.client.answer(p.task_id, p.request_id, "allow",
                                 always=bool(body.get("updatedPermissions")))

    # ------------------------------------------------------------ following

    def _on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id != self.task_id:
            return
        call = delta.call_seq
        if delta.attempt < self._streaming.get(call, 1):
            return  # the tail of an attempt that failed
        if call in self._streaming and delta.attempt > self._streaming[call]:
            # A retry starts over: the failed attempt's message ends unfinished.
            if self._open_block >= 0:
                self._stream_event({"type": "content_block_stop", "index": self._open_block})
            self._stream_event({"type": "message_stop"})
            del self._streaming[call]
        if call not in self._streaming:
            self._streaming[call] = delta.attempt
            self._stream_event({"type": "message_start", "message": {
                "id": f"msg_{call}", "type": "message", "role": "assistant",
                "model": self.options.model, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": _usage_json(Usage())}})
            self._open_block = -1
        if delta.index != self._open_block:
            if self._open_block >= 0:
                self._stream_event({"type": "content_block_stop", "index": self._open_block})
            self._open_block = delta.index
            block = {"type": "text", "text": ""} if delta.kind == "text" \
                else {"type": "thinking", "thinking": "", "signature": ""}
            self._stream_event({"type": "content_block_start", "index": delta.index,
                                "content_block": block})
        piece = {"type": "text_delta", "text": delta.text} if delta.kind == "text" \
            else {"type": "thinking_delta", "thinking": delta.text}
        self._stream_event({"type": "content_block_delta", "index": delta.index,
                            "delta": piece})

    _open_block = -1

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
                          "errors": [f"lost the task's events: {exc}"]})
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
                else [b for b in map(_block_json, result.content) if b]
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
                        usage=_usage_json(body.last_usage or Usage())))
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
        if self._called in self._streaming:
            if self._open_block >= 0:
                self._stream_event({"type": "content_block_stop", "index": self._open_block})
                self._open_block = -1
            self._stream_event({"type": "message_delta", "delta": {
                "stop_reason": response.stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": response.usage.output}})
            self._stream_event({"type": "message_stop"})
        self._put(self._message(type="assistant", parent_tool_use_id=None, message={
            "id": f"msg_{self._called or rec.seq}", "type": "message", "role": "assistant",
            "model": response.model or self.options.model,
            "content": [b for b in map(_block_json, response.message.content) if b],
            "stop_reason": response.stop_reason, "stop_sequence": None,
            "usage": _usage_json(response.usage)}))
        budget = self.options.max_budget_usd
        if budget is not None and self._turn is not None and not self._turn.over_budget:
            total = await tree_usage(self.client, self.task_id)
            if total.cost_usd >= budget:
                self._turn.over_budget = True
                await self.client.cancel(self.task_id, "over the budget", turn_only=True)

    def _recorded(self, ev: ExtensionRecorded) -> None:
        data = ev.data if isinstance(ev.data, dict) else {}
        if ev.extension == CORE and ev.kind == "spawned":
            for child in data.get("children") or []:
                self._put(self._message(type="system", subtype="task_started",
                                        task_id=child.get("id"), description=child.get("id"),
                                        tool_use_id=child.get("call_id") or None,
                                        task_type="local_agent"))
        elif ev.extension == CORE and ev.kind == "notice":
            self._put(self._message(type="system", subtype="informational",
                                    content=str(data.get("text") or "")))
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
        result["usage"] = _usage_json(mine.usage)
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

    async def refuse_open_asks(self) -> None:
        """The client can no longer answer (its input ended): what it was
        asked is refused, so the turn can finish."""
        asked, self._asked = list(self._asked.values()), {}
        for p in asked:
            await self._refuse(p, "")


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
