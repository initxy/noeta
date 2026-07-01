"""AuditObserver: sink projection + classification guard + thread safety."""

from __future__ import annotations

import inspect
import threading

import pytest

from noeta.observers.audit import (
    _SUMMARY_FIELDS_BY_EVENT,
    _TYPE_ONLY_EVENTS,
    AuditObserver,
    AuditRecord,
    _summarize,
)
from noeta.protocols.events import (
    ContextPlanComposedPayload,
    LLMRequestFinishedPayload,
    LLMRequestStartedPayload,
    LLMResponseRecordedPayload,
    MessagesAppendedPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskStatePatchedPayload,
    ToolCallStartedPayload,
    ToolResultRecordedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.storage.memory import InMemoryEventLog


def _ref(tag: str, size: int = 10) -> ContentRef:
    return ContentRef(hash=tag * 64, size=size, media_type="application/json")


# ---------------------------------------------------------------------------
# Classification guard (issue 19 B2 hard gate)
# ---------------------------------------------------------------------------


def test_summary_field_names_match_real_payload_dataclasses() -> None:
    """Every field name in ``_SUMMARY_FIELDS_BY_EVENT`` must exist on
    the corresponding ``*Payload`` dataclass — otherwise a typo
    silently drops an intended audit field via the ``hasattr`` skip
    in ``_summarize_whitelisted``. Catches drift in either direction:
    fields renamed on the payload, or typos in the allowlist."""
    import noeta.protocols.events as events_module
    from dataclasses import fields as dc_fields

    for event_type, allowed in _SUMMARY_FIELDS_BY_EVENT.items():
        cls = getattr(events_module, f"{event_type}Payload", None)
        assert cls is not None, (
            f"_SUMMARY_FIELDS_BY_EVENT references unknown payload "
            f"class for event type {event_type!r}"
        )
        declared = {f.name for f in dc_fields(cls)}
        missing = set(allowed) - declared
        assert not missing, (
            f"{event_type}Payload missing fields listed in "
            f"_SUMMARY_FIELDS_BY_EVENT: {sorted(missing)}"
        )


def test_summarize_classification_covers_every_payload_type() -> None:
    """Every *Payload class in noeta.protocols.events must be
    classified into either _SUMMARY_FIELDS_BY_EVENT (value
    allowlist) or _TYPE_ONLY_EVENTS (type-only projection).
    New payloads cannot silently slide into the fallback path —
    that would weaken the audit boundary by smuggling unknown
    fields out without an explicit allow decision."""
    import noeta.protocols.events as events_module

    payload_classes = [
        cls
        for name, cls in inspect.getmembers(events_module, inspect.isclass)
        if name.endswith("Payload") and cls.__module__ == events_module.__name__
    ]
    expected = {cls.__name__.removesuffix("Payload") for cls in payload_classes}
    classified = set(_SUMMARY_FIELDS_BY_EVENT) | _TYPE_ONLY_EVENTS

    missing = expected - classified
    overlap = set(_SUMMARY_FIELDS_BY_EVENT) & _TYPE_ONLY_EVENTS

    assert not missing, (
        f"unclassified payload types: {sorted(missing)} — add to "
        "_SUMMARY_FIELDS_BY_EVENT (allowlist) or _TYPE_ONLY_EVENTS"
    )
    assert not overlap, (
        f"types in both classification sets: {sorted(overlap)} — pick one"
    )


# ---------------------------------------------------------------------------
# Per-event _summarize behaviour
# ---------------------------------------------------------------------------


def test_summarize_tool_call_started_does_not_leak_arguments() -> None:
    payload = ToolCallStartedPayload(
        call_id="c1", tool_name="echo", arguments={"secret": "leak"}
    )
    summary = _summarize("ToolCallStarted", payload)
    assert summary == {"call_id": "c1", "tool_name": "echo"}
    assert "arguments" not in summary


def test_summarize_task_completed_is_type_only_no_answer_value() -> None:
    payload = TaskCompletedPayload(answer="42 and the secret password")
    summary = _summarize("TaskCompleted", payload)
    assert summary == {
        "_type": "TaskCompletedPayload",
        # field NAMES only — the audit summary never leaks the answer VALUE.
        # ``answer_ref`` is the ContentStore spill pointer for large answers.
        "fields": ["answer", "answer_ref"],
    }
    assert "answer" not in summary


def test_summarize_task_created_is_type_only_no_goal_or_inputs_value() -> None:
    payload = TaskCreatedPayload(
        goal="do the thing", policy_name="stub", inputs={"x": "private"}
    )
    summary = _summarize("TaskCreated", payload)
    assert summary["_type"] == "TaskCreatedPayload"
    # fields list contains the declared dataclass fields; values absent.
    assert "goal" in summary["fields"]
    assert "inputs" in summary["fields"]
    # Crucially no values:
    for v in summary["fields"]:
        # ``fields`` is a list[str] for the type-only mode.
        assert isinstance(v, str)


def test_summarize_task_state_patched_is_type_only_no_patch_value() -> None:
    payload = TaskStatePatchedPayload(patch={"private": "memory"})
    summary = _summarize("TaskStatePatched", payload)
    assert summary["_type"] == "TaskStatePatchedPayload"
    assert "private" not in str(summary)
    assert "memory" not in str(summary)


def test_summarize_messages_appended_projects_ref_metadata_only() -> None:
    ref = _ref("a", size=1234)
    payload = MessagesAppendedPayload(messages_ref=ref, count=3)
    summary = _summarize("MessagesAppended", payload)
    assert summary["count"] == 3
    assert summary["messages_ref"] == {
        "hash": "a" * 64,
        "size": 1234,
        "media_type": "application/json",
    }


def test_summarize_llm_request_started_projects_request_ref_metadata_only() -> None:
    ref = _ref("b", size=512)
    payload = LLMRequestStartedPayload(
        call_id="L1", model="gpt-4", request_ref=ref, input_tokens=100
    )
    summary = _summarize("LLMRequestStarted", payload)
    assert summary["call_id"] == "L1"
    assert summary["model"] == "gpt-4"
    assert summary["request_ref"] == {
        "hash": "b" * 64,
        "size": 512,
        "media_type": "application/json",
    }
    # input_tokens is not in the allowlist; it must not appear.
    assert "input_tokens" not in summary


def test_summarize_llm_response_recorded_projects_response_ref_metadata_only() -> None:
    payload = LLMResponseRecordedPayload(
        call_id="L1", response_ref=_ref("c"), stop_reason="end_turn"
    )
    summary = _summarize("LLMResponseRecorded", payload)
    assert summary["stop_reason"] == "end_turn"
    assert set(summary["response_ref"].keys()) == {"hash", "size", "media_type"}


def test_summarize_llm_request_finished_surfaces_cost_usd() -> None:
    payload = LLMRequestFinishedPayload(
        call_id="L1", success=True, cost_usd=0.42, latency_ms=120
    )
    summary = _summarize("LLMRequestFinished", payload)
    assert summary["cost_usd"] == 0.42
    # latency_ms not in allowlist.
    assert "latency_ms" not in summary


def test_summarize_context_plan_composed_projects_plan_ref_metadata_only() -> None:
    payload = ContextPlanComposedPayload(plan_ref=_ref("d", size=8))
    summary = _summarize("ContextPlanComposed", payload)
    assert summary == {
        "plan_ref": {"hash": "d" * 64, "size": 8, "media_type": "application/json"}
    }


def test_summarize_tool_result_recorded_projects_output_ref() -> None:
    payload = ToolResultRecordedPayload(
        call_id="c1",
        success=True,
        output_ref=_ref("e", size=99),
        summary="all good",
    )
    summary = _summarize("ToolResultRecorded", payload)
    assert summary["summary"] == "all good"
    assert summary["output_ref"] == {
        "hash": "e" * 64,
        "size": 99,
        "media_type": "application/json",
    }


def test_summarize_unknown_event_type_falls_back_to_typename_only() -> None:
    """An event type missing from both classification sets falls into
    the forward-compat fallback. The reflection guard prevents this
    from happening for real schemas in repo, but the fallback itself
    must still keep values out."""
    summary = _summarize("FutureType", {"x": "secret", "n": 7})
    assert summary["_type"] == "dict"
    # Values not surfaced.
    for entry in summary["fields"]:
        assert isinstance(entry, dict)
        for key, type_name in entry.items():
            assert type_name in {"str", "int", "float", "bool", "list", "dict"}
            assert "secret" not in str(entry[key])


# ---------------------------------------------------------------------------
# AuditObserver wiring
# ---------------------------------------------------------------------------


def test_audit_observer_invokes_sink_with_full_envelope_metadata() -> None:
    """AuditRecord (B3) must carry the full EventEnvelope metadata
    footprint so external sinks can dedup / trace causality without
    re-querying the EventLog."""
    log = InMemoryEventLog()
    captured: list[AuditRecord] = []
    obs = AuditObserver(event_log=log, sink=captured.append)
    try:
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            trace_id="trace-abc",
        )
    finally:
        obs.stop()

    assert len(captured) == 1
    record = captured[0]
    expected_fields = {
        "id", "task_id", "seq", "type", "schema_version",
        "occurred_at", "actor", "trace_id", "correlation_id",
        "causation_id", "origin", "payload_summary",
    }
    actual_fields = {f for f in dir(record) if not f.startswith("_")}
    assert expected_fields.issubset(actual_fields)
    assert record.task_id == "t1"
    assert record.type == "TaskCreated"
    assert record.trace_id == "trace-abc"
    assert record.origin == "engine"
    assert record.seq == 0


