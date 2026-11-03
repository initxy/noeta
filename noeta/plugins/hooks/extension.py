"""The ``Hooks`` extension: fires Claude Code's hook events at the matching
points of a task and acts on what the hooks answer."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
import msgspec

from noeta.core import records
from noeta.core.client import CONTINUED_AS, CONTINUED_AS_KEY
from noeta.core.errors import TurnDone, TurnFailed
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
from noeta.core.loop.state import CONFIG_SIGNAL, TaskState
from noeta.core.tool import ToolOutput
from noeta.core.types import Message, Text, ToolResult, ToolUse, user
from noeta.plugins.hooks.config import HookCommand, Matcher, parse_hooks, read_settings
from noeta.plugins.hooks.dispatch import (
    ASYNC_SIGNAL,
    CONTEXT_LABEL,
    NAME,
    PROMPT_STOPPED,
    STOPPED_DEFAULT,
    SUCCESS_LABEL,
    Dispatcher,
    HookState,
    Read,
    agent_fields,
    blocking_text,
    fold,
    start_repeat,
)
from noeta.plugins.hooks.gates import HANDBACK_TOOL, ToolGates
from noeta.plugins.hooks.runners import Runner, run_command
from noeta.toolkit.hook_callbacks import CLIENT_HOOKS_KEY, HookCallbacks
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


#: A Stop hook's ``continue: false`` without a reason (Claude Code's).
STOPPED_STOP = "Stop hook prevented continuation"


def _for_model(event: str, reads: Sequence[Read]) -> list[str]:
    """What SessionStart / UserPromptSubmit hooks add for the model, each as
    Claude Code leads it: ``additionalContext`` as additional context, plain
    stdout as the hook's success."""
    return [text for text, _raw in _given(event, reads)]


def _given(event: str, reads: Sequence[Read]) -> list[tuple[str, str]]:
    """:func:`_for_model`'s texts, each with the hook's own words in it."""
    out: list[tuple[str, str]] = []
    for r in reads:
        if r.context:
            out.append((f"{event}{CONTEXT_LABEL}: {r.context}", r.context.strip()))
        elif r.text.strip():
            out.append((f"{event}{SUCCESS_LABEL}: {r.text.strip()}", r.text.strip()))
    return out


