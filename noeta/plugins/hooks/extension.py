"""The ``Hooks`` extension: fires Claude Code's hook events at the matching
points of a task and acts on what the hooks answer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx
import msgspec

from noeta.core.errors import TurnFailed
from noeta.core.events import (
    Cancel,
    Compacted,
    ExtensionRecorded,
    External,
    InputTaken,
    ModelReplied,
    Recorded,
    Say,
    TaskEnded,
    Waiting,
)
from noeta.core.extension import Allow, Ask, Deny, Extension, Gate, Reminder, Rewrite, TaskView
from noeta.core.tool import ToolOutput
from noeta.core.types import Message, Text, ToolUse, user
from noeta.plugins.hooks.config import (
    SUBJECT,
    TOOL_EVENTS,
    HookCommand,
    Matcher,
    parse_hooks,
    read_settings,
)
from noeta.plugins.hooks.runners import (
    HookResult,
    Runner,
    run_command,
    run_http,
    run_prompt,
)
from noeta.toolkit.permission_rules import RuleContext, match
from noeta.toolkit.signals import RESUMED_SIGNAL

log = logging.getLogger("noeta.hooks")

#: A Stop / SubagentStop hook that keeps blocking is let through after this
#: many in a row.
MAX_STOP_CONTINUATIONS = 8
#: The tool a sub-agent ends its run with (a model-visible name).
HANDBACK_TOOL = "SubagentHandback"
#: What the model is told when a PermissionDenied hook allows a retry.
RETRY_NOTE = ("A PermissionDenied hook says the denied {tool} call may be retried, "
              "if it is still needed.")
_KEEP = 256


@dataclass(frozen=True)
class _State:
    #: Seq of the newest person's message the prompt hooks have seen.
    prompted_through: int = 0
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
    #: The permission mode, as the permissions plugin last recorded it.
    mode: str | None = None


@dataclass(frozen=True)
class _Read:
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


def _read(event: str, result: HookResult) -> _Read:
    if result.exit_code == 2:
        return _Read(block=result.stderr.strip() or "blocked by a hook")
    if result.exit_code != 0:
        return _Read()
    data = result.data
    if data is None:
        return _Read(text=result.text)
    specific = data.get("hookSpecificOutput")
    specific = specific if isinstance(specific, dict) else {}
    named = specific.get("hookEventName")
    if named is not None and named != event:
        log.warning("hooks: a %s hook answered for %r; its answer is ignored", event, named)
        return _Read()
    block = str(data.get("reason") or "blocked by a hook") \
        if data.get("decision") == "block" else None
    stop = str(data.get("stopReason") or "") if data.get("continue") is False else None
    return _Read(block=block, stop=stop, specific=specific, data=data)


def _agent(view: TaskView) -> dict[str, str]:
    """A sub-agent's ``agent_id`` and ``agent_type``: the id and type the
    spawning tool put in its config (``agent_id`` / ``subagent_type``), else
    the task's id and agent."""
    config = view.state.config
    agent_id, kind = config.get("agent_id"), config.get("subagent_type")
    return {"agent_id": agent_id if isinstance(agent_id, str) and agent_id else view.task_id,
            "agent_type": kind if isinstance(kind, str) and kind else view.state.agent}


def _text(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, Text))