def test_audit_observer_default_sink_logs_at_info(caplog) -> None:
    log = InMemoryEventLog()
    obs = AuditObserver(event_log=log)
    try:
        with caplog.at_level("INFO", logger="noeta.observers.audit"):
            log.emit(
                task_id="t1",
                type="TaskCreated",
                payload=TaskCreatedPayload(goal="g", policy_name="p"),
            )
    finally:
        obs.stop()
    audit_records = [
        rec.audit
        for rec in caplog.records
        if rec.name == "noeta.observers.audit" and hasattr(rec, "audit")
    ]
    assert len(audit_records) == 1
    assert isinstance(audit_records[0], AuditRecord)


def test_audit_observer_sink_raise_does_not_break_eventlog() -> None:
    log = InMemoryEventLog()

    def boom(_: AuditRecord) -> None:
        raise RuntimeError("sink kaboom")

    obs = AuditObserver(event_log=log, sink=boom)
    try:
        ev = log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
        )
    finally:
        obs.stop()
    assert ev.seq == 0
    assert len(log.read("t1")) == 1


def test_audit_observer_stop_idempotent_and_silences_callbacks() -> None:
    log = InMemoryEventLog()
    captured: list[AuditRecord] = []
    obs = AuditObserver(event_log=log, sink=captured.append)
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    obs.stop()
    obs.stop()  # idempotent — must not raise
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=__import__(
            "noeta.protocols.events", fromlist=["TaskStartedPayload"]
        ).TaskStartedPayload(lease_id="L"),
    )
    # Observer is stopped, so the second emit never reaches the sink.
    assert len(captured) == 1


