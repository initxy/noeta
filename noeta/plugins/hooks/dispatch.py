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

from noeta.core.events import Cancel, ExtensionRecorded, External, Say
from noeta.core.extension import TaskView
from noeta.core.large_output import (
    large_output_message,
    mark_spill_dir,
    spill_path,
    task_spill_dir,
)
from noeta.core.mcp_names import legacy_names, warn_legacy
from noeta.core.tool import Tool, ToolContext, ToolOutput
from noeta.core.types import Message, Text, ToolUse, user
from noeta.plugins.hooks.config import SUBJECT, TOOL_EVENTS, HookCommand, Matcher
from noeta.plugins.hooks.runners import (
    HookResult,
    Runner,
    RunTool,
    run_agent,
    run_callback,
    run_http,
    run_mcp_tool,
    run_prompt,
)
from noeta.toolkit.hook_callbacks import CLIENT_HOOKS_KEY, HookCallbacks
from noeta.toolkit.inline_commands import refusal
from noeta.toolkit.permission_rules import RuleContext, match

log = logging.getLogger("noeta.hooks")

NAME = "hooks"
#: Entries kept per cache (per call id).
KEEP = 256
#: How what a hook added for the model is led, after its hook name (the
#: event, ``:<tool>`` for a tool's): Claude Code's labels.
CONTEXT_LABEL = " hook additional context"
BLOCK_LABEL = " hook blocking error"
#: A SessionStart / UserPromptSubmit hook's plain stdout, and a
#: ``continue: false`` the model is told about (Claude Code's labels).
SUCCESS_LABEL = " hook success"
STOPPED_LABEL = " hook stopped continuation"
#: The stop reason when a hook gave none (Claude Code's, per event).
STOPPED_DEFAULT = "Execution stopped by {event} hook"
#: Characters of a hook's output for the model kept inline; past it the
#: whole is saved to a file and a preview sent (Claude Code's 10,000).
OUTPUT_CAP = 10_000
#: The inbox signal that carries a background hook's answer to its task.
ASYNC_SIGNAL = "hooks.async_response"
#: What the model is told when a UserPromptSubmit hook answers ``continue:
#: false`` (Claude Code's words; ``: <reason>`` follows when it gave one).
PROMPT_STOPPED = "Operation stopped by hook"
#: How an ``asyncRewake`` hook's exit code 2 wakes the model (Claude Code's
#: format; the hook name stands where its command would).
REWAKE_TEXT = 'Stop hook blocking error from command "{name}": {output}'
#: Why a call is refused on a worker that cannot ask the client whose hook
#: callback would judge it.
UNREACHED = ("a {event} hook of the client that runs this task cannot be asked from the "
             "worker running this step, so the call is refused")


def stopped_text(name: str, reason: str) -> str:
    """``continue: false`` as the model is told: ``<hook name> hook stopped
    continuation: <reason>``."""
    return f"{name}{STOPPED_LABEL}: {reason}"


async def capped(view: TaskView, text: str, name: str) -> str:
    """``text`` when it fits :data:`OUTPUT_CAP`; past it, saved whole to the
    task's spill directory through its exec env (any worker reads it there)
    and replaced by a preview, or cut around an elision when it cannot be
    saved."""
    if len(text) <= OUTPUT_CAP:
        return text
    saved: str | None = None
    if view.spill_dir and view.env is not None:
        path = spill_path(view.spill_dir, view.task_id, name)
        try:
            await view.env.write_bytes(path, text.encode("utf-8"))
        except OSError:
            log.warning("hooks: output of %s could not be saved", name, exc_info=True)
        else:
            await mark_spill_dir(view.env, task_spill_dir(view.spill_dir, view.task_id),
                                 view.task_id)
            saved = path
    return large_output_message(text, OUTPUT_CAP, saved_to=saved)


