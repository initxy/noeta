"""Claude Code's command hooks: commands a person configures that run at
fixed points of a task and can add context for the model or stop what is
about to happen.

The configuration is Claude Code's ``hooks`` object, from the ``hooks``
argument and from settings files (``{"hooks": {...}}``; the ``noeta``
command reads ``~/.noeta/settings.json``)::

    {"PreToolUse": [{"matcher": "Bash|Edit",
                     "hooks": [{"type": "command", "command": "check.sh", "timeout": 30}]}]}

A command runs through ``sh -c`` on this machine, in the task's working
directory when it exists here, with the event as JSON on stdin and a
scrubbed environment plus ``NOETA_PROJECT_DIR`` (and ``CLAUDE_PROJECT_DIR``
for Claude Code's scripts). Exit 0 is success; exit 2 blocks, with stderr as
the reason; anything else is logged and ignored. Stdout may instead be a
JSON object (``decision``, ``reason``, ``hookSpecificOutput``).

Events:

* ``UserPromptSubmit`` — before the model answers new messages from the
  person. Stdout (or ``additionalContext``) is added for the model; a block
  fails the turn with the reason.
* ``PreToolUse`` (matched on the tool name) — a block denies the call; JSON
  ``permissionDecision`` ``deny`` / ``ask`` denies it or asks the person.
* ``PostToolUse`` (matched on the tool name) — a block, or
  ``additionalContext``, is passed to the model after the call's result.
* ``Stop`` — when the model ends its turn in a top-level task; a block makes
  it go on, with the reason as its next message (``stop_hook_active`` is
  true then; at most :data:`MAX_STOP_CONTINUATIONS` times in a row).
* ``Notification`` — when a task starts waiting on an approval or a question;
  for side effects only (not waited for).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from noeta.core.errors import TurnFailed
from noeta.core.events import ModelReplied, Recorded, Say, Waiting
from noeta.core.execenv import scrub_env
from noeta.core.extension import Ask, Deny, Extension, Gate, Reminder, TaskView
from noeta.core.tool import ToolOutput
from noeta.core.types import ToolUse, user

__all__ = [
    "EVENTS",
    "MAX_STOP_CONTINUATIONS",
    "HookCommand",
    "HookResult",
    "Hooks",
    "Matcher",
    "parse_hooks",
    "run_command",
]

log = logging.getLogger("noeta.hooks")

EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Notification")
#: A Stop hook that keeps blocking is let through after this many in a row.
MAX_STOP_CONTINUATIONS = 8
DEFAULT_TIMEOUT_S = 60.0
_OUTPUT_CAP = 64 * 1024


@dataclass(frozen=True)
class HookCommand:
    command: str
    timeout: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class Matcher:
    """``pattern`` is matched against the tool name as a whole (a regular
    expression, e.g. ``Edit|Write``); empty or ``*`` matches everything."""

    pattern: str
    hooks: tuple[HookCommand, ...]

    def matches(self, name: str) -> bool:
        if self.pattern in ("", "*"):
            return True
        try:
            return re.fullmatch(self.pattern, name) is not None
        except re.error:
            return self.pattern == name


@dataclass(frozen=True)
class HookResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def data(self) -> dict[str, Any] | None:
        """Stdout as a JSON object, when it is one."""
        text = self.stdout.strip()
        if not text.startswith("{"):
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


Runner = Callable[[HookCommand, dict[str, Any], "str | None"], Awaitable[HookResult]]


def parse_hooks(config: Mapping[str, Any]) -> dict[str, list[Matcher]]:
    """Claude Code's ``hooks`` object -> matchers per event. Unknown events
    and non-command hooks are skipped with a warning."""
    out: dict[str, list[Matcher]] = {}
    for event, groups in config.items():
        if event not in EVENTS:
            log.warning("hooks: unknown event %r skipped", event)
            continue
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} must be a list")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"hooks.{event}: each entry is an object with 'hooks'")
            commands: list[HookCommand] = []
            for h in group.get("hooks") or []:
                if not isinstance(h, dict) or h.get("type", "command") != "command":
                    log.warning("hooks.%s: only command hooks run; skipped %r", event, h)
                    continue
                command = h.get("command")
                if not isinstance(command, str) or not command.strip():
                    raise ValueError(f"hooks.{event}: a command hook needs 'command'")
                timeout = h.get("timeout", DEFAULT_TIMEOUT_S)
                commands.append(HookCommand(command, float(timeout)))
            if commands:
                pattern = group.get("matcher") or ""
                out.setdefault(event, []).append(Matcher(str(pattern), tuple(commands)))
    return out


def _read_settings(path: str | Path) -> Mapping[str, Any]:
    p = Path(path).expanduser()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("hooks: cannot read %s: %s", p, exc)
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return hooks if isinstance(hooks, dict) else {}


async def run_command(hook: HookCommand, payload: dict[str, Any], cwd: str | None) -> HookResult:
    """Run one hook command: ``sh -c``, the payload on stdin, killed after
    its timeout (which reads as a non-blocking failure)."""
    env = scrub_env()
    if cwd:
        env["NOETA_PROJECT_DIR"] = env["CLAUDE_PROJECT_DIR"] = cwd
    try:
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", hook.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd if cwd and os.path.isdir(cwd) else None, env=env, start_new_session=True)
    except OSError as exc:
        return HookResult(exit_code=-1, stderr=str(exc))
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode()), hook.timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return HookResult(exit_code=-1, stderr=f"timed out after {hook.timeout:g}s")
    return HookResult(exit_code=proc.returncode or 0,
                      stdout=out[:_OUTPUT_CAP].decode("utf-8", "replace"),
                      stderr=err[:_OUTPUT_CAP].decode("utf-8", "replace"))


def _specific(data: Mapping[str, Any] | None) -> Mapping[str, Any]:
    value = (data or {}).get("hookSpecificOutput")
    return value if isinstance(value, dict) else {}


def _blocked(result: HookResult) -> str | None:
    """The reason when ``result`` blocks, else ``None``."""
    if result.exit_code == 2:
        return result.stderr.strip() or "blocked by a hook"
    data = result.data
    if result.exit_code == 0 and data and data.get("decision") == "block":
        return str(data.get("reason") or "blocked by a hook")
    return None


@dataclass(frozen=True)
class _State:
    #: Seq of the newest person's message the prompt hooks have seen.
    prompted_through: int = 0
    #: Stop hooks that blocked since the person's message ``stop_since``.
    stop_blocks: int = 0
    stop_since: int = 0


class Hooks(Extension):
    """Constructor arguments:

    * ``hooks`` — Claude Code's ``hooks`` object.
    * ``settings_files`` — settings files whose ``hooks`` are added (a file
      that is missing or unreadable adds nothing).
    * ``runner`` — runs one command (tests swap it).
    """

    name = "hooks"

    def __init__(self, *, hooks: Mapping[str, Any] | None = None,
                 settings_files: Sequence[str | Path] = (),
                 runner: Runner = run_command) -> None:
        self.matchers: dict[str, list[Matcher]] = {}
        for config in [*(_read_settings(f) for f in settings_files), hooks or {}]:
            for event, ms in parse_hooks(config).items():
                self.matchers.setdefault(event, []).extend(ms)
        self.runner = runner
        self._background: set[asyncio.Future[list[HookResult]]] = set()

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
        return st

    def _state(self, view: TaskView) -> _State:
        st = view.ext_state(self.name)
        return st if isinstance(st, _State) else _State()

    # ------------------------------------------------------------ running

    async def _run(self, event: str, view: TaskView, payload: dict[str, Any],
                   tool: str = "") -> list[HookResult]:
        commands = [h for m in self.matchers.get(event, ()) if m.matches(tool) for h in m.hooks]
        if not commands:
            return []
        cwd = str(view.state.config.get("cwd") or "") or None
        full = {"hook_event_name": event, "task_id": view.task_id, "cwd": cwd or "", **payload}
        results: list[HookResult] = []
        for hook in commands:
            result = await self.runner(hook, full, cwd)
            if result.exit_code not in (0, 2):
                log.warning("hook %r for %s exited %s: %s", hook.command, event,
                            result.exit_code, result.stderr.strip()[:500])
            results.append(result)
        return results

    # ------------------------------------------------------------ events

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """``UserPromptSubmit``, before the first model call of a turn."""
        if "UserPromptSubmit" not in self.matchers or view.state.steps_in_turn:
            return ()
        st = self._state(view)
        new = [(seq, m) for seq, m in zip(view.state.message_seqs, view.state.messages,
                                           strict=False)
               if m.role == "user" and m.origin == "human" and seq > st.prompted_through]
        if not new:
            return ()
        through = new[-1][0]
        prompt = "\n\n".join(m.text for _, m in new)
        results = await self._run("UserPromptSubmit", view, {"prompt": prompt})
        view.record(self.name, "prompted", {"through": through})
        for result in results:
            reason = _blocked(result)
            if reason is not None:
                raise TurnFailed("blocked_by_hook", reason)
        added = []
        for result in results:
            extra = _specific(result.data).get("additionalContext")
            if isinstance(extra, str) and extra.strip():
                added.append(extra.strip())
            elif result.exit_code == 0 and result.data is None and result.stdout.strip():
                added.append(result.stdout.strip())
        return [Reminder(text=text, kind="context") for text in added]

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        results = await self._run("PreToolUse", view,
                                  {"tool_name": call.name, "tool_input": call.input},
                                  tool=call.name)
        for result in results:
            specific = _specific(result.data)
            decision = specific.get("permissionDecision")
            reason = str(specific.get("permissionDecisionReason") or "")
            if decision == "deny":
                return Deny(reason=reason or "denied by a hook")
            blocked = _blocked(result)
            if blocked is not None:
                return Deny(reason=blocked)
            if decision == "ask":
                return Ask(prompt=reason or f"A hook asks to confirm {call.name}")
        return None

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        text = "".join(getattr(b, "text", "") for b in output.content)
        results = await self._run(
            "PostToolUse", view,
            {"tool_name": call.name, "tool_input": call.input,
             "tool_response": {"text": text, "is_error": output.is_error}},
            tool=call.name)
        for result in results:
            notes = [n for n in (_blocked(result), _specific(result.data).get("additionalContext"))
                     if isinstance(n, str) and n.strip()]
            for note in notes:
                view.record(self.name, "feedback", {"tool": call.name},
                            message=user(f"PostToolUse hook ({call.name}): {note.strip()}",
                                         origin="system"))
        return output

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        event = recorded.event
        if isinstance(event, Waiting) and event.on in ("approval", "question"):
            if "Notification" in self.matchers:
                message = ("Noeta needs your permission" if event.on == "approval"
                           else "Noeta has a question for you")
                task = asyncio.ensure_future(
                    self._run("Notification", view, {"message": message}))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
            return
        if not (isinstance(event, ModelReplied) and "Stop" in self.matchers):
            return
        if view.state.parent_id is not None or event.response.message.tool_uses:
            return
        if event.response.stop_reason != "end_turn":
            return
        st = self._state(view)
        since = max((seq for seq, m in zip(view.state.message_seqs, view.state.messages,
                                            strict=False)
                     if m.role == "user" and m.origin == "human"), default=0)
        blocks = st.stop_blocks if since == st.stop_since else 0
        if blocks >= MAX_STOP_CONTINUATIONS:
            log.warning("task %s: Stop hooks blocked %d times in a row; letting it stop",
                        view.task_id, blocks)
            return
        results = await self._run("Stop", view, {"stop_hook_active": blocks > 0})
        for result in results:
            reason = _blocked(result)
            if reason is not None and view.post is not None:
                view.record(self.name, "stop_blocked", {"since": since, "reason": reason})
                await view.post(Say(message=user(reason, origin="system"), when="now"))
                return
