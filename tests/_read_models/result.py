"""Code-session result dataclass + EventLog read-back helpers.

``CodeSessionResult`` is the output shape :class:`AgentSessionRunner` returns
for the CLI to render; the module-private helpers walk the durable EventLog
to project files-changed / failed-edits / last-shell / selected-skills out of
``ToolResultRecorded`` and ``ContextPlanComposed`` events.
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
    """Output of :meth:`CodeSessionRunner.run` for the CLI to render.

    ``failed_edits`` (Phase 4.5 I4) carries every ``edit``
    call that ended in ``ToolResult.success=False`` so the operator
    (or downstream tooling reading ``to_json()``) can tell which
    edits the model attempted but could not apply. ``write``
    failure reporting is **deferred** to a later slice — those
    failures still appear in the EventLog as
    ``ToolResultRecorded(success=False)`` events but do not surface
    in ``failed_edits``. Phase 4 semantics stay honestly
    **non-atomic** — a failure in the middle of a multi-file
    sequence does NOT roll back earlier writes.
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


# replace_text → edit, write_file → write.
_EDIT_TOOLS = frozenset({"edit", "write"})
_SHELL_TOOLS = frozenset({"shell_run", "git_status", "git_diff"})


def _iter_tool_results(
    events: list[EventEnvelope], content_store: ContentStore
) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(tool_name, output_dict)`` for every successful
    ``ToolResultRecorded`` in the stream. ``output`` is restored from
    the ``output_ref`` artifact (canonical JSON) — the inline summary
    is too small for the full ``files_changed`` shape we want."""
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
    """Walk ``ToolResultRecorded`` payloads from the edit tools and
    summarise files changed. ``applied=False`` rows are kept (proposed
    diffs are still part of the session record)."""
    out: list[dict[str, Any]] = []
    for tool_name, output in _iter_tool_results(events, content_store):
        if tool_name == "apply_patch":
            # M1: one batch tool result → one row per edit in the batch.
            for e in output.get("edits") or []:
                if not isinstance(e, dict):
                    continue
                out.append(
                    {
                        "tool": "apply_patch",
                        "path": e.get("path"),
                        "applied": e.get("applied"),
                        "added": e.get("added"),
                        "removed": e.get("removed"),
                        "before_sha256": e.get("before_sha256"),
                        "after_sha256": e.get("after_sha256"),
                    }
                )
            continue
        if tool_name not in _EDIT_TOOLS:
            continue
        out.append(
            {
                "tool": tool_name,
                "path": output.get("path"),
                "applied": output.get("applied"),
                "added": output.get("added"),
                "removed": output.get("removed"),
                "before_sha256": output.get("before_sha256"),
                "after_sha256": output.get("after_sha256"),
            }
        )
    return tuple(out)


def _collect_failed_edits(
    events: list[EventEnvelope], content_store: ContentStore
) -> tuple[dict[str, Any], ...]:
    """Walk ``ToolResultRecorded`` for ``edit`` calls (formerly
    ``replace_text``) where the tool returned ``success=False``
    (Phase 4.5 I4).

    **Scope** (per architect rev): this helper covers ``edit``
    only. ``write`` failure-reporting (existing-path / over-size /
    parent-missing branches) is **out of scope** for this slice and is
    deferred to a later issue — both to keep the slice focused on the
    multi-file ``edit`` UX it advertises and so the tests
    actually exercise every branch the field carries.

    Per the architect's review watchpoint, ``CodeSessionResult.to_json()``
    must carry these rows in a machine-readable list so downstream
    tooling does not have to scrape the human summary prose.

    Row shape pinned by the architect (#noeta:dfab2667 follow-up):

        {"tool": "edit", "path": str | None, "success": False,
         "reason": str, "summary": str, "call_id": str}

    Sources for each field, in order of preference:

    * ``path`` — read from the **recorded ``ToolCallStarted``
      arguments** keyed by call_id (dereferenced from the ContentStore
      when the call's arguments were offloaded). The recorded input is the
      source of truth; summary text is a human-side rendering and must not
      be the machine field's primary source.
    * ``reason`` — the recorded ``summary`` text with the leading
      ``"edit: "`` prefix stripped (Phase 4 ``edit``
      returns ``output=None`` on failure, so there is no structured
      output to consult). Future tools that emit a structured failure
      object can widen :func:`_extract_reason`.
    * ``summary`` — the original recorded ``summary`` text, kept
      verbatim so the human-readable line in ``_format_summary``
      stays byte-identical to what the EventLog carried.
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
            # Scope (Phase 4.5 I4): edit only (formerly replace_text). write
            # failure reporting is deferred.
            if tool_name != "edit":
                continue
            started = call_id_to_started.get(payload.call_id)
            args = (
                resolve_tool_call_arguments(started, content_store)
                if started is not None
                else {}
            )
            path_raw = args.get("path")
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
    """Render the failure reason for a recorded ``ToolResultRecorded``.

    ``ToolResultRecordedPayload`` does NOT carry an inline ``output``
    field — only an ``output_ref`` artifact, which for Phase 4 fs edit
    failures contains the serialised ``None`` (no structured reason).
    The reason is therefore the inline ``summary`` text with the
    leading ``<tool>: `` prefix stripped so the row reads cleanly
    when concatenated with the row's separate ``tool`` field. Future
    tools that emit a structured failure object can widen this helper
    by reading the ``output_ref`` artifact instead.
    """
    summary = payload.summary or ""
    prefix = f"{tool_name}: "
    if summary.startswith(prefix):
        return summary[len(prefix):]
    return summary


def _last_shell_result(
    events: list[EventEnvelope], content_store: ContentStore
) -> Optional[dict[str, Any]]:
    """Return a compact summary of the final shell / test tool call,
    if any. Used as the 'test result' line in the session summary —
    the LLM chooses what to run, the runner just surfaces it."""
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
    """Pull ``ContextPlan.selected_skills`` from the most recent
    ``ContextPlanComposed`` event (Composer wrote the body to
    ContentStore via ``plan_ref``)."""
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