def blocking_text(name: str, read: Read) -> str:
    """A hook's block as the model gets it: Claude Code's ``<hook name> hook
    blocking error from command: "<command>": <reason>`` (the command is
    the hook's command line, URL or prompt)."""
    return f'{name}{BLOCK_LABEL} from command: "{read.command}": {(read.block or "").strip()}'
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
    #: What background hooks added for the model, not sent yet: ``(id,
    #: text)``.
    async_pending: tuple[tuple[str, str], ...] = ()
    #: A tool hook's ``continue: false`` note, told once the turn has
    #: stopped (told while the call is open, it would start the turn again).
    stop_note: str | None = None
    #: What SessionStart hooks gave the model since the last compaction,
    #: oldest first: ``(command, texts)``, the command ``None`` for a hook
    #: that answered at once (a background answer the same as the last one
    #: is not given again).
    start_texts: tuple[tuple[str | None, tuple[str, ...]], ...] = ()


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
        # What the model saw before the compaction is gone.
        return replace(st, compact_seen=int(data.get("through") or 0), start_texts=())
    if kind == "once":
        return replace(st, once=st.once | {str(data.get("key"))})
    if kind == "batched":
        return replace(st, batched_through=int(data.get("through") or 0))
    if kind == "cwd_seen":
        return replace(st, cwd=str(data.get("cwd") or "") or None)
    if kind == "model_seen":
        return replace(st, model=str(data.get("model") or "") or None)
    if kind == "async_response":
        texts = [str(t) for t in data.get("texts") or () if t]
        key = str(data.get("id") or "")
        st = replace(st, async_pending=(*st.async_pending,
                                        *((f"{key}:{i}", t) for i, t in enumerate(texts))))
        if data.get("event") == "SessionStart":
            command = data.get("command")
            st = replace(st, start_texts=(*st.start_texts, (
                str(command) if command is not None else "", tuple(texts))))
        return st
    if kind == "context" and data.get("event") == "SessionStart":
        return replace(st, start_texts=(*st.start_texts,
                                        (None, tuple(str(t) for t in data.get("texts") or ()))))
    if kind == "stop_pending":
        return replace(st, stop_note=str(data.get("text") or "") or None)
    if kind == "stopped":
        return replace(st, stop_note=None)
    if kind == "async_delivered":
        return replace(st, async_pending=tuple(
            p for p in st.async_pending if p[0] != data.get("id")))
    if kind == "permission":
        decided = dict(st.permission)
        decided[str(data.get("call_id"))] = {k: v for k, v in data.items() if k != "call_id"}
        while len(decided) > KEEP:
            decided.pop(next(iter(decided)))
        return replace(st, permission=decided)
    return st


