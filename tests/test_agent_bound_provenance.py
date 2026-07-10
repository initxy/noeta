"""Durable Agent provenance via ``AgentBound``.

Locks the genesis sequence ``TaskCreated → AgentBound → …`` on the product
paths, the create_task consistency guards, the SQLite decode round-trip, and the
legacy/``unnamed`` byte-safety (no AgentBound for unnamed tasks). The verify-era
``agent_fingerprint`` field was retired with the verify/replay test
infrastructure — ``AgentBound`` now carries only ``agent_name``.
"""

from __future__ import annotations

from pathlib import Path

from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.client import SdkHost
from noeta.core.engine import Engine
from noeta.core.snapshot import rehydrate_task
from noeta.protocols.canonical import restore_dataclass
from noeta.protocols.events import (
    AgentBoundPayload,
    TaskCreatedPayload,
    TaskHostBoundPayload,
)
from noeta.protocols.task import GovernanceState
from noeta.testing.composer import trivial_three_segment
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.sqlite.eventlog import SqliteEventLog, _restore_payload
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    official_registry as official_agent_registry,
    runner_main_spec,
)


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


# --- Engine.create_task: atomic emit + guards ---


def _engine() -> tuple[Engine, InMemoryEventLog, InMemoryContentStore]:
    el = InMemoryEventLog()
    cs = InMemoryContentStore()
    return (
        Engine(event_log=el, content_store=cs, composer=trivial_three_segment(cs)),
        el,
        cs,
    )


def test_named_task_emits_taskcreated_then_agentbound() -> None:
    eng, el, cs = _engine()
    task = eng.create_task(
        goal="g", policy_name="react", agent_name="bug-fixer",
    )
    seq = [e.type for e in el.read(task.task_id)]
    assert seq == ["TaskCreated", "AgentBound"]
    bound = next(e for e in el.read(task.task_id) if e.type == "AgentBound")
    assert bound.payload.agent_name == "bug-fixer"


def test_unnamed_has_no_agentbound() -> None:
    eng, el, cs = _engine()
    task = eng.create_task(goal="g", policy_name="react", agent_name="unnamed")
    assert [e.type for e in el.read(task.task_id)] == ["TaskCreated"]


# --- SQLite decode round-trip ---


def test_agentbound_survives_sqlite_close_reopen(tmp_path: Path) -> None:
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    try:
        log.emit(task_id="t1", type="TaskCreated",
                 payload=TaskCreatedPayload(goal="g", policy_name="p", agent_name="bug-fixer"))
        log.emit(task_id="t1", type="AgentBound",
                 payload=AgentBoundPayload(agent_name="bug-fixer"))
    finally:
        log.close()
    reopened = SqliteEventLog(db)
    try:
        bound = next(e for e in reopened.read("t1") if e.type == "AgentBound")
        assert isinstance(bound.payload, AgentBoundPayload)
        assert bound.payload.agent_name == "bug-fixer"
    finally:
        reopened.close()


# --- R1: old recordings carrying retired verify-era fingerprint keys ---
#
# The verify/replay removal dropped the ``*_fingerprint`` fields from
# AgentBound / TaskHostBound / GovernanceState. A task persisted *before* that
# removal still carries those keys; the lenient ``restore_dataclass`` layer must
# drop them so the old recording still decodes / folds / resumes instead of
# crashing on an unexpected keyword. This is the only safety net for that
# backward compatibility, so it is pinned here.


def test_restore_dataclass_drops_retired_fingerprint_keys() -> None:
    bound = restore_dataclass(
        AgentBoundPayload,
        {"agent_name": "bug-fixer", "agent_fingerprint": "deadbeef"},
    )
    assert bound == AgentBoundPayload(agent_name="bug-fixer")

    host = restore_dataclass(
        TaskHostBoundPayload,
        {
            "host_id": "h1",
            "workspace_dir": "/ws",
            "host_config_fingerprint": "aaa",
            "registry_fingerprint": "bbb",
        },
    )
    assert host == TaskHostBoundPayload(host_id="h1", workspace_dir="/ws")

    gov = restore_dataclass(
        GovernanceState,
        {
            "host_id": "h1",
            "workspace": "/ws",
            "agent_fingerprint": "aaa",
            "host_config_fingerprint": "bbb",
            "registry_fingerprint": "ccc",
        },
    )
    # load-bearing fields survive; the retired keys are silently dropped
    # (construction would have raised TypeError otherwise).
    assert gov.host_id == "h1"
    assert gov.workspace == "/ws"


def test_eventlog_decode_tolerates_old_fingerprint_payloads() -> None:
    # The real SQLite decode dispatch (``_restore_payload``) routes AgentBound /
    # TaskHostBound through the tolerant restorer — proves the wiring, not just
    # the helper in isolation.
    bound = _restore_payload(
        "AgentBound", {"agent_name": "bug-fixer", "agent_fingerprint": "old"}
    )
    assert bound == AgentBoundPayload(agent_name="bug-fixer")
    host = _restore_payload(
        "TaskHostBound",
        {"host_id": "h1", "registry_fingerprint": "old",
         "host_config_fingerprint": "old"},
    )
    assert host == TaskHostBoundPayload(host_id="h1")


def test_snapshot_rehydrate_tolerates_old_governance_fingerprints() -> None:
    # The real snapshot decode (``rehydrate_task``) routes governance through
    # the tolerant restorer — an old suspended task stays resumable.
    task = rehydrate_task({
        "task_id": "t1", "status": "suspended", "parent_task_id": None,
        "runtime": {"messages": []}, "state": {}, "context": {},
        "governance": {
            "host_id": "h1", "workspace": "/ws",
            "agent_fingerprint": "a", "host_config_fingerprint": "b",
            "registry_fingerprint": "c",
        },
        "wake_on": None,
    })
    assert task.governance.host_id == "h1"
    assert task.governance.workspace == "/ws"


# --- Product path: InteractionDriver.start (locked sequence) ---


def _driver_host(ws: Path, responses: list[LLMResponse]) -> tuple[SdkHost, InMemoryEventLog]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    host = SdkHost(
        event_log=event_log,
        content_store=InMemoryContentStore(),
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        workspace_dir=ws,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,

        registry=official_agent_registry(),
        aliases={"default": "main"},
        require_approval_tools=())
    return host, event_log


def test_driver_start_locks_taskcreated_agentbound_modelbound_order(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, event_log = _driver_host(ws, [_end_turn("hi")])
    outcome = InteractionDriver(host).start(goal="hello", agent="main")

    seq = [e.type for e in event_log.read(outcome.task_id)]
    # TaskCreated → AgentBound → ModelBound → … (locked prefix).
    assert seq[:3] == ["TaskCreated", "AgentBound", "ModelBound"]

    bound = next(e for e in event_log.read(outcome.task_id) if e.type == "AgentBound")
    # Note: agent="main" passed in; the task records the canonical name "main".
    assert bound.payload.agent_name == "main"


# --- Product path: SdkHost + InteractionDriver.start (the shipping SDK assembly) ---


def test_code_session_runner_emits_agentbound(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=[_end_turn("ok")]),
        model="gpt-test",
        multi_turn=False,
    )
    out = make_driver(host).start(goal="do", agent="main")
    events = host.event_log.read(out.task_id)
    seq = [e.type for e in events]
    assert seq[:2] == ["TaskCreated", "AgentBound"]
    bound = next(e for e in events if e.type == "AgentBound")
    assert bound.payload.agent_name == "main"
