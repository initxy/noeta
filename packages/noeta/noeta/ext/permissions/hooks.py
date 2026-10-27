"""The ``Hooks`` extension: PostToolUse and Notification commands.

Side effects only. ``on_event`` sees an event only when a runner appends it
live — never while the log is folded on pickup or recovery — so a resumed
task never re-fires a hook. ``on_event`` only matches and enqueues; one
background thread runs the commands (argv, never a shell, minimal
environment, killed after the timeout), and a full queue drops the command
with a warning instead of slowing the task.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Protocol

from noeta.events import Recorded, ToolFinished, ToolStarted, Waiting
from noeta.ext.base import Extension, TaskView

log = logging.getLogger("noeta.hooks")

#: Seconds one hook command may run before it is killed.
DEFAULT_HOOK_COMMAND_TIMEOUT_S = 30.0
#: Commands waiting to run; past this a new one is dropped (logged).
DEFAULT_HOOK_QUEUE_MAX = 256

#: The environment a hook command inherits.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
_WORKER_POLL_S = 0.1
_SEEN_KEEP = 1024


@dataclass(frozen=True)
class PostToolUseRule:
    """Run ``command`` after a tool matching ``match_tool`` (fnmatch)
    finishes; ``log`` writes ``hook: post_tool_use <tool>`` as well or
    instead."""

    match_tool: str
    command: tuple[str, ...] | None = None
    log: bool = False


@dataclass(frozen=True)
class NotificationRule:
    """Run ``command`` when a tool call starts waiting for approval
    (``on="approval"``, the only moment)."""

    on: str
    command: tuple[str, ...] | None = None
    log: bool = False


class NotifyHandle(Protocol):
    def wait(self) -> None: ...

    def cancel(self) -> None: ...


#: Starts one command and returns its handle (injectable for tests).
NotifyRunner = Callable[[tuple[str, ...]], NotifyHandle]


class _NoopHandle:
    def wait(self) -> None:
        return None

    def cancel(self) -> None:
        return None


class _PopenHandle:
    def __init__(self, proc: subprocess.Popen[bytes], timeout_s: float) -> None:
        self._proc = proc
        self._timeout_s = timeout_s

    def wait(self) -> None:
        try:
            self._proc.communicate(timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            with contextlib.suppress(Exception):
                self._proc.communicate(timeout=2.0)
        except Exception as exc:  # noqa: BLE001 — defensive
            log.warning("hook command failed: %s", exc)

    def cancel(self) -> None:
        with contextlib.suppress(Exception):
            self._proc.terminate()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=2.0)
        with contextlib.suppress(Exception):
            if self._proc.poll() is None:
                self._proc.kill()


def subprocess_runner(*, cwd: str, timeout_s: float = DEFAULT_HOOK_COMMAND_TIMEOUT_S
                      ) -> NotifyRunner:
    """Run one command as argv in ``cwd``; a spawn failure is swallowed."""

    def run(argv: tuple[str, ...]) -> NotifyHandle:
        env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
        try:
            proc = subprocess.Popen(
                list(argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env)
        except OSError as exc:
            log.warning("hook command spawn failed: %s", exc)
            return _NoopHandle()
        return _PopenHandle(proc, timeout_s)

    return run


def _check_command(where: str, command: object, do_log: bool) -> None:
    if command is not None and (
        not isinstance(command, tuple) or not command
        or not all(isinstance(a, str) and a for a in command)
    ):
        raise ValueError(
            f"{where}.command must be a non-empty tuple of non-empty str (an argv, never "
            f"run through a shell) or None; got {command!r}")
    if command is None and not do_log:
        raise ValueError(f"{where} does nothing: set command, log=True, or both")


class Hooks(Extension):
    """PostToolUse / Notification hooks (0.6's ``HooksConfig`` minus
    ``pre_tool_use``, which is a gate: ``Permissions(pre_tool_use=...)``).

    * ``post_tool_use`` — :class:`PostToolUseRule` s; fire for calls that
      started (a denied call never ran, so it fires nothing).
    * ``notification`` — :class:`NotificationRule` s; fire once per call
      that starts waiting for approval.
    * ``cwd`` — where commands run (default: the process's directory).
    * ``command_timeout_s`` (30 s) and ``max_queue`` (256), as in 0.6.
    * ``runner`` — replaces the subprocess runner (tests).
    * ``log_sink`` — receives the ``hook: ...`` lines of ``log=True`` rules.

    Call :meth:`stop` at shutdown to cancel a running command.
    """

    name = "hooks"

    def __init__(
        self,
        *,
        post_tool_use: Sequence[PostToolUseRule] = (),
        notification: Sequence[NotificationRule] = (),
        cwd: str | None = None,
        command_timeout_s: float = DEFAULT_HOOK_COMMAND_TIMEOUT_S,
        max_queue: int = DEFAULT_HOOK_QUEUE_MAX,
        runner: NotifyRunner | None = None,
        log_sink: Callable[[str], None] | None = None,
    ) -> None:
        for i, rule in enumerate(post_tool_use):
            where = f"post_tool_use[{i}]"
            if not isinstance(rule, PostToolUseRule):
                raise ValueError(f"{where} must be a PostToolUseRule; got {rule!r}")
            if not isinstance(rule.match_tool, str) or not rule.match_tool:
                raise ValueError(f"{where}.match_tool must be a non-empty tool-name pattern; "
                                 f"got {rule.match_tool!r}")
            _check_command(where, rule.command, rule.log)
        for i, nrule in enumerate(notification):
            where = f"notification[{i}]"
            if not isinstance(nrule, NotificationRule):
                raise ValueError(f"{where} must be a NotificationRule; got {nrule!r}")
            if nrule.on != "approval":
                raise ValueError(f"{where}.on must be 'approval' (the only notification "
                                 f"moment); got {nrule.on!r}")
            _check_command(where, nrule.command, nrule.log)
        if isinstance(command_timeout_s, bool) or not command_timeout_s > 0:
            raise ValueError(f"command_timeout_s must be > 0; got {command_timeout_s!r}")
        if isinstance(max_queue, bool) or not isinstance(max_queue, int) or max_queue < 1:
            raise ValueError(f"max_queue must be an int >= 1; got {max_queue!r}")
        self.post = tuple(post_tool_use)
        self.notify = tuple(notification)
        self._runner = runner or subprocess_runner(cwd=cwd or os.getcwd(),
                                                   timeout_s=command_timeout_s)
        self._log_sink = log_sink or (lambda msg: log.info("%s", msg))
        self._q: queue.Queue[tuple[str, ...]] = queue.Queue(maxsize=max_queue)
        #: call id -> tool name, for calls that started and have not finished.
        self._started: OrderedDict[str, str] = OrderedDict()
        #: (task, call) pairs already notified, so a re-wait does not re-fire.
        self._notified: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._stop = threading.Event()
        self._current: NotifyHandle | None = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, ToolStarted):
            self._started[f"{view.task_id}/{ev.call.id}"] = ev.call.name
            while len(self._started) > _SEEN_KEEP:
                self._started.popitem(last=False)
        elif isinstance(ev, ToolFinished):
            tool = self._started.pop(f"{view.task_id}/{ev.result.call_id}", None)
            if tool is None:
                return
            for rule in self.post:
                if fnmatchcase(tool, rule.match_tool):
                    self._fire(rule.command, rule.log, f"post_tool_use {tool}")
        elif isinstance(ev, Waiting) and ev.on == "approval":
            for call_id in ev.ids:
                key = (view.task_id, call_id)
                if key in self._notified:
                    continue
                self._notified[key] = None
                while len(self._notified) > _SEEN_KEEP:
                    self._notified.popitem(last=False)
                for nrule in self.notify:
                    self._fire(nrule.command, nrule.log, "notification approval")

    def _fire(self, command: tuple[str, ...] | None, do_log: bool, label: str) -> None:
        if do_log:
            self._log_sink(f"hook: {label}")
        if command is None or self._stop.is_set():
            return
        self._ensure_worker()
        try:
            self._q.put_nowait(command)
        except queue.Full:
            log.warning("hook notify queue full; dropping command for %s", label)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain, name="noeta-hooks",
                                                daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                command = self._q.get(timeout=_WORKER_POLL_S)
            except queue.Empty:
                continue
            try:
                handle = self._runner(command)
            except Exception:
                log.warning("hook runner error", exc_info=True)
                continue
            with self._lock:
                if self._stop.is_set():
                    with contextlib.suppress(Exception):
                        handle.cancel()
                    continue
                self._current = handle
            try:
                handle.wait()
            except Exception:
                log.warning("hook wait error", exc_info=True)
            finally:
                with self._lock:
                    self._current = None

    def stop(self) -> None:
        """Bounded, idempotent: cancel the running command, drop the queue."""
        self._stop.set()
        with self._lock:
            current, worker = self._current, self._worker
        if current is not None:
            with contextlib.suppress(Exception):
                current.cancel()
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if worker is not None:
            worker.join(timeout=2.0)
