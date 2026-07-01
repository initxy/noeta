"""``HookObserver`` — Phase 4.5 F3 user PostToolUse / Notification hooks.

A user hook on these points can only **observe / record / notify**
— it never writes the EventLog, runtime messages, or
payloads. It subscribes to the EventLog post-COMMIT (read-only) and, on
a matching event, fires a side-effect.

**The subscriber callback never runs a command.** EventLog subscriber
callbacks fire synchronously inside the writer's emit path; running a
user `subprocess` there would stall the runtime decision loop. So the
callback does only lightweight matching + a non-blocking enqueue onto a
bounded queue, and a single background worker thread runs the commands.
A full queue drops + logs (back-pressure never blocks emit).

Layer boundary: this lives in ``noeta.observers`` and may import only
``noeta.protocols`` + stdlib (``observers-only-protocols`` forbids
``noeta.tools``), so the command runner uses a **local minimal env
scrub** rather than ``noeta.tools._env``. The runner is **injectable** so
tests can substitute a fake (no real subprocess / no real sleep).

This observer is **live-only**: it is wired only at the live construction
point (``noeta.execution.builder``) and never participates in fold / resume /
state reconstruction, so a hook side-effect cannot perturb a rebuilt state
and a resume never re-fires a user notification.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Callable, Optional, Protocol

from noeta.protocols.event_log import EventLogSubscriber, subscribe_with_stop
from noeta.protocols.events import EventEnvelope


__all__ = [
    "DEFAULT_NOTIFY_TIMEOUT_S",
    "HookObserver",
    "NotificationRule",
    "NotifyHandle",
    "NotifyRunner",
    "PostToolUseRule",
    "make_subprocess_runner",
]


_log = logging.getLogger(__name__)

DEFAULT_NOTIFY_TIMEOUT_S = 30.0
_DEFAULT_QUEUE_MAX = 256
_WORKER_POLL_S = 0.1

#: Minimal env a notify command inherits. Deliberately a small local
#: copy (not ``noeta.tools._env``) to keep ``noeta.observers`` importing
#: only ``noeta.protocols`` + stdlib (``observers-only-protocols``).
_OBS_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")


@dataclass(frozen=True, slots=True)
class PostToolUseRule:
    match_tool: str
    command: Optional[tuple[str, ...]] = None
    log: bool = False


@dataclass(frozen=True, slots=True)
class NotificationRule:
    on: str  # v1: "approval"
    command: Optional[tuple[str, ...]] = None
    log: bool = False


class NotifyHandle(Protocol):
    """A started notify side-effect the worker can wait on and `stop()`
    can **cancel** (F3 P1). ``wait`` blocks until the side-effect finishes
    (or its own bounded timeout); ``cancel`` terminates an in-flight one
    so ``stop()`` does not leave a user hook running after the session
    exits. Both must be safe to call from different threads."""

    def wait(self) -> None: ...

    def cancel(self) -> None: ...


#: Starts one notify command and returns its cancellable handle.
#: Injectable for tests.
NotifyRunner = Callable[[tuple[str, ...]], NotifyHandle]


def _scrub_env_local() -> dict[str, str]:
    return {k: os.environ[k] for k in _OBS_ENV_ALLOWLIST if k in os.environ}


class _NoopHandle:
    """A handle for a command that never started (spawn failure)."""

    def wait(self) -> None:
        return None

    def cancel(self) -> None:
        return None


class _PopenHandle:
    """Wraps a live ``Popen``: ``wait`` enforces the per-command timeout
    (kill on expiry); ``cancel`` terminates it (``stop()`` path)."""

    def __init__(self, proc: "subprocess.Popen[bytes]", timeout_s: float) -> None:
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
            _log.warning("notify command failed: %s", exc)

    def cancel(self) -> None:
        # Terminate the in-flight process; the worker's `wait()`
        # (communicate) then returns. terminate→kill, best-effort.
        with contextlib.suppress(Exception):
            self._proc.terminate()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=2.0)
        with contextlib.suppress(Exception):
            if self._proc.poll() is None:
                self._proc.kill()


def make_subprocess_runner(
    *, cwd: str, timeout_s: float = DEFAULT_NOTIFY_TIMEOUT_S
) -> NotifyRunner:
    """The default runner: start one command (argv, **never** ``shell``)
    with cwd=workspace, stdin/stdout/stderr=DEVNULL, and a scrubbed env;
    return a :class:`NotifyHandle` the worker waits on (bounded timeout,
    kill+reap on expiry) and ``stop()`` can cancel. A spawn failure is
    swallowed (returns a no-op handle) — a notify hook must never break
    the session."""

    def _run(argv: tuple[str, ...]) -> NotifyHandle:
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv list, never shell=True
                list(argv),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_scrub_env_local(),
            )
        except OSError as exc:
            _log.warning("notify command spawn failed: %s", exc)
            return _NoopHandle()
        return _PopenHandle(proc, timeout_s)

    return _run


class HookObserver:
    """Subscribe to the EventLog; enqueue notify side-effects for a
    background worker. Live-only, never wired into fold / resume."""

    def __init__(
        self,
        *,
        event_log: EventLogSubscriber,
        post_tool_use: tuple[PostToolUseRule, ...],
        notification: tuple[NotificationRule, ...],
        runner: NotifyRunner,
        max_queue: int = _DEFAULT_QUEUE_MAX,
        log_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._post = post_tool_use
        self._notify = notification
        self._runner = runner
        self._log_sink = log_sink or (lambda msg: _log.info("%s", msg))
        self._q: "queue.Queue[tuple[str, ...]]" = queue.Queue(maxsize=max_queue)
        #: In-flight tool calls (call_id -> tool_name), populated on
        #: ToolCallStarted and evicted on ToolResultRecorded so the dict
        #: only ever holds calls still awaiting a result. Guarded by
        #: ``_names_lock`` because subscriber callbacks fire post-COMMIT
        #: outside the writer lock and may run concurrently (see the
        #: Observer concurrency contract — same discipline as Audit/Metrics).
        self._call_names: dict[str, str] = {}
        self._names_lock = threading.Lock()
        self._stop = threading.Event()
        #: The handle for the command the worker is currently running, so
        #: ``stop()`` can cancel an in-flight notify (F3 P1). Guarded by
        #: ``_current_lock`` (set/read from worker + stop threads).
        self._current: Optional[NotifyHandle] = None
        self._current_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._drain, name="noeta-hook-observer", daemon=True
        )
        self._worker.start()
        self._handle = subscribe_with_stop(event_log, self._on_event)

    # -- subscriber callback (must stay non-blocking) --------------------

    def _on_event(self, env: EventEnvelope) -> None:
        try:
            if env.type == "ToolCallStarted":
                with self._names_lock:
                    self._call_names[env.payload.call_id] = env.payload.tool_name
                return
            if env.type == "ToolResultRecorded":
                # The call is finished — evict its entry so the dict only
                # retains in-flight calls (no unbounded growth over a long
                # session and its subtasks).
                with self._names_lock:
                    tool = self._call_names.pop(env.payload.call_id, None)
                if tool is None:
                    return
                for rule in self._post:
                    if fnmatchcase(tool, rule.match_tool):
                        self._fire(rule.command, rule.log, f"post_tool_use {tool}")
                return
            if env.type == "ToolCallApprovalRequested":
                for nrule in self._notify:
                    if nrule.on == "approval":
                        self._fire(
                            nrule.command, nrule.log, "notification approval"
                        )
        except Exception:  # noqa: BLE001 — an observer must never break the writer
            _log.warning("HookObserver callback error", exc_info=True)

    def _fire(
        self, command: Optional[tuple[str, ...]], do_log: bool, label: str
    ) -> None:
        if do_log:
            self._log_sink(f"hook: {label}")
        if command is None:
            return
        try:
            self._q.put_nowait(command)
        except queue.Full:
            _log.warning("hook notify queue full; dropping command for %s", label)

    # -- background worker ----------------------------------------------

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                command = self._q.get(timeout=_WORKER_POLL_S)
            except queue.Empty:
                continue
            try:
                handle = self._runner(command)
            except Exception:  # noqa: BLE001 — never let a hook crash the worker
                _log.warning("hook notify runner error", exc_info=True)
                continue
            with self._current_lock:
                # If stop() already fired, cancel immediately instead of
                # running a fresh command after teardown began.
                if self._stop.is_set():
                    with contextlib.suppress(Exception):
                        handle.cancel()
                    self._current = None
                    continue
                self._current = handle
            try:
                handle.wait()
            except Exception:  # noqa: BLE001 — defensive
                _log.warning("hook notify wait error", exc_info=True)
            finally:
                with self._current_lock:
                    self._current = None

    def stop(self) -> None:
        """Bounded teardown: unsubscribe, signal stop, **cancel the
        in-flight command** (F3 P1 — so no user hook keeps running after
        the session exits), drain/drop the queue, and join the worker
        with a timeout. Idempotent."""
        with contextlib.suppress(Exception):
            self._handle.stop()
        self._stop.set()
        # Cancel an in-flight notify so it cannot outlive the session.
        with self._current_lock:
            current = self._current
        if current is not None:
            with contextlib.suppress(Exception):
                current.cancel()
        # drop anything still queued so a long backlog cannot delay exit
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._worker.join(timeout=2.0)
