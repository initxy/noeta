"""The ``Hooks`` extension: fires Claude Code's hook events at the matching
points of a task and acts on what the hooks answer."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
import msgspec

from noeta.core.errors import TurnFailed
from noeta.core.events import (
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
from noeta.core.extension import Ask, Deny, Extension, Gate, Reminder, TaskView
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.core.tool import ToolOutput
from noeta.core.types import Message, Text, ToolResult, ToolUse, user
from noeta.plugins.hooks.config import Matcher, parse_hooks, read_settings
from noeta.plugins.hooks.dispatch import (
    CONTEXT_LABEL,
    NAME,
    Dispatcher,
    HookState,
    Read,
    agent_fields,
    blocking_text,
    fold,
)
from noeta.plugins.hooks.gates import HANDBACK_TOOL, ToolGates
from noeta.plugins.hooks.runners import Runner, run_command
from noeta.toolkit.signals import RESUMED_SIGNAL

__all__ = ["HANDBACK_TOOL", "MAX_STOP_CONTINUATIONS", "RETRY_NOTE", "Hooks"]

log = logging.getLogger("noeta.hooks")

#: A Stop / SubagentStop hook that keeps blocking is let through after this
#: many in a row.
MAX_STOP_CONTINUATIONS = 8
#: What the model is told when a PermissionDenied hook allows a retry.
RETRY_NOTE = ("A PermissionDenied hook says the denied {tool} call may be retried, "
              "if it is still needed.")
#: The plugin whose ``cwd`` record says the shell changed directory.
SHELL_PLUGIN = "shell"
#: The plugin whose records (``start``, ``nested``, ``reread``) and state
#: name the instruction files it showed.
INSTRUCTIONS_PLUGIN = "instructions"
#: Its file kinds as Claude Code's ``memory_type``.
MEMORY_TYPES = {"user": "User", "project": "Project", "local": "Local", "managed": "Managed"}

#: Turn failures that are a model call's (Claude Code's StopFailure fires on
#: API errors, not on hooks or limits).
_MODEL_FAILURES = frozenset({"llm_error", "llm_empty_response", "llm_truncated",
                             "context_overflow"})
_FAILURE_KINDS = (
    (r"\b(401|403)\b|auth|api.?key", "authentication_failed"),
    (r"\b429\b|rate.?limit", "rate_limit"),
    (r"\b529\b|overloaded", "overloaded"),
    (r"\b404\b|model.*not.*found|not_found", "model_not_found"),
    (r"\b(400|413|422)\b|invalid", "invalid_request"),
    (r"\b5\d\d\b|server", "server_error"),
)


def _failure_kind(reason: str, detail: str) -> str:
    """A failed model call as Claude Code's StopFailure ``error`` kind."""
    if reason == "llm_truncated":
        return "max_output_tokens"
    if reason == "context_overflow":
        return "invalid_request"
    text = detail.lower()
    return next((kind for pattern, kind in _FAILURE_KINDS if re.search(pattern, text)),
                "unknown")


def _text(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, Text))


def _answered(messages: Sequence[Message]) -> list[Message]:
    """``messages`` without the calls that have no result yet (a sub-agent's
    hand-back being judged): a model refuses a call left unanswered."""
    results = {b.call_id for m in messages for b in m.content if isinstance(b, ToolResult)}
    out: list[Message] = []
    for m in messages:
        if m.role == "assistant" and any(u.id not in results for u in m.tool_uses):
            content = [b for b in m.content
                       if not (isinstance(b, ToolUse) and b.id not in results)]
            if not content:
                continue
            m = msgspec.structs.replace(m, content=content)
        out.append(m)
    return out


