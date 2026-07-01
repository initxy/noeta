"""``LiveTimelineObserver`` — project canonical EventLog envelopes into a
terminal tool-call timeline **while a `noeta code` turn is in flight**
(issue 04).

Today the CLI renders only a batch :class:`CodeSessionResult` after a turn
finishes (``_render_turn``). This observer makes the loop legible *live*:
the moment the Engine commits a ``ToolCallStarted`` / ``ToolResultRecorded``
/ ``ToolCallFinished`` / ``MessagesAppended`` (assistant text) envelope, the
observer prints a line so the operator watches the tool-call lifecycle
(started → args → result → ok/fail) and assistant prose appear one at a
time — exactly like Claude Code.

**Read-only projection, no new schema.** The observer reads *canonical*
:class:`EventEnvelope`s only; it invents **no** CLI-private event type. It
is wired the same way as :class:`noeta.observers.audit.AuditObserver`
(self-subscribes on construction, ``stop()`` unsubscribes) — so the
EventLog and replay are untouched (a recording made with the
observer wired byte-equals one made without it: the observer never appends).

The callback fires synchronously post-COMMIT in the same thread that drives
``Engine.run_one_step`` (``InMemoryEventLog._notify`` / the storage
subscriber contract), so no background thread is needed and lines arrive in
strict ``seq`` order. Failures inside rendering are swallowed at WARNING —
an Observer must never raise back into the EventLog writer.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional, TextIO

from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogSubscriber, subscribe_with_stop
from noeta.protocols.events import EventEnvelope
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.tool_args import resolve_tool_call_arguments


__all__ = ["LiveTimelineObserver"]


_log = logging.getLogger(__name__)


class LiveTimelineObserver:
    """Render canonical envelopes into a live terminal timeline.

    Projects four canonical event types per appended envelope:

    * ``ToolCallStarted``  → ``→ tool {name} ({k=v, …})`` (started + args).
    * ``ToolResultRecorded`` → ``✔/✗ tool result … — {summary}`` (result +
      ok/fail). ``ToolCallStarted.arguments`` are captured for the started
      line; ``ToolResultRecorded.success`` drives the ✔/✗ glyph.
    * ``ToolCallFinished`` → the lifecycle-close marker (kept minimal so the
      timeline does not double-print the result).
    * ``MessagesAppended`` → each appended **assistant** ``TextBlock`` is
      dereferenced from ContentStore and printed as prose.

    All other envelope types are ignored — the batch
    :class:`CodeSessionResult` still renders the wrap-up summary.
    """

    name = "live-timeline"

    #: Cap a rendered argument / assistant-text line so a giant tool input
    #: or a long assistant paragraph cannot flood the terminal. The full
    #: body is still in the canonical record (this is a *projection*).
    _MAX_LINE = 200

    def __init__(
        self,
        *,
        event_log: EventLogSubscriber,
        content_store: ContentStore,
        stream: TextIO,
    ) -> None:
        self._content_store = content_store
        self._stream = stream
        self._lock = threading.Lock()
        # call_id → tool_name, so a result/finished line can name its tool
        # without re-reading the started envelope.
        self._tool_names: dict[str, str] = {}
        self._handle = subscribe_with_stop(event_log, self._on_event)

    def stop(self) -> None:
        self._handle.stop()

    # -- subscriber callback (post-COMMIT, in the driver thread) ----------

    def _on_event(self, env: EventEnvelope) -> None:
        try:
            line = self._render(env)
            if line is not None:
                with self._lock:
                    print(line, file=self._stream, flush=True)
        except Exception:  # noqa: BLE001 — Observer must not break writer
            _log.warning(
                "LiveTimelineObserver: render raised for envelope "
                "(id=%s task=%s type=%s)",
                env.id,
                env.task_id,
                env.type,
                exc_info=True,
            )

    # -- canonical-envelope → terminal-line projection -------------------

    def _render(self, env: EventEnvelope) -> Optional[str]:
        if env.type == "ToolCallStarted":
            return self._render_started(env.payload)
        if env.type == "ToolResultRecorded":
            return self._render_result(env.payload)
        if env.type == "ToolCallFinished":
            return self._render_finished(env.payload)
        if env.type == "MessagesAppended":
            return self._render_messages(env)
        return None

    def _render_started(self, payload: Any) -> str:
        name = getattr(payload, "tool_name", "?")
        call_id = getattr(payload, "call_id", "")
        if call_id:
            self._tool_names[call_id] = name
        try:
            # Derefs ``arguments_ref`` from the ContentStore for offloaded
            # (large-argument) calls; falls back to whatever is inline if
            # the deref fails — a render must never break the writer.
            arguments = resolve_tool_call_arguments(payload, self._content_store)
        except Exception:  # noqa: BLE001 — Observer must not break writer
            arguments = getattr(payload, "arguments", {}) or {}
        args = self._format_args(arguments)
        return self._truncate(f"→ {name}({args})")

    def _render_result(self, payload: Any) -> str:
        call_id = getattr(payload, "call_id", "")
        name = self._tool_names.get(call_id, "tool")
        success = bool(getattr(payload, "success", False))
        glyph = "ok" if success else "fail"
        mark = "✔" if success else "✗"
        summary = getattr(payload, "summary", "") or ""
        head = f"{mark} {name} [{glyph}]"
        if summary:
            return self._truncate(f"{head} — {summary}")
        return head

    def _render_finished(self, payload: Any) -> Optional[str]:
        # The result line already carried ok/fail + summary; the finished
        # marker would be redundant noise, so it closes the lifecycle
        # silently. Kept as an explicit branch so the four canonical tool
        # lifecycle events are all consciously projected (not fallen
        # through to "ignored").
        return None

    def _render_messages(self, env: EventEnvelope) -> Optional[str]:
        """Deref ``MessagesAppended.messages_ref`` and render assistant
        prose. Only ``assistant``-role ``TextBlock`` text is shown — tool
        results / user echoes are not the model's visible speech."""
        messages = self._messages(env)
        chunks: list[str] = []
        for msg in messages:
            if getattr(msg, "role", None) != "assistant":
                continue
            for block in getattr(msg, "content", ()):  # ordered blocks
                if isinstance(block, TextBlock):
                    text = (block.text or "").strip()
                    if text:
                        chunks.append(text)
        if not chunks:
            return None
        return self._truncate(" ".join(chunks))

    def _messages(self, env: EventEnvelope) -> list[Message]:
        ref = getattr(env.payload, "messages_ref", None)
        if ref is None:
            return []
        from noeta.core.fold import messages_from_appended

        return messages_from_appended(env, self._content_store)

    # -- formatting helpers ----------------------------------------------

    def _format_args(self, arguments: dict[str, Any]) -> str:
        parts: list[str] = []
        for key, value in arguments.items():
            parts.append(f"{key}={self._compact(value)}")
        return ", ".join(parts)

    @staticmethod
    def _compact(value: Any) -> str:
        """One-line scalar rendering of a tool argument value. Strings are
        quoted; structured values fall back to compact JSON (then truncated
        by the line cap)."""
        if isinstance(value, str):
            return json.dumps(value)
        try:
            return json.dumps(value, separators=(",", ":"))
        except (TypeError, ValueError):
            return repr(value)

    def _truncate(self, line: str) -> str:
        if len(line) <= self._MAX_LINE:
            return line
        return line[: self._MAX_LINE - 1] + "…"