def _remember(cache: OrderedDict[Any, Any], key: Any, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _KEEP:
        cache.popitem(last=False)


class Hooks(Extension):
    """Constructor arguments:

    * ``hooks`` — Claude Code's ``hooks`` object.
    * ``settings_files`` — settings files whose ``hooks`` are added (a file
      that is missing or unreadable adds nothing).
    * ``runner`` — runs one command hook (tests swap it).
    * ``http_transport`` — the transport HTTP hooks are sent through
      (tests swap it).
    """

    name = "hooks"

    def __init__(self, *, hooks: Mapping[str, Any] | None = None,
                 settings_files: Sequence[str | Path] = (),
                 runner: Runner = run_command,
                 http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.matchers: dict[str, list[Matcher]] = {}
        for config in [*(read_settings(f) for f in settings_files), hooks or {}]:
            for event, ms in parse_hooks(config).items():
                self.matchers.setdefault(event, []).extend(ms)
        self.runner = runner
        self.http_transport = http_transport
        self._background: set[asyncio.Future[Any]] = set()
        #: (task id, call id) -> (the input a PreToolUse hook rewrote the
        #: call to, its verdict on the call): the rewritten call is gated
        #: again, and the hook is not asked about its own rewrite.
        self._rewrites: OrderedDict[tuple[str, str], tuple[dict[str, Any], Gate | None]] = \
            OrderedDict()
        #: PermissionRequest answers not yet folded into the task's state.
        self._decisions: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        #: ``once`` hooks run in a step whose records are not folded yet.
        self._ran_once: OrderedDict[tuple[str, str], bool] = OrderedDict()

    # ------------------------------------------------------------ state

    def initial_state(self) -> _State:
        return _State()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = state if isinstance(state, _State) else _State()
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
        if kind == "mode":
            return replace(st, mode=str(data.get("mode") or "") or None)
        if kind == "once":
            return replace(st, once=st.once | {str(data.get("key"))})
        if kind == "permission":
            decided = dict(st.permission)
            decided[str(data.get("call_id"))] = {k: v for k, v in data.items() if k != "call_id"}
            while len(decided) > _KEEP:
                decided.pop(next(iter(decided)))
            return replace(st, permission=decided)
        return st

    def _state(self, view: TaskView) -> _State:
        st = view.ext_state(self.name)
        return st if isinstance(st, _State) else _State()

    # ------------------------------------------------------------ running

    @staticmethod
    def _cwd(view: TaskView) -> str | None:
        return view.cwd or None

    def _payload(self, event: str, view: TaskView, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Claude Code's common input fields, then the event's own. The root
        task stands for Claude Code's conversation id."""
        st = view.state
        mode = self._state(view).mode or st.config.get("permission_mode")
        out: dict[str, Any] = {
            "session_id": st.root_id or st.task_id, "transcript_path": "",
            "cwd": self._cwd(view) or "", "permission_mode": mode if isinstance(mode, str)
            else "default", "hook_event_name": event, "task_id": st.task_id,
        }
        if st.parent_id is not None:
            out.update(_agent(view))
        out.update(payload)
        return out

    def _select(self, event: str, view: TaskView, payload: Mapping[str, Any],
                call: ToolUse | None) -> list[HookCommand]:
        """The hooks to run: matched on the event's subject, ``if`` met,
        identical ones once, ``once`` hooks that have not run."""
        subject_field = SUBJECT.get(event)
        subject = payload.get(subject_field) if subject_field else None
        done = self._state(view).once
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
                    cwd = self._cwd(view) or os.getcwd()
                    ctx = RuleContext(root=cwd, home=os.path.expanduser("~"), cwd=cwd)
                    if not match(h.condition, call.name, call.input, ctx):
                        continue
                if h.once and (h.key in done or (view.task_id, h.key) in self._ran_once):
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
            return await self.runner(hook, payload, cwd)
        except Exception as exc:  # a broken hook is a failed hook
            log.exception("hook %r failed", hook.label)
            return HookResult(exit_code=-1, stderr=f"{type(exc).__name__}: {exc}")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _run(self, event: str, view: TaskView, payload: Mapping[str, Any], *,
                   call: ToolUse | None = None, transcript: list[Message] | None = None,
                   record: bool = True) -> list[_Read]:
        """Run the event's hooks at once (``async`` ones in the background)
        and read their answers, in configuration order. ``record``: the
        person is told about failures and ``systemMessage``s, and ``once``
        hooks are remembered (off where records cannot be made)."""
        hooks = self._select(event, view, payload, call)
        if not hooks:
            return []
        full = self._payload(event, view, payload)
        cwd = self._cwd(view)
        for h in hooks:
            if h.once:
                _remember(self._ran_once, (view.task_id, h.key), True)
                if record:
                    view.record(self.name, "once", {"key": h.key})
        for h in hooks:
            if h.background:
                self._spawn(self._one(h, full, cwd, view, transcript))
        now = [h for h in hooks if not h.background]
        results = await asyncio.gather(*(self._one(h, full, cwd, view, transcript)
                                         for h in now))
        out: list[_Read] = []
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

    def _context(self, view: TaskView, kind: str, label: str, texts: Sequence[str]) -> None:
        """Record what hooks added for the model, as ``<label>: <text>``
        (after the open calls' results when calls are open)."""
        for text in texts:
            if text.strip():
                view.record(self.name, kind, {"hook": label},
                            message=user(f"{label}: {text.strip()}", origin="system"))

    async def _stop_turn(self, view: TaskView, reason: str) -> None:
        """``continue: false``: the turn ends (the person sees why)."""
        if reason:
            view.notify(reason)
        if view.post is not None:
            await view.post(Cancel(reason=reason or "stopped by a hook", turn_only=True))

    # ------------------------------------------------------------ start

    async def on_start(self, view: TaskView) -> None:
        """SessionStart (``startup``) the first time a top-level task
        starts; SubagentStart the first time a sub-agent starts. What they
        add reaches the model. A pick-up by another worker or after a
        restart is not a resume: that is a front end's
        :data:`RESUMED_SIGNAL` (:meth:`on_event`)."""
        st = self._state(view)
        if view.state.parent_id is not None:
            if st.started or "SubagentStart" not in self.matchers:
                return
            view.record(self.name, "started", {"event": "SubagentStart"})
            reads = await self._run("SubagentStart", view, _agent(view))
            for r in reads:
                if r.block is not None:
                    view.notify(f"SubagentStart hook: {r.block}")
            self._context(view, "context", "SubagentStart hook", [r.context for r in reads])
            return
        if "SessionStart" not in self.matchers or st.started:
            return
        view.record(self.name, "started", {"event": "SessionStart"})
        reads = await self._run("SessionStart", view, {"source": "startup", "model": view.model})
        self._start_context(view, "startup", reads)

    async def _resumed(self, view: TaskView) -> None:
        """SessionStart (``resume``): a front end reopened a started task."""
        if view.state.parent_id is not None or "SessionStart" not in self.matchers \
                or not self._state(view).started:
            return
        reads = await self._run("SessionStart", view, {"source": "resume", "model": view.model})
        self._start_context(view, "resume", reads)

    def _start_context(self, view: TaskView, source: str, reads: Sequence[_Read]) -> None:
        for r in reads:
            if r.block is not None:
                view.notify(f"SessionStart hook: {r.block}")
        self._context(view, "context", f"SessionStart hook ({source})",
                      [r.context or r.text for r in reads])

    # ------------------------------------------------------------ reminders

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """SessionStart (``compact``) after a compaction; UserPromptSubmit
        before the first model call of a turn."""
        out: list[Reminder] = []
        compaction = view.state.compaction
        st = self._state(view)
        if compaction is not None and compaction.through != st.compact_seen \
                and view.state.parent_id is None and "SessionStart" in self.matchers:
            view.record(self.name, "compact_seen", {"through": compaction.through})
            reads = await self._run("SessionStart", view, {"source": "compact",
                                                           "model": view.model})
            for r in reads:
                if r.block is not None:
                    view.notify(f"SessionStart hook: {r.block}")
            out.extend(Reminder(text=f"SessionStart hook (compact): {t}", kind="context")
                       for t in (r.context or r.text for r in reads) if t)
        out.extend(await self._prompt_submit(view))
        return out

    async def _prompt_submit(self, view: TaskView) -> list[Reminder]:
        if "UserPromptSubmit" not in self.matchers or view.state.steps_in_turn:
            return []
        st = self._state(view)
        new = [(seq, m) for seq, m in zip(view.state.message_seqs, view.state.messages,
                                           strict=False)
               if m.role == "user" and m.origin == "human" and seq > st.prompted_through]
        if not new:
            return []
        through = new[-1][0]
        prompt = "\n\n".join(m.text for _, m in new)
        reads = await self._run("UserPromptSubmit", view, {"prompt": prompt})
        view.record(self.name, "prompted", {"through": through})
        for r in reads:
            if r.block is not None:
                raise TurnFailed("blocked_by_hook", r.block)
            if r.stop is not None:
                raise TurnFailed("stopped_by_hook", r.stop or "a UserPromptSubmit hook "
                                                              "stopped the turn")
        return [Reminder(text=t, kind="context") for t in (r.context or r.text for r in reads)
                if t]

    # ------------------------------------------------------------ tool calls

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        """PreToolUse: ``deny`` (or exit 2) denies the call, ``ask`` asks
        the person (firmly), ``allow`` vouches for it (a deny or ask rule
        still wins), ``updatedInput`` rewrites it. A sub-agent's hand-back
        is its SubagentStop: a block keeps it working."""
        rewrite = self._rewrites.get((view.task_id, call.id))
        if rewrite is not None and rewrite[0] == call.input:
            return rewrite[1]
        if call.name == HANDBACK_TOOL and view.state.parent_id is not None:
            message = call.input.get("message")
            reason = await self._stop("SubagentStop", view,
                                      message if isinstance(message, str) else "")
            if reason is not None:
                return Deny(reason=reason)
        reads = await self._run("PreToolUse", view, {
            "tool_name": call.name, "tool_input": call.input, "tool_use_id": call.id}, call=call)
        decision: str | None = None
        reason = ""
        updated: dict[str, Any] | None = None
        contexts: list[str] = []
        for r in reads:
            if r.stop is not None:
                await self._stop_turn(view, r.stop)
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
        self._context(view, "context", f"PreToolUse hook ({call.name})", contexts)
        verdict: Gate | None = None
        if decision == "ask":
            verdict = Ask(prompt=reason or f"A hook asks to confirm {call.name}", firm=True)
        elif decision == "allow":
            verdict = Allow(approve=True)
        if updated is not None and updated != call.input:
            _remember(self._rewrites, (view.task_id, call.id), (updated, verdict))
            return Rewrite(input=updated)
        return verdict

    async def resolve_ask(self, view: TaskView, call: ToolUse,
                          asks: Sequence[Ask]) -> Gate | None:
        """PermissionRequest: a hook answers in the person's place (``allow``,
        optionally with ``updatedInput``; ``deny`` with a ``message``, and
        ``interrupt`` to stop the turn). The answer is recorded, so a call
        gated again is not asked about twice."""
        if "PermissionRequest" not in self.matchers:
            return None
        decided = self._state(view).permission.get(call.id) \
            or self._decisions.get((view.task_id, call.id))
        if decided is None:
            decided = await self._permission_request(view, call)
            _remember(self._decisions, (view.task_id, call.id), decided)
            view.record(self.name, "permission", {"call_id": call.id, **decided})
            if decided.get("behavior") == "deny" and decided.get("interrupt"):
                await self._stop_turn(view, "")
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
        reads = await self._run("PermissionRequest", view, {
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

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        """PostToolUse (PostToolUseFailure for an error result): a block or
        ``additionalContext`` reaches the model after the result;
        ``updatedToolOutput`` replaces the result."""
        text = "".join(getattr(b, "text", "") for b in output.content)
        base = {"tool_name": call.name, "tool_input": call.input, "tool_use_id": call.id}
        if output.is_error:
            event = "PostToolUseFailure"
            payload = {**base, "error": text, "is_interrupt": False}
        else:
            event = "PostToolUse"
            payload = {**base, "tool_response": {"text": text, "is_error": False}}
        reads = await self._run(event, view, payload, call=call)
        notes: list[str] = []
        for r in reads:
            notes.extend(n for n in (r.block, r.context) if n)
            if event == "PostToolUse":
                replaced = r.specific.get("updatedToolOutput")
                if replaced is None and call.name.startswith("mcp__"):
                    replaced = r.specific.get("updatedMCPToolOutput")
                if replaced is not None:
                    shown = replaced if isinstance(replaced, str) else json.dumps(replaced)
                    output = msgspec.structs.replace(output, content=[Text(text=shown)])
            if r.stop is not None:
                await self._stop_turn(view, r.stop)
        self._context(view, "feedback", f"{event} hook ({call.name})", notes)
        return output

    # ------------------------------------------------------------ compaction

    async def before_compact(self, view: TaskView, trigger: Literal["manual", "auto"],
                             instructions: str) -> str | Deny | None:
        """PreCompact: plain stdout is added to the summary instructions; a
        block skips the compaction."""
        if not {"PreCompact", "PostCompact"} & self.matchers.keys():
            return None
        view.record(self.name, "compacting", {"trigger": trigger})
        reads = await self._run("PreCompact", view, {
            "trigger": trigger, "custom_instructions": instructions or None})
        added: list[str] = []
        for r in reads:
            if r.block is not None:
                return Deny(reason=r.block)
            if r.text:
                added.append(r.text)
        return "\n\n".join(added) or None

    # ------------------------------------------------------------ events

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        event = recorded.event
        if isinstance(event, Waiting) and event.on in ("approval", "question"):
            self._notify(view, event)
        elif isinstance(event, Compacted):
            if "PostCompact" in self.matchers:
                reads = await self._run("PostCompact", view, {
                    "trigger": self._state(view).compact_trigger,
                    "compact_summary": event.summary})
                for r in reads:
                    if r.text:
                        view.notify(r.text)
        elif isinstance(event, ExtensionRecorded):
            if event.extension == "permissions" and event.kind == "classified":
                await self._denied(view, event.data)
            elif event.extension == "permissions" and event.kind in ("mode", "plan_exit") \
                    and isinstance(event.data, dict):
                view.record(self.name, "mode", {"mode": event.data.get("mode")})
        elif isinstance(event, TaskEnded):
            if view.state.parent_id is None and "SessionEnd" in self.matchers:
                await self._run("SessionEnd", view, {"reason": "other"}, record=False)
        elif isinstance(event, ModelReplied):
            await self._model_stopped(view, event)
        elif isinstance(event, InputTaken) and any(
                isinstance(i.body, External) and i.body.name == RESUMED_SIGNAL
                for i in event.items):
            await self._resumed(view)

    def _notify(self, view: TaskView, event: Waiting) -> None:
        """Notification, in the background (side effects only)."""
        if "Notification" not in self.matchers:
            return
        if event.on == "approval":
            names = [c.use.name for c in view.state.calls if c.use.id in event.ids]
            message = f"Noeta needs your permission to use {', '.join(names) or 'a tool'}"
            kind = "permission_prompt"
        else:
            message, kind = "Noeta has a question for you", "elicitation_dialog"
        self._spawn(self._run("Notification", view, {
            "message": message, "notification_type": kind}, record=False))

    async def _denied(self, view: TaskView, data: Any) -> None:
        """PermissionDenied, after the auto-mode classifier denied a call:
        ``retry`` tells the model it may try again."""
        if "PermissionDenied" not in self.matchers or not isinstance(data, dict) \
                or data.get("decision") != "deny":
            return
        wanted = data.get("call_id")
        call = next((u for m in reversed(view.state.messages) if m.role == "assistant"
                     for u in m.tool_uses if u.id == wanted), None)
        if call is None:
            return
        reads = await self._run("PermissionDenied", view, {
            "tool_name": call.name, "tool_input": call.input, "tool_use_id": call.id,
            "reason": str(data.get("reason") or "")}, call=call)
        if any(r.specific.get("retry") is True for r in reads):
            view.record(self.name, "retry", {"call_id": call.id},
                        message=user(RETRY_NOTE.format(tool=call.name), origin="system"))

    async def _model_stopped(self, view: TaskView, event: ModelReplied) -> None:
        """Stop (SubagentStop in a sub-agent) when the model ends its turn:
        a block or ``additionalContext`` keeps it going, with that as its
        next message."""
        response = event.response
        if response.message.tool_uses or response.stop_reason != "end_turn":
            return
        name = "SubagentStop" if view.state.parent_id is not None else "Stop"
        reason = await self._stop(name, view, _text(response.message))
        if reason is not None and view.post is not None:
            await view.post(Say(message=user(reason, origin="system"), when="now"))

    async def _stop(self, event: str, view: TaskView, last: str) -> str | None:
        """Run Stop / SubagentStop hooks; the reason to go on, or ``None``."""
        if event not in self.matchers:
            return None
        st = self._state(view)
        since = max((seq for seq, m in zip(view.state.message_seqs, view.state.messages,
                                            strict=False)
                     if m.role == "user" and m.origin == "human"), default=0)
        blocks = st.stop_blocks if since == st.stop_since else 0
        if blocks >= MAX_STOP_CONTINUATIONS:
            log.warning("task %s: %s hooks blocked %d times in a row; letting it stop",
                        view.task_id, event, blocks)
            return None
        payload: dict[str, Any] = {"stop_hook_active": blocks > 0,
                                   "last_assistant_message": last}
        if event == "SubagentStop":
            payload.update(_agent(view), agent_transcript_path="")
        reads = await self._run(event, view, payload, transcript=list(view.state.messages))
        if any(r.stop is not None for r in reads):
            return None
        reasons = [n for r in reads for n in (r.block, r.context) if n]
        if not reasons:
            return None
        view.record(self.name, "stop_blocked", {"since": since, "event": event})
        return "\n\n".join(reasons)