class _PromptStopped(Exception):
    """A UserPromptSubmit hook answered ``continue: false``."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def _start_data(text: str, raw: str) -> dict[str, Any]:
    """A SessionStart output's record: kept to tell a background answer that
    only repeats it (:func:`start_repeat`)."""
    return {"hook": text.split(": ", 1)[0], "event": "SessionStart", "texts": [raw]}


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


def _prompt_tool_matcher(name: str) -> Matcher:
    """The PermissionRequest hook ``--permission-prompt-tool`` stands for:
    the MCP tool ``name`` answers, and a failure denies."""
    server, sep, tool = name.removeprefix("mcp__").partition("__")
    if not name.startswith("mcp__") or not sep or not server or not tool:
        raise ValueError(f"permission_prompt_tool {name!r} is not an MCP tool name "
                         "(mcp__<server>__<tool>)")
    return Matcher("", (HookCommand(type="mcp_tool", server=server, tool=tool,
                                    permission_prompt=True, block_on_failure=True),))


class Hooks(Extension):
    """Constructor arguments:

    * ``hooks`` — Claude Code's ``hooks`` object.
    * ``settings_files`` — settings files whose ``hooks`` are added (a file
      that is missing or unreadable adds nothing).
    * ``runner`` — runs one command hook (tests swap it).
    * ``http_transport`` — the transport HTTP hooks are sent through
      (tests swap it).
    * ``callbacks`` — the hooks a connected client registers and answers
      (``noeta.toolkit.hook_callbacks``; Claude Code's SDK hook callbacks).
    * ``permission_prompt_tool`` — an MCP tool (``mcp__<server>__<tool>``)
      that answers every permission request in the person's place, as
      Claude Code's ``--permission-prompt-tool``: it is called with
      ``tool_name``, ``input`` and ``tool_use_id`` and answers
      ``{"behavior": "allow", "updatedInput"?}`` or ``{"behavior": "deny",
      "message"}`` as JSON text; any other answer denies the call.
    """

    name = NAME

    def __init__(self, *, hooks: Mapping[str, Any] | None = None,
                 settings_files: Sequence[str | Path] = (),
                 runner: Runner = run_command,
                 http_transport: httpx.AsyncBaseTransport | None = None,
                 callbacks: HookCallbacks | None = None,
                 permission_prompt_tool: str | None = None) -> None:
        self.matchers: dict[str, list[Matcher]] = {}
        for config in [*(read_settings(f) for f in settings_files), hooks or {}]:
            for event, ms in parse_hooks(config).items():
                self.matchers.setdefault(event, []).extend(ms)
        if permission_prompt_tool:
            self.matchers.setdefault("PermissionRequest", []).append(
                _prompt_tool_matcher(permission_prompt_tool))
        self._d = Dispatcher(self.matchers, runner, http_transport)
        self._gates = ToolGates(self._d, self._stop)
        self._client_matchers: list[tuple[str, Matcher]] = []
        if callbacks is not None:
            self._d.callbacks = callbacks
            callbacks.listen(self._client_hooks)

    def _client_hooks(self, hooks: dict[str, Any]) -> None:
        """The connected client's hooks replace the ones it registered
        before (the shared ``matchers`` are changed in place)."""
        for event, old in self._client_matchers:
            group = self.matchers.get(event, [])
            if old in group:
                group.remove(old)
            if not group:
                self.matchers.pop(event, None)
        self._client_matchers = []
        for event, ms in parse_hooks(hooks, callbacks=True).items():
            self.matchers.setdefault(event, []).extend(ms)
            self._client_matchers += [(event, m) for m in ms]

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

    def inherit(self, state: TaskState) -> Mapping[str, Any]:
        """A headless client's hook callbacks judge its child tasks too: they
        carry the record, so a worker that cannot ask the client refuses
        what they would judge."""
        record = state.config.get(CLIENT_HOOKS_KEY)
        return {CLIENT_HOOKS_KEY: record} if record is not None else {}

    def initial_state(self) -> HookState:
        return HookState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return fold(state, kind, data)

    def continue_state(self, state: Any) -> Any:
        """A continuing task (a fork, a resumed conversation) checks its own
        prompts: the positions here are seqs of the source's log (the
        carried messages sit at 0), so a prompt hook would otherwise skip
        the new log's first prompts. What belongs to the source's run (its
        stop count, permission answers, unsent background output, a stop
        note) stays there: background hook output the source had not given
        the model yet, and a hook's note that the source's turn was stopped,
        are not carried. Its first start fires SessionStart with the source
        Claude Code gives a resumed or forked session (:meth:`on_start`)."""
        if not isinstance(state, HookState):
            return state
        return dataclasses.replace(state, prompted_through=0, batched_through=0, stop_blocks=0,
                                   stop_since=0, permission={}, async_pending=(),
                                   stop_note=None, continued=True)

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
        if st.continued:
            # A continued conversation (``history_from``): Claude Code's
            # resume / fork sources; a rewind fires none. Only before the
            # task's own first reply: a continuation that ran before this
            # rule existed folds to ``continued`` too, mid-conversation.
            source = view.state.config.get(CONTINUED_AS_KEY) or "fork"
            if source not in CONTINUED_AS:
                log.warning("hooks: %s %r is none of %s; taken as fork", CONTINUED_AS_KEY,
                            source, ", ".join(CONTINUED_AS))
                source = "fork"
            view.record(self.name, "started", {"event": "SessionStart"})
            replied = any(m.role == "assistant" and seq > 0 for seq, m in
                          zip(view.state.message_seqs, view.state.messages, strict=False))
            if source in ("resume", "fork") and not replied \
                    and "SessionStart" in self.matchers:
                reads = await self._d.run("SessionStart", view,
                                          {"source": source, "model": view.model})
                self._start_context(view, source, reads)
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
        for text, raw in _given("SessionStart", reads):
            view.record(self.name, "context", _start_data(text, raw),
                        message=user(text, origin="system"))

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
            out.extend(Reminder(text=t, kind="context", data=_start_data(t, raw))
                       for t, raw in _given("SessionStart", reads))
        try:
            out.extend(await self._prompt_submit(view))
        except (TurnFailed, _PromptStopped) as ended:
            # The compaction's context is recorded as seen: keep what it
            # added for the next turn's request.
            for kept in out:
                view.record(self.name, kept.kind, kept.data,
                            message=user(kept.text, origin=kept.origin))
            if isinstance(ended, TurnFailed):
                raise
            # As Claude Code: the prompt stays, the model is told the hook
            # stopped it (and why), and the turn ends without a model call.
            view.record(self.name, "prompt_stopped", {"hook": "UserPromptSubmit"},
                        message=user(ended.text, origin="system"))
            view.notify(ended.text)
            raise TurnDone from None
        out.extend(await self._tool_batch(view))
        out.extend(Reminder(text=text, kind="async_delivered", data={"id": key})
                   for key, text in st.async_pending)
        if st.stop_note:
            # The turn the note was for ended some other way.
            out.append(Reminder(text=st.stop_note, kind="stopped",
                                data={"hook": st.stop_note.split(": ", 1)[0]}))
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
        # A block or ``continue: false`` ends the turn, the model told why
        # (the first reason given), as Claude Code ends it.
        reasons = [r.block.strip() if r.block is not None else (r.stop or "")
                   for r in reads if r.block is not None or r.stop is not None]
        if reasons:
            reason = next((x for x in reasons if x),
                          STOPPED_DEFAULT.format(event="PostToolBatch"))
            self._d.stopped(view, "PostToolBatch", reason)
            raise TurnFailed("stopped_by_hook", reason)
        return [Reminder(text=f"PostToolBatch{CONTEXT_LABEL}: {r.context}", kind="context")
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
                raise _PromptStopped(f"{PROMPT_STOPPED}: {r.stop}" if r.stop
                                     else PROMPT_STOPPED)
        return [Reminder(text=t, kind="context") for t in _for_model("UserPromptSubmit", reads)]

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
                self._d.stopped(view, f"{event}:{call.name}",
                                r.stop or STOPPED_DEFAULT.format(event=event), later=True)
                await self._d.stop_turn(view, r.stop)
                break
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
            elif event.extension == "core" and event.kind in ("turn_stopped", "turn_failed"):
                self._d.stop_told(view)
            if event.extension == "core" and event.kind == "turn_done":
                data = event.data if isinstance(event.data, dict) else {}
                if data.get("reason") == records.EMPTY_REPLY:
                    await self._turn_ended(view, None)
            if event.extension == "core" and event.kind == "turn_failed":
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
            for b in signals:
                if b.name == ASYNC_SIGNAL and isinstance(b.data, dict):
                    if b.data.get("event") == "SessionStart" and start_repeat(
                            self._d.state(view), str(b.data.get("command") or ""),
                            [str(t) for t in b.data.get("texts") or ()]):
                        # Claude Code leaves out a background SessionStart
                        # answer the model already has from an earlier run.
                        log.info("hooks: background SessionStart output of %s repeats "
                                 "an earlier one; not given again", b.data.get("command"))
                        continue
                    view.record(self.name, "async_response", b.data)

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
        await self._turn_ended(view, _text(response.message))

    async def _turn_ended(self, view: TaskView, last: str | None) -> None:
        """Stop (SubagentStop in a sub-agent) at the end of a turn; ``last``
        is ``None`` when the turn ended on an empty reply."""
        name = "SubagentStop" if view.state.parent_id is not None else "Stop"
        reason = await self._stop(name, view, last)
        if reason is not None and view.post is not None:
            await view.post(Say(message=user(reason, origin="system"), when="now"))

    async def _stop(self, event: str, view: TaskView, last: str | None) -> str | None:
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
        payload: dict[str, Any] = {"stop_hook_active": blocks > 0}
        if last is not None:
            payload["last_assistant_message"] = last
        if event == "SubagentStop":
            payload.update(agent_fields(view), agent_transcript_path="")
        reads = await self._d.run(event, view, payload, transcript=_answered(view.state.messages))
        stop = next((r.stop for r in reads if r.stop is not None), None)
        if stop is not None:
            # Claude Code names it the Stop hook in a sub-agent too.
            self._d.stopped(view, "Stop", stop or STOPPED_STOP)
            return None
        reasons = [n for r in reads for n in (
            blocking_text(event, r) if r.block else None, r.context) if n]
        if not reasons:
            return None
        view.record(self.name, "stop_blocked", {"since": since, "event": event})
        return "\n\n".join(reasons)
