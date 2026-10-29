"""Firing one hook event: which hooks match, Claude Code's input fields,
running them (``async`` ones in the background) and reading their answers
— plus the state the hooks plugin folds from its records."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from noeta.core.events import Cancel, ExtensionRecorded
from noeta.core.extension import TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput
from noeta.core.types import Message, Text, ToolUse, user
from noeta.plugins.hooks.config import SUBJECT, TOOL_EVENTS, HookCommand, Matcher
from noeta.plugins.hooks.runners import (
    HookResult,
    Runner,
    RunTool,
    run_agent,
    run_http,
    run_mcp_tool,
    run_prompt,
)
from noeta.toolkit.inline_commands import refusal
from noeta.toolkit.permission_rules import RuleContext, match

log = logging.getLogger("noeta.hooks")

NAME = "hooks"
#: Entries kept per cache (per call id).
KEEP = 256
#: The plugin whose folded ``mode`` is the task's permission mode.
PERMISSIONS_PLUGIN = "permissions"


@dataclass(frozen=True)
class HookState:
    #: Seq of the newest person's message the prompt hooks have seen
    #: (``None``: they have never looked at this task).
    prompted_through: int | None = None
    #: Stop hooks that blocked since the person's message ``stop_since``.
    stop_blocks: int = 0
    stop_since: int = 0
    #: SessionStart (startup) / SubagentStart has fired for this task.
    started: bool = False
    #: The compaction SessionStart (compact) last fired after, by ``through``.
    compact_seen: int = 0
    #: What started the newest compaction (``manual`` / ``auto``).
    compact_trigger: str = "auto"
    #: Keys of the ``once`` hooks that have run.
    once: frozenset[str] = frozenset()
    #: PermissionRequest answers by call id: ``{"behavior", "input",
    #: "message"}`` (``behavior`` ``None``: no hook decided).
    permission: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Seq of the newest batch of tool results PostToolBatch has seen.
    batched_through: int = 0
    #: The shell's working directory as CwdChanged last reported it.
    cwd: str | None = None
    #: The model as PostModelSwitch last reported it.
    model: str | None = None


def fold(state: Any, kind: str, data: Any) -> HookState:
    """One of this plugin's records folded into its state."""
    st = state if isinstance(state, HookState) else HookState()
    if not isinstance(data, dict):
        return st
    if kind == "prompted":
        return replace(st, prompted_through=int(data.get("through") or 0))
    if kind == "stop_blocked":
        since = int(data.get("since") or 0)
        blocks = st.stop_blocks + 1 if since == st.stop_since else 1
        return replace(st, stop_blocks=blocks, stop_since=since)
    if kind == "started":
        return replace(st, started=True)
    if kind == "compacting":
        return replace(st, compact_trigger=str(data.get("trigger") or "auto"))
    if kind == "compact_seen":
        return replace(st, compact_seen=int(data.get("through") or 0))
    if kind == "once":
        return replace(st, once=st.once | {str(data.get("key"))})
    if kind == "batched":
        return replace(st, batched_through=int(data.get("through") or 0))
    if kind == "cwd_seen":
        return replace(st, cwd=str(data.get("cwd") or "") or None)
    if kind == "model_seen":
        return replace(st, model=str(data.get("model") or "") or None)
    if kind == "permission":
        decided = dict(st.permission)
        decided[str(data.get("call_id"))] = {k: v for k, v in data.items() if k != "call_id"}
        while len(decided) > KEEP:
            decided.pop(next(iter(decided)))
        return replace(st, permission=decided)
    return st


@dataclass(frozen=True)
class Read:
    """One hook's answer, read the way every event reads it."""

    #: The reason when it blocks (exit 2, or ``decision: "block"``).
    block: str | None = None
    #: ``continue: false``: the stop reason (may be empty).
    stop: str | None = None
    #: ``hookSpecificOutput``.
    specific: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)
    #: Plain stdout (not a JSON object).
    text: str = ""

    @property
    def context(self) -> str:
        extra = self.specific.get("additionalContext")
        return extra.strip() if isinstance(extra, str) else ""


def _read(event: str, result: HookResult) -> Read:
    if result.exit_code == 2:
        return Read(block=result.stderr.strip() or "blocked by a hook")
    if result.exit_code != 0:
        return Read()
    data = result.data
    if data is None:
        return Read(text=result.text)
    specific = data.get("hookSpecificOutput")
    specific = specific if isinstance(specific, dict) else {}
    named = specific.get("hookEventName")
    if named is not None and named != event:
        log.warning("hooks: a %s hook answered for %r; its answer is ignored", event, named)
        return Read()
    block = str(data.get("reason") or "blocked by a hook") \
        if data.get("decision") == "block" else None
    stop = str(data.get("stopReason") or "") if data.get("continue") is False else None
    return Read(block=block, stop=stop, specific=specific, data=data)


