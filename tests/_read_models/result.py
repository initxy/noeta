"""Code-session result shape + the EventLog read-back that fills it.

``CodeSessionResult`` is what a test asserts on after driving a session; the
module-private helpers walk the durable EventLog to project files-changed /
failed-edits / last-shell / selected-skills out of ``ToolResultRecorded`` and
``ContextPlanComposed`` events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from noeta.protocols.content_store import ContentStore
from noeta.protocols.events import EventEnvelope
from noeta.protocols.tool_args import resolve_tool_call_arguments


__all__ = [
    "CodeSessionResult",
    "_EDIT_TOOLS",
    "_SHELL_TOOLS",
    "_iter_tool_results",
    "_collect_files_changed",
    "_collect_failed_edits",
    "_extract_reason",
    "_last_shell_result",
    "_last_selected_skills",
]


@dataclass(frozen=True, slots=True)
class CodeSessionResult:
    """Summary of one code session, projected from its EventLog.

    ``failed_edits`` carries every ``edit`` call that ended in
    ``ToolResult.success=False``, so a reader of ``to_json()`` can tell which
    edits the model attempted but could not apply. ``write`` failures are out of
    that field's scope — they appear in the EventLog as
    ``ToolResultRecorded(success=False)`` and nowhere here. Applying a
    multi-file sequence is honestly **non-atomic**: a failure part-way through
    does NOT roll back the earlier writes.
    """

    task_id: str
    status: str
    events: int
    selected_skills: tuple[str, ...]
    files_changed: tuple[dict[str, Any], ...]
    last_shell: Optional[dict[str, Any]]
    failed_edits: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "events": self.events,
            "selected_skills": list(self.selected_skills),
            "files_changed": [dict(f) for f in self.files_changed],
            "failed_edits": [dict(f) for f in self.failed_edits],
            "last_shell": dict(self.last_shell) if self.last_shell is not None else None,
        }


# ---------------------------------------------------------------------------
# EventLog read-back helpers
# ---------------------------------------------------------------------------


_EDIT_TOOLS = frozenset({"Edit", "Write"})
_SHELL_TOOLS = frozenset({"shell_run", "git_status", "git_diff"})


def _iter_tool_results(
    events: list[EventEnvelope], content_store: ContentStore
) -> list[tuple[str, dict[str, Any]]]:
    """``(tool_name, output_dict)`` for every successful ``ToolResultRecorded``.

    ``output`` is restored from the ``output_ref`` artifact rather than the
    inline summary, which is too lossy to carry the full ``files_changed``
    shape."""
    pairs: list[tuple[str, dict[str, Any]]] = []
    call_id_to_name: dict[str, str] = {}
    for env in events:
        if env.type == "ToolCallStarted":
            payload = env.payload
            call_id_to_name[payload.call_id] = payload.tool_name
        elif env.type == "ToolResultRecorded":
            payload = env.payload
            if not payload.success:
                continue
            tool_name = call_id_to_name.get(payload.call_id)
            if tool_name is None:
                continue
            ref = getattr(payload, "output_ref", None)
            if ref is None:
                continue
            try:
                body = content_store.get(ref)
                output = json.loads(body.decode("utf-8"))
            except Exception:  # noqa: BLE001 — malformed recording, skip
                continue
            if isinstance(output, dict):
                pairs.append((tool_name, output))
    return pairs


def _collect_files_changed(
    events: list[EventEnvelope], content_store: ContentStore
) -> tuple[dict[str, Any], ...]:
    """Summarise the files the edit tools touched.

    The tools' model-facing output is plain text now, so the machine fields
    come from the durable records instead: ``path`` from the recorded
    ``ToolCallStarted`` arguments and ``applied`` from the event summary's
    ``(applied)`` / ``(…, applied)`` marker. ``applied=False`` rows are kept:
    a proposed diff is still part of the session record."""
    out: list[dict[str, Any]] = []
    call_id_to_started: dict[str, Any] = {}
    for env in events:
        if env.type == "ToolCallStarted":
            call_id_to_started[env.payload.call_id] = env.payload
        elif env.type == "ToolResultRecorded":
            payload = env.payload
            if not payload.success:
                continue
            started = call_id_to_started.get(payload.call_id)
            if started is None or started.tool_name not in _EDIT_TOOLS:
                continue
            args = resolve_tool_call_arguments(started, content_store)
            path_raw = args.get("file_path")
            summary = payload.summary or ""
            out.append(
                {
                    "tool": started.tool_name,
                    "path": path_raw if isinstance(path_raw, str) else None,
                    "applied": summary.rstrip().endswith("applied)"),
                }
            )
    return tuple(out)


def _collect_failed_edits(
    events: list[EventEnvelope], content_store: ContentStore
) -> tuple[dict[str, Any], ...]:
    """Rows for every ``edit`` call the tool reported as ``success=False``.

    Scoped to ``edit``; ``write`` failures are not projected here. The rows are
    machine-readable — ``{"tool", "path", "success": False, "reason",
    "summary", "call_id"}`` — so a consumer never has to scrape the human
    summary prose.

    ``path`` is read from the recorded ``ToolCallStarted`` arguments
    (dereferenced from the ContentStore when they were offloaded): the recorded
    input is the source of truth, and summary text is a human-side rendering
    that must not become a machine field's primary source. ``reason`` does fall
    back to that summary, because a failed ``edit`` returns ``output=None`` and
    leaves no structured failure object to consult. ``summary`` is kept verbatim
    so a rendered line stays byte-identical to what the EventLog carried.
    """
    out: list[dict[str, Any]] = []
    call_id_to_name: dict[str, str] = {}
    call_id_to_started: dict[str, Any] = {}
    for env in events:
        if env.type == "ToolCallStarted":
            payload = env.payload
            call_id_to_name[payload.call_id] = payload.tool_name
            # Keep the started payload; arguments are dereferenced lazily
            # below only for the rare failed edit (avoids a
            # ContentStore read per call when arguments were offloaded).
            call_id_to_started[payload.call_id] = payload
        elif env.type == "ToolResultRecorded":
            payload = env.payload
            if payload.success:
                continue
            tool_name = call_id_to_name.get(payload.call_id)
            if tool_name != "Edit":
                continue
            started = call_id_to_started.get(payload.call_id)
            args = (
                resolve_tool_call_arguments(started, content_store)
                if started is not None
                else {}
            )
            path_raw = args.get("file_path")
            path = path_raw if isinstance(path_raw, str) else None
            reason = _extract_reason(payload, tool_name)
            out.append(
                {
                    "tool": tool_name,
                    "path": path,
                    "success": False,
                    "reason": reason,
                    "summary": payload.summary,
                    "call_id": payload.call_id,
                }
            )
    return tuple(out)


def _extract_reason(payload: Any, tool_name: str) -> str:
    """Render the failure reason of a recorded ``ToolResultRecorded``.

    ``ToolResultRecordedPayload`` carries no inline ``output`` — only an
    ``output_ref``, which for an fs edit failure holds a serialised ``None``.
    So the reason is the inline ``summary`` with its leading ``<tool>: ``
    prefix stripped, which reads cleanly beside the row's separate ``tool``
    field. A tool that emits a structured failure object could widen this to
    read the ref instead.
    """
    summary = payload.summary or ""
    prefix = f"{tool_name}: "
    if summary.startswith(prefix):
        return summary[len(prefix):]
    return summary


def _last_shell_result(
    events: list[EventEnvelope], content_store: ContentStore
) -> Optional[dict[str, Any]]:
    """Compact summary of the final shell / test tool call, if any.

    Serves as the session's "test result" line: the LLM chooses what to run,
    this projection only surfaces the last thing it ran."""
    last: Optional[dict[str, Any]] = None
    for tool_name, output in _iter_tool_results(events, content_store):
        if tool_name not in _SHELL_TOOLS:
            continue
        last = {
            "tool": tool_name,
            "command": output.get("command"),
            "returncode": output.get("returncode"),
            "duration_ms": output.get("duration_ms"),
            "timed_out": output.get("timed_out"),
        }
    return last


def _last_selected_skills(
    events: list[EventEnvelope], content_store: ContentStore
) -> tuple[str, ...]:
    """``ContextPlan.selected_skills`` from the most recent
    ``ContextPlanComposed`` — the plan body lives behind ``plan_ref``, not on
    the event."""
    selected: tuple[str, ...] = ()
    for env in events:
        if env.type != "ContextPlanComposed":
            continue
        ref = getattr(env.payload, "plan_ref", None)
        if ref is None:
            continue
        try:
            body = content_store.get(ref)
            plan = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        raw = plan.get("selected_skills")
        if isinstance(raw, list):
            selected = tuple(str(x) for x in raw)
    return selected
