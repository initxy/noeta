"""What a background command or a monitor tells the model, in Claude Code's words.

A finished background ``Bash`` command and every event a ``Monitor`` script
emits reach the model as a ``<task-notification>`` — the XML Claude Code
(2.1.300) queues between tool calls. This module renders those notices and
owns the monitor's event stream: stdout split into lines, lines batched,
batches rate-limited, all with Claude Code's literals and limits. It renders
and decides; it never delivers — the caller hands each notice on.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


__all__ = [
    "MonitorStream",
    "event_body",
    "format_duration",
    "monitor_end_summary",
    "monitor_event",
    "monitor_expired_note",
    "shell_end_summary",
    "task_notification",
]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def task_notification(
    *,
    task_id: str,
    summary: str,
    tool_use_id: str = "",
    status: str = "",
    body: str = "",
    trailing: str = "",
) -> str:
    """Claude Code's ``<task-notification>`` element: one child per non-empty
    field in its order, ``body`` before the closing tag, ``trailing`` after."""
    out = "<task-notification>"
    for tag, value in (
        ("task-id", task_id),
        ("tool-use-id", tool_use_id),
        ("status", status),
        ("summary", _escape(summary)),
    ):
        if value:
            out += f"\n<{tag}>{value}</{tag}>"
    return f"{out}{body}\n</task-notification>{trailing}"


def monitor_event(*, task_id: str, description: str, text: str) -> str:
    """One monitor event (or a monitor's own note) as Claude Code delivers it."""
    return task_notification(
        task_id=task_id,
        summary=f'Monitor event: "{description}"',
        body=event_body(text),
    )


def event_body(text: str) -> str:
    """The ``<event>`` element a monitor notification carries."""
    return f"\n<event>{_escape(text)}</event>"


def shell_end_summary(description: str, status: str, exit_code: Optional[int]) -> str:
    """Claude Code's summary of a background command's end; ``status`` is
    ``completed`` / ``failed`` / ``killed``."""
    if status == "completed":
        code = f" (exit code {exit_code})" if exit_code is not None else ""
        return f'Background command "{description}" completed{code}'
    if status == "failed":
        code = f" with exit code {exit_code}" if exit_code is not None else ""
        return f'Background command "{description}" failed{code}'
    return f'Background command "{description}" was stopped'


def monitor_end_summary(
    description: str, status: str, exit_code: Optional[int], *, produced_output: bool
) -> str:
    """Claude Code's summary of a monitor's end."""
    code = f" (exit {exit_code})" if exit_code is not None else ""
    if status == "completed":
        if not produced_output:
            return f'Monitor "{description}" ended without producing output{code}'
        return f'Monitor "{description}" stream ended'
    if status == "failed":
        return f'Monitor "{description}" script failed{code}'
    return f'Monitor "{description}" stopped'


def format_duration(ms: float) -> str:
    """Claude Code's duration format with trailing zero units hidden
    (``300000`` → ``5m``, ``90000`` → ``1m 30s``)."""
    if ms < 60_000:
        if ms == 0:
            return "0s"
        if ms < 1:
            return f"{ms / 1000:.1f}s"
        return f"{int(ms // 1000)}s"
    days = int(ms // 86_400_000)
    hours = int(ms % 86_400_000 // 3_600_000)
    minutes = int(ms % 3_600_000 // 60_000)
    seconds = round(ms % 60_000 / 1000)
    if seconds == 60:
        seconds, minutes = 0, minutes + 1
    if minutes == 60:
        minutes, hours = 0, hours + 1
    if hours == 24:
        hours, days = 0, days + 1
    if days > 0:
        if hours == 0 and minutes == 0:
            return f"{days}d"
        if minutes == 0:
            return f"{days}d {hours}h"
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        if minutes == 0 and seconds == 0:
            return f"{hours}h"
        if seconds == 0:
            return f"{hours}h {minutes}m"
        return f"{hours}h {minutes}m {seconds}s"
    if seconds == 0:
        return f"{minutes}m"
    return f"{minutes}m {seconds}s"


def monitor_expired_note(timeout_ms: float, events: int) -> str:
    """The one notice a monitor sends when its ``timeout_ms`` runs out."""
    after = format_duration(timeout_ms)
    if events == 0:
        return (
            f"[Monitor expired after {after} with no events delivered. Re-arm "
            "it if you still need the watch — and widen the filter if silence "
            "was unexpected.]"
        )
    noun = "event" if events == 1 else "events"
    return (
        f"[Monitor expired after {after} with {events} {noun} delivered. "
        "Re-arm it if you still need the watch.]"
    )


#: Claude Code's monitor limits: a line is cut at 500 characters, a batch at
#: 3000, lines within 200 ms share a batch, the unfinished line is held to its
#: last 1 MB, and a token bucket of 10 refilled one per 2 s paces the batches;
#: suppression that lasts 30 s stops the monitor.
LINE_MAX_CHARS = 500
BATCH_MAX_CHARS = 3000
BATCH_WINDOW_S = 0.2
PARTIAL_MAX_CHARS = 1_048_576
BUCKET_CAPACITY = 10
BUCKET_REFILL_S = 2.0
SUPPRESSION_STOP_S = 30.0

#: ``(text, housekeeping)`` — one event batch (or a monitor's own note) for
#: the caller to deliver as a notification.
EventSink = Callable[[str, bool], None]


class MonitorStream:
    """A monitor's stdout turned into notification-sized events.

    Thread-safe: the pipe reader feeds :meth:`feed`, a timer flushes each batch
    :data:`BATCH_WINDOW_S` after its first line, and the watcher drains the
    rest at exit. ``sink`` is called under the stream's lock, so events reach
    it in order and none after :meth:`close`. ``stop`` is how the stream ends
    a monitor that keeps flooding past the bucket.
    """

    def __init__(
        self,
        *,
        sink: EventSink,
        stop: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink
        self._stop = stop
        self._clock = clock
        self._lock = threading.RLock()
        self._partial = ""
        self._lines: list[str] = []
        self._timer: Optional[threading.Timer] = None
        self._closed = False
        self._tokens = BUCKET_CAPACITY
        self._refilled_at = clock()
        self._delivered = 0
        self._suppressed = 0
        self._suppressed_since: Optional[float] = None
        self._last_suppressed: Optional[float] = None
        self._saw_output = False

    @property
    def delivered(self) -> int:
        """Events delivered so far (notes not counted)."""
        return self._delivered

    @property
    def saw_output(self) -> bool:
        """Whether stdout ever carried a non-blank line."""
        return self._saw_output

    def feed(self, text: str) -> None:
        """Take a chunk of stdout; whole lines join the pending batch."""
        with self._lock:
            if self._closed:
                return
            self._partial += text
            if len(self._partial) > PARTIAL_MAX_CHARS:
                self._partial = self._partial[-PARTIAL_MAX_CHARS:]
            while (at := self._partial.find("\n")) != -1:
                self._queue(self._partial[:at])
                self._partial = self._partial[at + 1 :]
            if self._lines and self._timer is None:
                self._timer = threading.Timer(BATCH_WINDOW_S, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        """Deliver the pending batch now (the batch timer's body)."""
        with self._lock:
            batch = self._take(final=False)
            if batch is not None:
                self._deliver(batch)

    def note(self, text: str) -> None:
        """Deliver the monitor's own note (an expiry), outside the bucket."""
        with self._lock:
            if not self._closed:
                self._sink(text, True)

    def drain(self) -> Optional[str]:
        """Close the stream; return the undelivered lines (the unfinished one
        included) when the bucket admits them, for the end notice to carry."""
        with self._lock:
            if self._closed:
                return None
            batch = self._take(final=True)
            self._closed = True
            if batch is not None and self._admit():
                return batch
            return None

    def close(self) -> None:
        """Stop delivering; anything pending is dropped."""
        with self._lock:
            self._cancel_timer()
            self._closed = True

    # -- internals (callers hold the lock) -----------------------------------

    def _queue(self, raw: str) -> None:
        line = raw.strip()
        if not line:
            return
        self._saw_output = True
        if len(line) > LINE_MAX_CHARS:
            line = line[:LINE_MAX_CHARS] + "...(truncated)"
        self._lines.append(line)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _take(self, *, final: bool) -> Optional[str]:
        self._cancel_timer()
        if final and self._partial.strip():
            self._queue(self._partial)
            self._partial = ""
        if not self._lines:
            return None
        batch = "\n".join(self._lines)
        if len(batch) > BATCH_MAX_CHARS:
            batch = batch[:BATCH_MAX_CHARS] + "\n...(truncated)"
        self._lines = []
        return batch

    def _consume(self) -> bool:
        now = self._clock()
        refills = int((now - self._refilled_at) // BUCKET_REFILL_S)
        if refills > 0:
            self._tokens = min(BUCKET_CAPACITY, self._tokens + refills)
            self._refilled_at += refills * BUCKET_REFILL_S
        if self._tokens > 0:
            self._tokens -= 1
            return True
        return False

    def _admit(self) -> bool:
        if not self._consume():
            now = self._clock()
            self._suppressed += 1
            self._last_suppressed = now
            if self._suppressed_since is None:
                self._suppressed_since = now
            return False
        if self._suppressed > 0:
            self._sink(
                f"[{self._suppressed} events suppressed — output rate too high. "
                "Consider using KillShell to restart this monitor with a more "
                "selective filter.]",
                True,
            )
            self._suppressed = 0
            if (
                self._last_suppressed is not None
                and self._clock() - self._last_suppressed > BUCKET_REFILL_S * 3
            ):
                self._suppressed_since = None
        self._delivered += 1
        return True

    def _deliver(self, batch: str) -> None:
        if self._closed:
            return
        if self._admit():
            self._sink(batch, False)
            return
        since = self._suppressed_since
        if since is not None and self._clock() - since > SUPPRESSION_STOP_S:
            seconds = round(self._clock() - since)
            self._sink(
                f"[Monitor stopped — too much output ({self._suppressed} events "
                f"suppressed over {seconds}s). Restart with a more selective "
                "source.]",
                True,
            )
            self._closed = True
            self._stop()
