"""MS1 — message-selection provenance.

Covers: the policy helper (`_build_request_and_selection` — always
records a summary, `dropped=0` when under the limit; correct counts on
truncation); the crown-jewel boundary (`LLMRequest` canonical bytes /
`request_ref` hash unchanged — selection is event-only, never on the
request); provider/fake/stub `complete(request)` signatures unchanged
while the runtime client takes the `selection` kwarg; `RuntimeLLMClient`
is the persister; the explicit sqlite restorer (missing / typed /
plain-dict / malformed); a fresh truncating recording records
`dropped>0`; an old-shape payload with no selection persists/restores as
`None`; and the audit/trace projection of the 5 scalar fields.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional

from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.observers.audit import AuditObserver, AuditRecord
from noeta.policies.react import ReActPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.events import LLMRequestStartedPayload, MessageSelection
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.task import Task, TaskState
from noeta.protocols.values import ContentRef
from noeta.protocols.view import View
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.sqlite.eventlog import (
    SqliteEventLog,
    _restore_llm_request_started_payload,
)
from noeta.testing.composer import trivial_three_segment
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.risk_level = "low"
        self.input_schema = {"type": "object", "additionalProperties": True}

    def invoke(self, arguments: Any, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _view_with_history(n: int) -> View:
    cs = InMemoryContentStore()
    composer = ThreeSegmentComposer(
        system_prompt="be helpful",
        tools={"echo": _FakeTool("echo")},
        content_store=cs,
    )
    task = Task(task_id="t-1", state=TaskState())
    task.runtime.messages.extend(
        Message(role="user", content=[TextBlock(text=f"m{i}")]) for i in range(n)
    )
    return composer.compose(task)


def _policy(llm: Any, *, max_history_messages: int = 50) -> ReActPolicy:
    return ReActPolicy(
        llm=llm,
        tools={"echo": _FakeTool("echo")},
        system_prompt="unused",
        model="m",
        max_history_messages=max_history_messages,
    )


# ---------------------------------------------------------------------------
# 1. Policy helper (B4): direct assertions on counts + presence semantics
# ---------------------------------------------------------------------------


def test_build_request_and_selection_no_truncation_still_records_dropped_zero() -> None:
    view = _view_with_history(3)
    policy = _policy(object(), max_history_messages=50)
    req, sel = policy._build_request_and_selection(view)
    n = len(view.iter_messages())
    assert len(req.messages) == n          # nothing dropped
    assert sel.strategy == "tail_window"
    assert sel.candidates == n
    assert sel.selected == n
    assert sel.dropped == 0                # present even with no truncation
    assert sel.limit == 50


def test_default_max_history_none_no_truncation_no_selection() -> None:
    """The count-based tail-window guard is DEFAULT-OFF.

    With ``max_history_messages`` left at its (new) ``None`` default, even a
    long history is passed through whole and NO ``tail_window``
    ``MessageSelection`` is produced — its absence is the signal that no
    count-based truncation happened (pure token compaction is the only gate
    now)."""
    view = _view_with_history(120)
    # Construct directly so we exercise ReActPolicy's OWN default (``None``),
    # not the test-helper's legacy ``50`` default.
    policy = ReActPolicy(
        llm=object(),
        tools={"echo": _FakeTool("echo")},
        system_prompt="unused",
        model="m",
    )
    req, sel = policy._build_request_and_selection(view)
    n = len(view.iter_messages())
    assert n == 120
    assert len(req.messages) == n          # nothing dropped, whole history kept
    assert sel is None                     # no count-based selection emitted


def test_build_request_and_selection_truncation_counts_match_request() -> None:
    view = _view_with_history(10)
    candidates = len(view.iter_messages())
    assert candidates > 2
    policy = _policy(object(), max_history_messages=2)
    req, sel = policy._build_request_and_selection(view)
    assert len(req.messages) == 2          # request actually truncated
    assert sel.candidates == candidates
    assert sel.selected == 2
    assert sel.dropped == candidates - 2   # dropped > 0 == truncation happened
    assert sel.limit == 2
    # the kept messages are the tail
    assert req.messages == view.iter_messages()[-2:]


# ---------------------------------------------------------------------------
# 2. Crown jewel: LLMRequest bytes / request_ref unchanged by selection
# ---------------------------------------------------------------------------


def _one_shot_request() -> LLMRequest:
    view = _view_with_history(4)
    req, _ = _policy(object())._build_request_and_selection(view)
    return req


def test_llm_request_canonical_bytes_carry_no_selection() -> None:
    req = _one_shot_request()
    raw = to_canonical_bytes(req)
    # selection is event metadata, never on the request → not in its bytes
    assert b"selection" not in raw
    assert b"message_selection" not in raw


def test_request_ref_hash_identical_with_and_without_selection() -> None:
    """The recorded ``request_ref`` for one request is byte-identical
    whether or not a selection is passed to ``complete`` — selection never
    reaches ``_put_request``."""
    req = _one_shot_request()
    sel = MessageSelection(
        strategy="tail_window", candidates=4, selected=4, dropped=0, limit=50
    )

    def _run(selection: Optional[MessageSelection]) -> ContentRef:
        log = InMemoryEventLog()
        cs = InMemoryContentStore()
        client = RuntimeLLMClient(
            provider=FakeLLMProvider(
                responses=[LLMResponse(stop_reason="end_turn", content=[])]
            ),
            event_log=log,
            content_store=cs,
        )
        ctx = StepContext(task_id="t1", lease_id="l", trace_id="tr")
        client.complete(req, ctx, selection=selection)
        started = [e for e in log.read("t1") if e.type == "LLMRequestStarted"][0]
        ref: ContentRef = started.payload.request_ref
        return ref

    assert _run(sel) == _run(None)         # request_ref independent of selection


# ---------------------------------------------------------------------------
# 3. Signatures: providers/fakes unchanged; runtime clients take the kwarg
# ---------------------------------------------------------------------------


def test_provider_and_fake_complete_signatures_unchanged() -> None:
    from noeta.providers.openai_compat import OpenAICompatProvider
    from noeta.providers.anthropic import AnthropicProvider
    from noeta.testing.stub_provider import StubProvider as CliStub
    from tests._stub_provider import CodeStubProvider as CodeStub

    for cls in (
        OpenAICompatProvider,
        AnthropicProvider,
        FakeLLMProvider,
        CliStub,
        CodeStub,
    ):
        params = list(inspect.signature(cls.complete).parameters)
        assert params == ["self", "request"], f"{cls.__name__}: {params}"


def test_runtime_client_takes_selection_kwarg() -> None:
    sig = inspect.signature(RuntimeLLMClient.complete)
    assert "selection" in sig.parameters
    assert sig.parameters["selection"].kind == inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# 4. sqlite restorer (B2): missing / typed / plain-dict / malformed
# ---------------------------------------------------------------------------


def _started_dict(selection: Any, *, with_key: bool = True) -> dict[str, Any]:
    ref = ContentRef(hash="h" * 64, size=3, media_type="application/json")
    d: dict[str, Any] = {"call_id": "c1", "model": "m", "request_ref": ref,
                         "input_tokens": 0}
    if with_key:
        d["selection"] = selection
    return d


def test_restorer_missing_selection_is_none() -> None:
    payload = _restore_llm_request_started_payload(
        _started_dict(None, with_key=False)
    )
    assert payload.selection is None


def test_restorer_already_typed_selection_passes_through() -> None:
    sel = MessageSelection(
        strategy="tail_window", candidates=5, selected=2, dropped=3, limit=2
    )
    payload = _restore_llm_request_started_payload(_started_dict(sel))
    assert payload.selection is sel


def test_restorer_plain_dict_rebuilt_to_typed() -> None:
    payload = _restore_llm_request_started_payload(
        _started_dict(
            {"strategy": "tail_window", "candidates": 5, "selected": 2,
             "dropped": 3, "limit": 2}
        )
    )
    assert payload.selection == MessageSelection(
        strategy="tail_window", candidates=5, selected=2, dropped=3, limit=2
    )


def test_restorer_malformed_dict_fails_loud() -> None:
    import pytest

    with pytest.raises(KeyError):
        _restore_llm_request_started_payload(
            _started_dict({"strategy": "tail_window"})  # missing the counts
        )


def test_restorer_unexpected_shape_fails_loud() -> None:
    import pytest

    with pytest.raises(TypeError):
        _restore_llm_request_started_payload(_started_dict("not-a-selection"))


# ---------------------------------------------------------------------------
# 5. old-shape (no selection) persists + restores as None through sqlite
# ---------------------------------------------------------------------------


def test_sqlite_roundtrip_no_selection_restores_none(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "k.db")
    try:
        log.emit(
            task_id="t1",
            type="LLMRequestStarted",
            payload=LLMRequestStartedPayload(
                call_id="c1", model="m",
                request_ref=ContentRef(
                    hash="a" * 64, size=3, media_type="application/json"
                ),
                # no selection → old-shape
            ),
        )
        env = [e for e in log.read("t1") if e.type == "LLMRequestStarted"][0]
        assert env.payload.selection is None
    finally:
        log.close()


def test_sqlite_roundtrip_with_selection_restores_typed(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "k.db")
    sel = MessageSelection(
        strategy="tail_window", candidates=9, selected=2, dropped=7, limit=2
    )
    try:
        log.emit(
            task_id="t1",
            type="LLMRequestStarted",
            payload=LLMRequestStartedPayload(
                call_id="c1", model="m",
                request_ref=ContentRef(
                    hash="a" * 64, size=3, media_type="application/json"
                ),
                selection=sel,
            ),
        )
        env = [e for e in log.read("t1") if e.type == "LLMRequestStarted"][0]
        assert env.payload.selection == sel       # tagged → auto-rehydrated typed
        assert isinstance(env.payload.selection, MessageSelection)
    finally:
        log.close()


# ---------------------------------------------------------------------------
# 6. End-to-end: a truncating recording records dropped>0
# ---------------------------------------------------------------------------


_SYSTEM = "You answer succinctly using the echo tool."
_GOAL = "say hi via echo, then finish"


def _echo_tool() -> FakeTool:
    return FakeTool(
        name="echo",
        script={("hi",): "echo-said: hi"},
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


def _script() -> list[LLMResponse]:
    return [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(call_id="tc-1", tool_name="echo",
                             arguments={"text": "hi"})
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "r1"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="all done")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "r2"},
        ),
    ]


def _record_truncating_loop() -> tuple[InMemoryEventLog, InMemoryContentStore, str]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    cs = InMemoryContentStore()
    provider = FakeLLMProvider(responses=_script())
    normal = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)
    tool = _echo_tool()
    # max_history_messages=1 → the second round-trip (history has the user
    # goal + assistant tool_use + tool result) is forced to truncate.
    policy = ReActPolicy(
        llm=normal, tools={"echo": tool}, system_prompt=_SYSTEM,
        model="gpt-test", max_steps=5, max_history_messages=1,
    )
    engine = Engine(
        event_log=log, content_store=cs,
        composer=trivial_three_segment(cs), policy=policy,
        tools={"echo": tool},
    )
    task = engine.create_task(goal=_GOAL, policy_name="react")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="rec")
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text=_GOAL)], lease_id=lease.lease_id)
    engine.run_one_step(task, lease_id=lease.lease_id)
    return log, cs, task.task_id


def test_truncating_recording_records_dropped() -> None:
    log, cs, task_id = _record_truncating_loop()
    started = [e for e in log.read(task_id) if e.type == "LLMRequestStarted"]
    assert started, "recording captured no LLM request"
    sels = [e.payload.selection for e in started]
    assert all(s is not None for s in sels)
    assert all(s.strategy == "tail_window" and s.limit == 1 for s in sels)
    # at least one round-trip truncated (history grew past the limit of 1)
    assert any(s.dropped > 0 for s in sels)


# ---------------------------------------------------------------------------
# 7. Audit / trace projection of the 5 scalar fields (B6)
# ---------------------------------------------------------------------------


def test_audit_projects_selection_as_five_scalar_fields() -> None:
    log = InMemoryEventLog()
    seen: list[AuditRecord] = []
    obs = AuditObserver(event_log=log, sink=seen.append)
    try:
        log.emit(
            task_id="t1",
            type="LLMRequestStarted",
            payload=LLMRequestStartedPayload(
                call_id="c1", model="m",
                request_ref=ContentRef(
                    hash="a" * 64, size=3, media_type="application/json"
                ),
                selection=MessageSelection(
                    strategy="tail_window", candidates=9, selected=2,
                    dropped=7, limit=2,
                ),
            ),
        )
    finally:
        obs.stop()
    rec = [r for r in seen if r.type == "LLMRequestStarted"][0]
    assert rec.payload_summary["selection"] == {
        "strategy": "tail_window", "candidates": 9, "selected": 2,
        "dropped": 7, "limit": 2,
    }
    # request_ref still flattened to its metadata only; no message bodies.
    assert set(rec.payload_summary["request_ref"]) == {"hash", "size", "media_type"}