class Hooks(Extension):
    """Constructor arguments:

    * ``hooks`` — Claude Code's ``hooks`` object.
    * ``settings_files`` — settings files whose ``hooks`` are added (a file
      that is missing or unreadable adds nothing).
    * ``runner`` — runs one command hook (tests swap it).
    * ``http_transport`` — the transport HTTP hooks are sent through
      (tests swap it).
    """

    name = NAME

    def __init__(self, *, hooks: Mapping[str, Any] | None = None,
                 settings_files: Sequence[str | Path] = (),
                 runner: Runner = run_command,
                 http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.matchers: dict[str, list[Matcher]] = {}
        for config in [*(read_settings(f) for f in settings_files), hooks or {}]:
            for event, ms in parse_hooks(config).items():
                self.matchers.setdefault(event, []).extend(ms)
        self._d = Dispatcher(self.matchers, runner, http_transport)
        self._gates = ToolGates(self._d, self._stop)

    @property
    def runner(self) -> Runner:
        """Runs one command hook."""
        return self._d.runner

    @runner.setter
    def runner(self, runner: Runner) -> None:
        self._d.runner = runner

    @property
    def http_transport(self) -> httpx.AsyncBaseTransport | None:
        """The transport HTTP hooks are sent through."""
        return self._d.http_transport

    @http_transport.setter
    def http_transport(self, transport: httpx.AsyncBaseTransport | None) -> None:
        self._d.http_transport = transport

    # ------------------------------------------------------------ state

    def initial_state(self) -> HookState:
        return HookState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return fold(state, kind, data)

    # ------------------------------------------------------------ start

    async def on_start(self, view: TaskView) -> None:
        """SessionStart (``startup``) the first time a top-level task
        starts; SubagentStart the first time a sub-agent starts. What they
        add reaches the model. A pick-up by another worker or after a
        restart is not a resume: that is a front end's
        :data:`RESUMED_SIGNAL` (:meth:`on_event`)."""
        st = self._d.state(view)
        if "PostModelSwitch" in self.matchers and st.model is None:
            view.record(self.name, "model_seen", {"model": view.model})
        if view.state.parent_id is not None:
            if st.started or "SubagentStart" not in self.matchers:
                return
            view.record(self.name, "started", {"event": "SubagentStart"})
            reads = await self._d.run("SubagentStart", view, agent_fields(view))
            for r in reads:
                if r.block is not None:
                    view.notify(f"SubagentStart hook: {r.block}")
            self._d.context(view, "context", f"SubagentStart{CONTEXT_LABEL}",
                            [r.context for r in reads])
            return
        if "SessionStart" not in self.matchers or st.started:
            return
        view.record(self.name, "started", {"event": "SessionStart"})
        reads = await self._d.run("SessionStart", view, {"source": "startup", "model": view.model})
        self._start_context(view, "startup", reads)

    async def _resumed(self, view: TaskView) -> None:
        """SessionStart (``resume``): a front end reopened a started task."""
        if view.state.parent_id is not None or "SessionStart" not in self.matchers \
                or not self._d.state(view).started:
            return
        reads = await self._d.run("SessionStart", view, {"source": "resume", "model": view.model})
        self._start_context(view, "resume", reads)

    def _start_context(self, view: TaskView, source: str, reads: Sequence[Read]) -> None:
        for r in reads:
            if r.block is not None:
                view.notify(f"SessionStart hook: {r.block}")
        self._d.context(view, "context", f"SessionStart{CONTEXT_LABEL}",
                        [r.context or r.text for r in reads])

    # ------------------------------------------------------------ reminders

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """SessionStart (``compact``) after a compaction; UserPromptSubmit
        before the model call that first carries the person's new messages."""
        out: list[Reminder] = []
        compaction = view.state.compaction
        st = self._d.state(view)
        if compaction is not None and compaction.through != st.compact_seen \
                and view.state.parent_id is None and "SessionStart" in self.matchers:
            view.record(self.name, "compact_seen", {"through": compaction.through})
            reads = await self._d.run("SessionStart", view, {"source": "compact",
                                                              "model": view.model})
            for r in reads:
                if r.block is not None:
                    view.notify(f"SessionStart hook: {r.block}")
            out.extend(Reminder(text=f"SessionStart{CONTEXT_LABEL}: {t}", kind="context")
                       for t in (r.context or r.text for r in reads) if t)
        try:
            out.extend(await self._prompt_submit(view))
        except TurnFailed:
            # The compaction's context is recorded as seen: keep what it
            # added for the next turn's request.
            for kept in out:
                view.record(self.name, kept.kind, kept.data,
                            message=user(kept.text, origin=kept.origin))
            raise
        out.extend(await self._tool_batch(view))
        return out

    async def _tool_batch(self, view: TaskView) -> list[Reminder]:
        """PostToolBatch: once every call of the newest reply has its
        result, before the model sees them. What a hook adds (or blocks
        with) reaches the model; ``continue: false`` ends the turn."""
        if "PostToolBatch" not in self.matchers or not view.state.steps_in_turn:
            return []
        state = view.state
        at = next((i for i in range(len(state.messages) - 1, -1, -1)
                   if state.messages[i].role == "assistant"), None)
        if at is None or not state.messages[at].tool_uses:
            return []
        results = {b.call_id: b for m in state.messages[at + 1:] if m.role == "user"
                   for b in m.content if isinstance(b, ToolResult)}
        uses = state.messages[at].tool_uses
        through = state.message_seqs[at] if at < len(state.message_seqs) else 0
        if through <= self._d.state(view).batched_through or \
                any(u.id not in results for u in uses):
            return []
        view.record(self.name, "batched", {"through": through})
        reads = await self._d.run("PostToolBatch", view, {"tool_calls": [
            {"tool_name": u.name, "tool_input": u.input, "tool_use_id": u.id,
             "tool_response": results[u.id].text} for u in uses]})
        for r in reads:
            if r.stop is not None:
                raise TurnFailed("stopped_by_hook", r.stop or "a PostToolBatch hook stopped "
                                                              "the turn")
        out = [Reminder(text=blocking_text("PostToolBatch", r), kind="context")
               for r in reads if r.block and r.block.strip()]
        return out + [Reminder(text=f"PostToolBatch{CONTEXT_LABEL}: {r.context}", kind="context")
                      for r in reads if r.context]

    async def _prompt_submit(self, view: TaskView) -> list[Reminder]:
        # Before every model call, not only a turn's first: a message sent
        # mid-turn is checked before the model sees it, and once only.
        if "UserPromptSubmit" not in self.matchers:
            return []
        st = self._d.state(view)
        after = st.prompted_through
        if after is None:
            # First look at this task (the hook was added since, or the task
            # predates the record): what the model already answered stays.
            replied = [seq for seq, m in zip(view.state.message_seqs, view.state.messages,
                                             strict=False) if m.role == "assistant"]
            compaction = view.state.compaction
            after = max([0, *replied[-1:], *([compaction.through] if compaction else [])])
        new = [(seq, m) for seq, m in zip(view.state.message_seqs, view.state.messages,
                                           strict=False)
               if m.role == "user" and m.origin == "human" and seq > after]
        if not new:
            return []
        through = new[-1][0]
        prompt = "\n\n".join(m.text for _, m in new)
        reads = await self._d.run("UserPromptSubmit", view, {"prompt": prompt})
        view.record(self.name, "prompted", {"through": through})
        for r in reads:
            if r.block is not None:
                # A blocked prompt is erased, as Claude Code does: the next
                # turn's request never carries it.
                # What a command or skill expanded them into (``inline``
                # text recorded after them) goes too.
                first = new[0][0]
                view.erase_messages(sorted({seq for seq, m in zip(
                    view.state.message_seqs, view.state.messages, strict=False)
                    if seq >= first and m.role == "user" and m.origin in ("human", "inline")}))
                raise TurnFailed("blocked_by_hook", r.block)
            if r.stop is not None:
                raise TurnFailed("stopped_by_hook", r.stop or "a UserPromptSubmit hook "
                                                              "stopped the turn")
        return [Reminder(text=f"UserPromptSubmit{CONTEXT_LABEL}: {t}", kind="context")
                for t in (r.context or r.text for r in reads) if t]

    # ------------------------------------------------------------ tool calls

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        """PreToolUse: ``deny`` (or exit 2) denies the call, ``ask`` asks
        the person (firmly), ``allow`` vouches for it (a deny or ask rule
        still wins), ``updatedInput`` rewrites it. A sub-agent's hand-back
        is its SubagentStop: a block keeps it working."""
        return await self._gates.before_tool(view, call)

    async def resolve_ask(self, view: TaskView, call: ToolUse,
                          asks: Sequence[Ask]) -> Gate | None:
        """PermissionRequest: a hook answers in the person's place (``allow``,
        optionally with ``updatedInput``; ``deny`` with a ``message``, and
        ``interrupt`` to stop the turn). The answer is recorded, so a call
        gated again is not asked about twice."""
        return await self._gates.resolve_ask(view, call)


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
        reads = await self._d.run(event, view, payload, call=call)
        notes: list[str] = []
        for r in reads:
            notes.extend(n for n in (r.context,) if n)
            if event == "PostToolUse":
                replaced = r.specific.get("updatedToolOutput")
                if replaced is None and call.name.startswith("mcp__"):
                    replaced = r.specific.get("updatedMCPToolOutput")
                if replaced is not None:
                    shown = replaced if isinstance(replaced, str) else json.dumps(replaced)
                    output = msgspec.structs.replace(output, content=[Text(text=shown)])
            if r.stop is not None:
                await self._d.stop_turn(view, r.stop)
        self._d.blocks(view, "feedback", f"{event}:{call.name}", reads)
        self._d.context(view, "feedback", f"{event}:{call.name}{CONTEXT_LABEL}", notes)
        return output

    # ------------------------------------------------------------ compaction

    async def before_compact(self, view: TaskView, trigger: Literal["manual", "auto"],
                             instructions: str) -> str | Deny | None:
        """PreCompact: plain stdout is added to the summary instructions; a
        block skips the compaction."""
        if not {"PreCompact", "PostCompact"} & self.matchers.keys():
            return None
        view.record(self.name, "compacting", {"trigger": trigger})
        reads = await self._d.run("PreCompact", view, {
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
            await self._instructions_loaded(view, "compact", self._shown_files(view))
            if "PostCompact" in self.matchers:
                reads = await self._d.run("PostCompact", view, {
                    "trigger": self._d.state(view).compact_trigger,
                    "compact_summary": event.summary})
                for r in reads:
                    if r.text:
                        view.notify(r.text)
        elif isinstance(event, ExtensionRecorded):
            if event.extension == "permissions" and event.kind == "classified":
                await self._denied(view, event.data)
            elif event.extension == SHELL_PLUGIN and event.kind == "cwd":
                await self._cwd_changed(view, event.data)
            elif event.extension == INSTRUCTIONS_PLUGIN:
                await self._instructions_record(view, event.kind, event.data)
            elif event.extension == "core" and event.kind == "turn_failed":
                data = event.data if isinstance(event.data, dict) else {}
                await self._stop_failure(view, str(data.get("reason") or ""),
                                         str(data.get("detail") or ""))
        elif isinstance(event, TaskEnded):
            if event.status == "failed":
                reason, _, detail = event.error.partition(": ")
                await self._stop_failure(view, reason, detail)
            if view.state.parent_id is None and "SessionEnd" in self.matchers:
                await self._d.run("SessionEnd", view, {"reason": "other"}, record=False)
        elif isinstance(event, ModelReplied):
            await self._model_stopped(view, event)
        elif isinstance(event, InputTaken):
            signals = [i.body for i in event.items if isinstance(i.body, External)]
            if any(b.name == CONFIG_SIGNAL and isinstance(b.data, dict) and b.data.get("model")
                   for b in signals):
                await self._model_switched(view, event)
            if any(b.name == RESUMED_SIGNAL for b in signals):
                await self._resumed(view)

    async def _cwd_changed(self, view: TaskView, data: Any) -> None:
        """CwdChanged, when a command left the shell in another directory
        (the shell plugin's ``cwd`` record; empty: back at the start)."""
        if "CwdChanged" not in self.matchers or not isinstance(data, dict):
            return
        new = str(data.get("cwd") or "") or self._d.cwd(view) or ""
        old = self._d.state(view).cwd or self._d.cwd(view) or ""
        if new == old:
            return
        view.record(self.name, "cwd_seen", {"cwd": new})
        await self._d.run("CwdChanged", view, {"old_cwd": old, "new_cwd": new})

    @staticmethod
    def _shown_files(view: TaskView) -> list[dict[str, Any]]:
        """The instruction files the instructions plugin showed so far."""
        state = view.state.ext.get(INSTRUCTIONS_PLUGIN)
        if not isinstance(state, dict):
            return []
        return [e for e in [*state.get("files", []), *state.get("nested", [])]
                if isinstance(e, dict)]

    async def _instructions_record(self, view: TaskView, kind: str, data: Any) -> None:
        """InstructionsLoaded for the files an instructions record shows:
        at the start, those a touched file brings, and those shown again
        because they changed (Claude Code loads them again on a resume)."""
        if "InstructionsLoaded" not in self.matchers or not isinstance(data, dict):
            return
        if kind in ("start", "nested"):
            files = [e for e in data.get("files", []) if isinstance(e, dict)]
            trigger = data.get("trigger") if kind == "nested" else None
            await self._instructions_loaded(
                view, "session_start" if kind == "start" else "nested_traversal", files,
                trigger=trigger if isinstance(trigger, str) else None)
        elif kind == "reread":
            changed = data.get("digests") or {}
            again = {e["path"]: e for e in self._shown_files(view) if e.get("path") in changed}
            await self._instructions_loaded(view, "session_start", list(again.values()))

    async def _instructions_loaded(self, view: TaskView, reason: str,
                                   files: Sequence[dict[str, Any]], *,
                                   trigger: str | None = None) -> None:
        """InstructionsLoaded once per file, in order (observation only:
        its answer is not read). A file another one imports loads as
        ``include``; a ``paths:`` rule a touched file brings as
        ``path_glob_match``."""
        if "InstructionsLoaded" not in self.matchers:
            return
        for e in files:
            memory_type = MEMORY_TYPES.get(str(e.get("kind")))
            if memory_type is None or not e.get("path"):
                continue
            load_reason = reason
            if reason != "compact" and e.get("parent"):
                load_reason = "include"
            elif reason == "nested_traversal" and e.get("globs"):
                load_reason = "path_glob_match"
            payload: dict[str, Any] = {"file_path": e["path"], "memory_type": memory_type,
                                       "load_reason": load_reason}
            if e.get("globs"):
                payload["globs"] = list(e["globs"])
            if trigger and reason == "nested_traversal":
                payload["trigger_file_path"] = trigger
            if e.get("parent"):
                payload["parent_file_path"] = e["parent"]
            await self._d.run("InstructionsLoaded", view, payload)

    async def _stop_failure(self, view: TaskView, reason: str, detail: str) -> None:
        """StopFailure, when a turn ends on a model call that failed (not a
        hook or a limit): side effects only, its answer is not read."""
        if "StopFailure" not in self.matchers or reason not in _MODEL_FAILURES \
                or view.state.parent_id is not None:
            return
        last = next((_text(m) for m in reversed(view.state.messages)
                     if m.role == "assistant"), "")
        payload: dict[str, Any] = {"error": _failure_kind(reason, detail),
                                   "error_details": detail or reason}
        if last:
            payload["last_assistant_message"] = last
        await self._d.run("StopFailure", view, payload, record=False)

    async def _model_switched(self, view: TaskView, event: InputTaken) -> None:
        """PostModelSwitch, after a front end switched the task's model:
        what a hook adds reaches the model with its next request."""
        to = next(str(i.body.data["model"]) for i in event.items
                  if isinstance(i.body, External) and i.body.name == CONFIG_SIGNAL
                  and isinstance(i.body.data, dict) and i.body.data.get("model"))
        if "PostModelSwitch" not in self.matchers:
            return
        before = self._d.state(view).model
        view.record(self.name, "model_seen", {"model": to})
        reads = await self._d.run("PostModelSwitch", view, {
            "from_model": before or None, "to_model": to, "requested_model": to,
            "source": "sdk"})
        self._d.context(view, "context", f"PostModelSwitch{CONTEXT_LABEL}",
                        [r.context for r in reads])

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
        self._d.spawn(self._d.run("Notification", view, {
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
        reads = await self._d.run("PermissionDenied", view, {
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
        st = self._d.state(view)
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
            payload.update(agent_fields(view), agent_transcript_path="")
        reads = await self._d.run(event, view, payload, transcript=_answered(view.state.messages))
        if any(r.stop is not None for r in reads):
            return None
        reasons = [n for r in reads for n in (
            blocking_text(event, r) if r.block else None, r.context) if n]
        if not reasons:
            return None
        view.record(self.name, "stop_blocked", {"since": since, "event": event})
        return "\n\n".join(reasons)