def test_audit_observer_thread_safe_under_concurrent_writes() -> None:
    """Issue 19 B1 stress: concurrent EventLog writes from multiple
    threads must reach the sink without dropping records. We do not
    assert order — only that every emit produces a record and that
    the ``(task_id, seq)`` set matches the emits."""
    log = InMemoryEventLog()
    captured: list[AuditRecord] = []
    obs = AuditObserver(event_log=log, sink=captured.append)

    def worker(task_id: str, n: int) -> None:
        for _ in range(n):
            log.emit(
                task_id=task_id,
                type="TaskCreated",
                payload=TaskCreatedPayload(goal="g", policy_name="p"),
            )

    threads = [
        threading.Thread(target=worker, args=(f"t-{i}", 50)) for i in range(5)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        obs.stop()

    assert all(not t.is_alive() for t in threads)
    assert len(captured) == 250
    # Each task got exactly 50 emits; the (task_id, seq) set is
    # exhaustive and unique.
    seqs_per_task: dict[str, set[int]] = {}
    for rec in captured:
        seqs_per_task.setdefault(rec.task_id, set()).add(rec.seq)
    for task_id, seqs in seqs_per_task.items():
        assert seqs == set(range(50)), (task_id, seqs)


# ---------------------------------------------------------------------------
# ContentRef projection invariants
# ---------------------------------------------------------------------------


def test_contentref_projections_only_carry_three_keys() -> None:
    """Every ContentRef value in a summary must reduce to exactly
    ``{hash, size, media_type}``."""
    payload = MessagesAppendedPayload(
        messages_ref=_ref("z", size=42), count=1
    )
    summary = _summarize("MessagesAppended", payload)
    assert set(summary["messages_ref"].keys()) == {"hash", "size", "media_type"}