def agent_fields(view: TaskView) -> dict[str, str]:
    """A sub-agent's ``agent_id`` and ``agent_type``: the id and type the
    spawning tool put in its config (``agent_id`` / ``subagent_type``), else
    the task's id and agent."""
    config = view.state.config
    agent_id, kind = config.get("agent_id"), config.get("subagent_type")
    return {"agent_id": agent_id if isinstance(agent_id, str) and agent_id else view.task_id,
            "agent_type": kind if isinstance(kind, str) and kind else view.state.agent}


def _read_only_tools(view: TaskView) -> list[Tool]:
    """The task's read-only tools: what an agent hook may look with."""
    names = dict.fromkeys(t.name for ext in view.extensions for t in ext.tools(view)
                          if getattr(t, "read_only", False))
    return [t for t in (view.find_tool(n) for n in names) if t is not None]


def _mcp_tool(view: TaskView, hook: HookCommand) -> Tool | None:
    """An ``mcp_tool`` hook's tool, by its model-visible name (the MCP
    plugin's ``mcp__<server>__<tool>``, other characters made ``_``)."""
    name = f"mcp__{hook.server}__{hook.tool}"
    return view.find_tool(name) or view.find_tool(re.sub(r"[^A-Za-z0-9_-]", "_", name))


def _tool_runner(view: TaskView, kind: str, *, checked: bool = False) -> RunTool:
    """Runs a tool for a hook, outside the model's calls: nothing it would
    record is kept. ``checked``: the task's permission checks (not its
    hooks) run first, and what would ask is refused, as Claude Code runs an
    agent hook in ``dontAsk`` mode."""

    async def run(tool: Tool, input: dict[str, Any], n: int) -> ToolOutput:
        if view.env is None:
            raise RuntimeError("this task has no execution environment")
        if checked:
            why = await refusal(view, ToolUse(id=f"hook-{kind}-{n}", name=tool.name,
                                              input=input), skip=NAME)
            if why is not None:
                return ToolOutput(content=[Text(text=f"{tool.name} was not run: {why}")],
                                  is_error=True)
        ctx = ToolContext(task_id=view.task_id, call_id=f"hook-{kind}-{n}", env=view.env,
                          state=dict(view.state.ext), record=lambda *_a: None)
        return await tool.run(input, ctx)

    return run


