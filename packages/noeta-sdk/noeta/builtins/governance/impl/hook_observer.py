"""``HookObserver`` — user PostToolUse / Notification hooks as side-effects.

A hook here may only observe, record or notify; it never writes the EventLog.
EventLog subscriber callbacks fire synchronously inside the writer's emit
path, so running a user subprocess there would stall the decision loop: the
callback only matches and enqueues onto a bounded queue, one background worker
runs the commands, and a full queue drops rather than applying back-pressure
to emit. The observer is live-only — it never participates in fold or resume,
so a hook side-effect cannot perturb a rebuilt state and a resume never
re-fires a user notification.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import subprocess
import threading
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Callable, Optional, Protocol

from noeta.protocols.event_log import EventLogSubscriber, subscribe_with_stop
from noeta.protocols.events import EventEnvelope
from noeta.runtime.env import scrub_env


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

#: Minimal env a notify command inherits — deliberately narrower than the
#: default ``ENV_ALLOWLIST`` (a user notify hook has no business seeing the
#: Python interpreter keys).
_OBS_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")


@dataclass(frozen=True, slots=True)
class PostToolUseRule:
    match_tool: str
    command: Optional[tuple[str, ...]] = None
    log: bool = False


@dataclass(frozen=True, slots=True)
class NotificationRule:
    on: str  # the only recognised value is "approval"
    command: Optional[tuple[str, ...]] = None
    log: bool = False


class NotifyHandle(Protocol):
    """A started notify side-effect the worker waits on and ``stop()`` cancels.

    ``wait`` blocks until the side-effect finishes or its own bounded timeout;
    ``cancel`` terminates an in-flight one so no user hook keeps running after
    the session exits. Both must be safe to call from different threads."""

    def wait(self) -> None: ...

    def cancel(self) -> None: ...


#: Starts one notify command and returns its cancellable handle.
#: Injectable for tests.
NotifyRunner = Callable[[tuple[str, ...]], NotifyHandle]


def _scrub_env_local() -> dict[str, str]:
    return scrub_env(allowlist=_OBS_ENV_ALLOWLIST)


class _NoopHandle:
    """A handle for a command that never started (spawn failure)."""

    def wait(self) -> None:
        return None

    def cancel(self) -> None:
        return None


class _PopenHandle:
    """Wraps a live ``Popen``; ``wait`` kills the process once the per-command
    timeout expires, so one wedged hook cannot pin the worker."""

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
        # terminate→kill is best-effort; the worker's blocked ``communicate``
        # unblocks once the process is gone.
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
    """The default runner: one command as argv, **never** through a shell.

    A spawn failure is swallowed (a no-op handle comes back) because a notify
    hook must never break the session."""

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
    """Enqueue notify side-effects for a background worker. Live-only."""

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
        #: In-flight tool calls (call_id -> tool_name); evicting on
        #: ToolResultRecorded is what keeps it from growing without bound over
        #: a long session. Guarded by ``_names_lock`` because subscriber
        #: callbacks fire post-COMMIT outside the writer lock and may run
        #: concurrently.
        self._call_names: dict[str, str] = {}
        self._names_lock = threading.Lock()
        self._stop = threading.Event()
        #: The handle for the command the worker is currently running, so
        #: ``stop()`` can cancel an in-flight notify. Guarded by
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
        """Bounded, idempotent teardown.

        Cancelling the in-flight command is the point: without it a user hook
        keeps running after the session exits. Every step is bounded (queue
        dropped, worker joined with a timeout) so teardown cannot hang.
        """
        with contextlib.suppress(Exception):
            self._handle.stop()
        self._stop.set()
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