def start_repeat(st: HookState, command: str, texts: Sequence[str]) -> bool:
    """Whether a background SessionStart answer only repeats what the model
    already has, as Claude Code judges it: the newest earlier SessionStart
    output that could be the same (one that answered at once, or this
    command's) holds every one of its texts."""
    if not texts:
        return False
    for earlier, seen in reversed(st.start_texts):
        if earlier is not None and earlier != command:
            continue
        if seen:
            return all(t in seen for t in texts)
    return False


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
    #: The hook that answered: its command line, URL or prompt.
    command: str = ""

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
    found = view.find_tool(name) or view.find_tool(re.sub(r"[^A-Za-z0-9_-]", "_", name))
    if found is not None:
        return found
    # Written with the tool's old name (noeta.core.mcp_names).
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    for ext in view.extensions:
        for tool in ext.tools(view):
            if tool.name.startswith("mcp__") and safe in legacy_names(tool.name):
                warn_legacy("mcp_tool hook", name, tool.name)
                return view.find_tool(tool.name)
    return None


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
        #: The connected client ``callback`` hooks ask (none: they fail).
        self.callbacks: HookCallbacks | None = None
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

    def unreached(self, event: str, view: TaskView, subject: str | None) -> bool:
        """Whether the task records a client hook callback for ``event``
        matching ``subject`` that this process cannot ask (the client is
        held by another process: ``noeta.toolkit.hook_callbacks``)."""
        record = view.state.config.get(CLIENT_HOOKS_KEY)
        if not isinstance(record, Mapping) or (
                self.callbacks is not None and self.callbacks.answers(record)):
            return False
        events = record.get("events")
        patterns = events.get(event) if isinstance(events, Mapping) else None
        return isinstance(patterns, list) and any(
            Matcher(str(p), ()).matches(subject) for p in patterns)

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
            if hook.type == "callback":
                return await run_callback(
                    hook, payload, self.callbacks.call if self.callbacks is not None else None)
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

    async def _in_background(self, hook: HookCommand, payload: dict[str, Any], cwd: str | None,
                             view: TaskView, name: str, key: str) -> None:
        """A background hook, run to its end: what it has for the model goes
        to the task's inbox (the store's, so whichever worker runs the task
        next takes it). ``asyncRewake``: an exit code 2 wakes the model with
        the hook's output. ``async``: its JSON answer's ``systemMessage`` and
        ``additionalContext`` go with the next model call."""
        result = await self._one(hook, payload, cwd, view, None)
        if view.post is None:
            return
        if hook.rewake:
            if result.exit_code != 2:
                return
            output = await capped(view, (result.stderr or result.stdout).strip(),
                                  f"hook-{key}-stderr")
            text = REWAKE_TEXT.format(name=name, output=output)
            await view.post(Say(message=user(text, origin="system"), when="now"),
                            dedupe_key=f"{NAME}:rewake:{view.task_id}:{key}")
            return
        data = result.data or {}
        specific = data.get("hookSpecificOutput")
        context = specific.get("additionalContext") if isinstance(specific, dict) else None
        texts = [await capped(view, t.strip(), f"hook-{key}-{i}")
                 for i, t in enumerate((data.get("systemMessage"), context))
                 if isinstance(t, str) and t.strip()]
        if texts:
            await view.post(External(name=ASYNC_SIGNAL, data={
                "id": key, "texts": texts, "event": payload.get("hook_event_name"),
                "command": hook.label}), dedupe_key=f"{NAME}:async:{view.task_id}:{key}")

    async def run(self, event: str, view: TaskView, payload: Mapping[str, Any], *,
                  call: ToolUse | None = None, transcript: list[Message] | None = None,
                  record: bool = True) -> list[Read]:
        """Run the event's hooks at once (``async`` ones in the background)
        and read their answers, in configuration order. ``record``: the
        person is told about failures and ``systemMessage``s, and ``once``
        hooks are remembered (off where records cannot be made)."""
        hooks = self._select(event, view, payload, call)
        subject = payload.get(SUBJECT.get(event, ""))
        if self.unreached(event, view, subject if isinstance(subject, str) else None):
            # The tool gates refuse the call; any other event goes on without
            # the client's say.
            log.warning("hooks: the client's %s hook callbacks of task %s cannot be asked "
                        "from this worker; skipped", event, view.task_id)
        if not hooks:
            return []
        full = self.payload(event, view, payload)
        cwd = self.cwd(view)
        for h in hooks:
            if h.once and record:
                view.record(NAME, "once", {"key": h.key})
        name = f"{event}:{call.name}" if call is not None and event in TOOL_EVENTS else event
        # Names this firing among the task's (several calls of one step fire
        # the same event): its background answers and saved outputs.
        fired = f"{view.state.last_seq}-{event}" + (f"-{call.id}" if call is not None else "")
        for i, h in enumerate(hooks):
            if h.background:
                self.spawn(self._in_background(h, full, cwd, view, name, f"{fired}-{i}"))
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
            read = replace(_read(event, result), command=hook.label)
            read = await self._capped(view, read, f"hook-{fired}-{len(out)}")
            message = read.data.get("systemMessage")
            if record and isinstance(message, str) and message.strip():
                view.notify(message.strip())
            out.append(read)
        return out

    @staticmethod
    async def _capped(view: TaskView, read: Read, name: str) -> Read:
        """``read`` with its plain stdout and ``additionalContext`` within
        :data:`OUTPUT_CAP` (:func:`capped`)."""
        text = await capped(view, read.text, f"{name}-stdout")
        context = read.specific.get("additionalContext")
        specific = read.specific
        if isinstance(context, str) and len(context) > OUTPUT_CAP:
            specific = {**specific, "additionalContext": await capped(
                view, context, f"{name}-additionalContext")}
        if text == read.text and specific is read.specific:
            return read
        return replace(read, text=text, specific=specific)

    def stopped(self, view: TaskView, name: str, reason: str, *, later: bool = False) -> None:
        """Record ``continue: false`` for the model (:func:`stopped_text`);
        ``later``: once the turn has stopped (:meth:`stop_told`)."""
        text = stopped_text(name, reason)
        if later:
            view.record(NAME, "stop_pending", {"text": text})
        else:
            view.record(NAME, "stopped", {"hook": f"{name}{STOPPED_LABEL}"},
                        message=user(text, origin="system"))

    def stop_told(self, view: TaskView) -> None:
        """The ``continue: false`` note a tool hook left, now that the turn
        it stopped is over."""
        text = self.state(view).stop_note
        if text:
            view.record(NAME, "stopped", {"hook": text.split(": ", 1)[0]},
                        message=user(text, origin="system"))

    def blocks(self, view: TaskView, kind: str, name: str, reads: Sequence[Read]) -> None:
        """Record each hook's block for the model (:func:`blocking_text`)."""
        for r in reads:
            if r.block and r.block.strip():
                view.record(NAME, kind, {"hook": f"{name}{BLOCK_LABEL}"},
                            message=user(blocking_text(name, r), origin="system"))

    def context(self, view: TaskView, kind: str, label: str, texts: Sequence[str]) -> None:
        # ``label`` is ``<hook name> hook additional context`` (or ``blocking
        # error``), Claude Code's lead, the hook name being the event and,
        # for a tool's events, ``:<tool>``.
        """Record what hooks added for the model, as ``<label>: <text>``
        (after the open calls' results when calls are open)."""
        for text in texts:
            if text.strip():
                view.record(NAME, kind, {"hook": label},
                            message=user(f"{label}: {text.strip()}", origin="system"))

    async def stop_turn(self, view: TaskView, reason: str) -> None:
        """``continue: false``: the turn ends (the person sees why). The stop
        is posted with this hook's records, so what they tell the model is
        written before the turn is stopped."""
        if reason:
            view.notify(reason)
        view.enqueue(Cancel(reason=reason or "stopped by a hook", turn_only=True))