class Dispatcher:
    """The configured hooks and how one event fires them (:meth:`run`)."""

    def __init__(self, matchers: dict[str, list[Matcher]], runner: Runner,
                 http_transport: httpx.AsyncBaseTransport | None) -> None:
        self.matchers = matchers
        self.runner = runner
        self.http_transport = http_transport
        self._background: set[asyncio.Future[Any]] = set()

    def state(self, view: TaskView) -> HookState:
        """This plugin's state, with what it recorded on ``view`` and the
        runner has not folded yet (a ``once`` hook run, a PermissionRequest
        answer earlier in the same step)."""
        found = view.ext_state(NAME)
        st: HookState = found if isinstance(found, HookState) else HookState()
        for ev in view.pending:
            if isinstance(ev, ExtensionRecorded) and ev.extension == NAME:
                st = fold(st, ev.kind, ev.data)
        return st

    @staticmethod
    def cwd(view: TaskView) -> str | None:
        return view.cwd or None

    def payload(self, event: str, view: TaskView, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Claude Code's common input fields, then the event's own. The root
        task stands for Claude Code's conversation id."""
        st = view.state
        # The permissions plugin's current mode (its folded ``mode``).
        mode = getattr(view.ext_state(PERMISSIONS_PLUGIN), "mode", None) \
            or st.config.get("permission_mode")
        out: dict[str, Any] = {
            "session_id": st.root_id or st.task_id, "transcript_path": "",
            "cwd": self.cwd(view) or "", "permission_mode": mode if isinstance(mode, str)
            else "default", "hook_event_name": event, "task_id": st.task_id,
        }
        if st.parent_id is not None:
            out.update(agent_fields(view))
        out.update(payload)
        return out

    def _select(self, event: str, view: TaskView, payload: Mapping[str, Any],
                call: ToolUse | None) -> list[HookCommand]:
        """The hooks to run: matched on the event's subject, ``if`` met,
        identical ones once, ``once`` hooks that have not run."""
        subject_field = SUBJECT.get(event)
        subject = payload.get(subject_field) if subject_field else None
        done = self.state(view).once
        seen: set[str] = set()
        out: list[HookCommand] = []
        for m in self.matchers.get(event, ()):
            if not m.matches(subject if isinstance(subject, str) else None):
                continue
            for h in m.hooks:
                if h.key in seen:
                    continue
                if h.type == "http" and event == "SessionStart":
                    log.info("hooks: HTTP hooks do not run on SessionStart; %s skipped", h.url)
                    continue
                if h.condition is not None:
                    if call is None or event not in TOOL_EVENTS:
                        log.info("hooks: %s cannot test 'if' %r; the hook does not run",
                                 event, h.condition.text)
                        continue
                    cwd = self.cwd(view) or os.getcwd()
                    ctx = RuleContext(root=cwd, home=os.path.expanduser("~"), cwd=cwd)
                    if not match(h.condition, call.name, call.input, ctx):
                        continue
                if h.once and h.key in done:
                    continue
                seen.add(h.key)
                out.append(h)
        return out

    async def _one(self, hook: HookCommand, payload: dict[str, Any], cwd: str | None,
                   view: TaskView, transcript: list[Message] | None) -> HookResult:
        try:
            if hook.type == "http":
                return await run_http(hook, payload, self.http_transport)
            if hook.type == "prompt":
                return await run_prompt(hook, payload, view.complete, view.model, transcript)
            if hook.type == "agent":
                return await run_agent(hook, payload, view.complete, view.model,
                                       _read_only_tools(view),
                                       _tool_runner(view, "agent", checked=True), transcript)
            if hook.type == "mcp_tool":
                return await run_mcp_tool(hook, payload, _mcp_tool(view, hook),
                                          _tool_runner(view, "mcp"))
            return await self.runner(hook, payload, cwd)
        except Exception as exc:  # a broken hook is a failed hook
            log.exception("hook %r failed", hook.label)
            return HookResult(exit_code=-1, stderr=f"{type(exc).__name__}: {exc}")

    def spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def run(self, event: str, view: TaskView, payload: Mapping[str, Any], *,
                  call: ToolUse | None = None, transcript: list[Message] | None = None,
                  record: bool = True) -> list[Read]:
        """Run the event's hooks at once (``async`` ones in the background)
        and read their answers, in configuration order. ``record``: the
        person is told about failures and ``systemMessage``s, and ``once``
        hooks are remembered (off where records cannot be made)."""
        hooks = self._select(event, view, payload, call)
        if not hooks:
            return []
        full = self.payload(event, view, payload)
        cwd = self.cwd(view)
        for h in hooks:
            if h.once and record:
                view.record(NAME, "once", {"key": h.key})
        for h in hooks:
            if h.background:
                self.spawn(self._one(h, full, cwd, view, transcript))
        now = [h for h in hooks if not h.background]
        results = await asyncio.gather(*(self._one(h, full, cwd, view, transcript)
                                         for h in now))
        out: list[Read] = []
        for hook, result in zip(now, results, strict=True):
            if result.failed:
                log.warning("%s hook %r failed (exit %s): %s", event, hook.label,
                            result.exit_code, result.stderr.strip()[:500])
                if hook.block_on_failure and event not in ("Stop", "SubagentStop"):
                    result = HookResult(exit_code=2, stderr=result.stderr or "the hook failed")
                elif record:
                    view.notify(f"{event} hook failed: {result.stderr.strip()[:300]}")
            read = _read(event, result)
            message = read.data.get("systemMessage")
            if record and isinstance(message, str) and message.strip():
                view.notify(message.strip())
            out.append(read)
        return out

    def context(self, view: TaskView, kind: str, label: str, texts: Sequence[str]) -> None:
        """Record what hooks added for the model, as ``<label>: <text>``
        (after the open calls' results when calls are open)."""
        for text in texts:
            if text.strip():
                view.record(NAME, kind, {"hook": label},
                            message=user(f"{label}: {text.strip()}", origin="system"))

    async def stop_turn(self, view: TaskView, reason: str) -> None:
        """``continue: false``: the turn ends (the person sees why)."""
        if reason:
            view.notify(reason)
        if view.post is not None:
            await view.post(Cancel(reason=reason or "stopped by a hook", turn_only=True))
