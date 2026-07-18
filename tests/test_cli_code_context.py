"""CW17 — recorded context/provenance view (``build_code_context_view``).

Projects recorded ContextPlanComposed / ContextPlan + LLMRequestStarted.selection
without re-running the composer, building a provider/driver, or reading the
workspace. Bodies are never emitted — only ContentRef summaries + counts.

The operator-CLI surface that wrapped this (the removed ``noeta code context``
subcommand: argv dispatch, --json/--all flags, exit codes, stdout/stderr
guards) is gone with the operator CLI. This file keeps the library-reachable
coverage: the pure ``build_code_context_view`` seam (latest-vs-all projection,
non-code → None).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from tests._read_models.context_view import build_code_context_view
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.events import (
    ContextPlanComposedPayload,
    LLMRequestStartedPayload,
    MessageSelection,
    TaskCreatedPayload,
    TaskStartedPayload,
)
from noeta.storage.sqlite import SqliteReadOnlyStore
from noeta.testing.profile import build_sqlite_stack


def _close(*objs: Any) -> None:
    for obj in objs:
        close = getattr(obj, "close", None)
        if callable(close):
            close()


def _seed(db: Path) -> tuple[Any, Any, Any]:
    return build_sqlite_stack(str(db))


def _emit_created_started(log: Any, task_id: str, *, agent: str = "default") -> None:
    log.emit(
        task_id=task_id, type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="react", agent_name=agent),
    )
    log.emit(
        task_id=task_id, type="TaskStarted", payload=TaskStartedPayload(lease_id="L")
    )


def _emit_plan(
    log: Any, cs: Any, task_id: str, *, skills: list[str],
    msg_body: bytes = b"msg-body", res_body: Optional[bytes] = None,
    dropped_body: Optional[bytes] = None,
    version: str = "three_segment.v2",
) -> None:
    msg_ref = cs.put(msg_body, media_type="application/json")
    dropped_refs = []
    if dropped_body is not None:
        dropped_refs.append(cs.put(dropped_body, media_type="application/json"))
    resources: list[dict[str, Any]] = []
    if res_body is not None:
        res_ref = cs.put(res_body, media_type="text/markdown")
        resources = [{
            "reason": "referenced", "content_ref": res_ref,
            "bytes": res_ref.size, "media_type": "text/markdown",
        }]
    plan = ContextPlan(
        composer_version=version,
        segment_hashes={"stable_prefix": "h1", "semi_stable": "h2", "dynamic_suffix": "h3"},
        selected_skills=list(skills),
        selected_messages=[msg_ref],
        dropped_messages=dropped_refs,
        retrieved_resources=resources,
    )
    plan_ref = cs.put(to_canonical_bytes(plan), media_type="application/json")
    log.emit(
        task_id=task_id, type="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=plan_ref),
    )


def _emit_request(
    log: Any, cs: Any, task_id: str, *, call_id: str, selected: int = 3,
    candidates: int = 5, input_tokens: int = 0,
) -> None:
    req_ref = cs.put(b'{"messages": []}', media_type="application/json")
    log.emit(
        task_id=task_id, type="LLMRequestStarted",
        payload=LLMRequestStartedPayload(
            call_id=call_id, model="gpt-test", request_ref=req_ref,
            input_tokens=input_tokens,
            selection=MessageSelection(
                strategy="tail_window", candidates=candidates, selected=selected,
                dropped=candidates - selected, limit=selected,
            ),
        ),
    )


def _seed_two_steps(db: Path) -> str:
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    _emit_plan(log, cs, "t1", skills=["tidy-up"])
    _emit_request(log, cs, "t1", call_id="c0", selected=3, candidates=5)
    _emit_plan(log, cs, "t1", skills=["tidy-up", "lint"], version="three_segment.v2")
    _emit_request(log, cs, "t1", call_id="c1", selected=4, candidates=9)
    _close(log, cs, disp)
    return "t1"


# ---------------------------------------------------------------------------
# Unit — build_code_context_view
# ---------------------------------------------------------------------------


def test_build_view_latest_vs_all(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _seed_two_steps(db)
    store = SqliteReadOnlyStore(str(db))
    try:
        latest = build_code_context_view(store, store, "t1", all_steps=False)
        allv = build_code_context_view(store, store, "t1", all_steps=True)
    finally:
        store.close()
    assert latest is not None and allv is not None
    agent_l, plans_l, sels_l = latest
    agent_a, plans_a, sels_a = allv
    assert agent_l == "default"
    assert len(plans_l) == 1 and len(sels_l) == 1
    assert len(plans_a) == 2 and len(sels_a) == 2
    assert plans_a[-1].selected_skills == ("tidy-up", "lint")


def test_build_view_non_code_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1", agent="unnamed")
    _close(log, cs, disp)
    store = SqliteReadOnlyStore(str(db))
    try:
        assert build_code_context_view(store, store, "t1") is None
    finally:
        store.close()
