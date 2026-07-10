"""Memory v2 phase 3 — the noeta-agent backend trigger + config (T7).

The two session-stop seams (``docs/adr/memory-consolidation.md``) live on
``EngineRoom``: the explicit ``close`` verb, and the turn boundary observed as
the trailing next-goal ``TaskSuspended`` on the post-commit envelope tap. Both
funnel into one debounced guard that fires ``noeta.sdk.run_consolidation`` on
a fire-and-forget daemon thread — off by default on the room, on by default in
the served product (``BackendConfig.memory_consolidation``). Reserved
``__``-prefixed agent names stay resolvable in the registry but invisible to
``/capabilities`` (``agent_names``).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

import noeta.execution.memory as execution_memory
from noeta.agent.backend import BackendConfig, EngineRoom
from noeta.client.consolidation import (
    CONSOLIDATION_AGENT_NAME,
    CONSOLIDATION_MARKER_FILENAME,
    write_consolidation_marker,
)
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _options() -> Options:
    return Options(
        system_prompt="you finish immediately",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )


def _room(
    workspace: Path,
    responses,
    *,
    memory_consolidation: bool = False,
    debounce_hours: float = 24.0,
) -> EngineRoom:
    return EngineRoom(
        _options(),
        provider=FakeLLMProvider(responses=list(responses)),
        workspace_dir=workspace,
        background_drive=True,
        memory_consolidation=memory_consolidation,
        memory_consolidation_debounce_hours=debounce_hours,
    )


def _consolidation_task(room: EngineRoom) -> Optional[str]:
    for summary in room.task_streams():
        envs = room.events(summary.task_id)
        if (
            envs
            and envs[0].type == "TaskCreated"
            and envs[0].payload.agent_name == CONSOLIDATION_AGENT_NAME
        ):
            return summary.task_id
    return None


def _wait_for_consolidation_task(
    room: EngineRoom, timeout: float = 10.0
) -> Optional[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task_id = _consolidation_task(room)
        if task_id is not None:
            return task_id
        time.sleep(0.05)
    return None


@pytest.fixture
def memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the SDK global memory default to a hermetic tmp dir.

    The room resolves its marker/store home through ``Client.memory_root`` →
    ``SdkHost.memory_root``, which reads the module attribute late — exactly
    so tests can pin it without touching ``~/.noeta/memories``.
    """
    root = tmp_path / "memories"
    monkeypatch.setattr(execution_memory, "DEFAULT_GLOBAL_MEMORY_DIR", root)
    return root


# ---------------------------------------------------------------------------
# 1. BackendConfig — the two knobs
# ---------------------------------------------------------------------------


def test_config_defaults_on_with_24h_debounce() -> None:
    config = BackendConfig.from_env({})
    assert config.memory_consolidation is True
    assert config.memory_consolidation_debounce_hours == 24.0


def test_config_env_knobs(tmp_path: Path) -> None:
    config = BackendConfig.from_env(
        {
            "NOETA_AGENT_MEMORY_CONSOLIDATION": "0",
            "NOETA_AGENT_MEMORY_CONSOLIDATION_DEBOUNCE_HOURS": "6.5",
        }
    )
    assert config.memory_consolidation is False
    assert config.memory_consolidation_debounce_hours == 6.5
    # Config-file keys carry the same values; env wins over the file.
    cfg = tmp_path / "noeta.config.json"
    cfg.write_text(
        json.dumps(
            {
                "memory_consolidation": False,
                "memory_consolidation_debounce_hours": 48,
            }
        )
    )
    from_file = BackendConfig.from_env({"NOETA_AGENT_CONFIG": str(cfg)})
    assert from_file.memory_consolidation is False
    assert from_file.memory_consolidation_debounce_hours == 48.0
    env_wins = BackendConfig.from_env(
        {
            "NOETA_AGENT_CONFIG": str(cfg),
            "NOETA_AGENT_MEMORY_CONSOLIDATION": "true",
            "NOETA_AGENT_MEMORY_CONSOLIDATION_DEBOUNCE_HOURS": "12",
        }
    )
    assert env_wins.memory_consolidation is True
    assert env_wins.memory_consolidation_debounce_hours == 12.0


def test_config_rejects_bad_debounce_values() -> None:
    with pytest.raises(ValueError):
        BackendConfig.from_env(
            {"NOETA_AGENT_MEMORY_CONSOLIDATION_DEBOUNCE_HOURS": "soon"}
        )
    with pytest.raises(ValueError):
        BackendConfig.from_env(
            {"NOETA_AGENT_MEMORY_CONSOLIDATION_DEBOUNCE_HOURS": "-1"}
        )


# ---------------------------------------------------------------------------
# 2. Registry visibility — resolvable, never advertised
# ---------------------------------------------------------------------------


def test_agent_names_hides_reserved_names(tmp_path: Path) -> None:
    room = EngineRoom.official(
        provider=FakeLLMProvider(responses=[]),
        workspace_dir=tmp_path,
        memory_consolidation=True,
    )
    try:
        # Registered (resolvable for the trigger's seed_start) …
        assert CONSOLIDATION_AGENT_NAME in room._client.registry.names()  # noqa: SLF001
        # … but never advertised on /capabilities' agents dropdown.
        names = room.agent_names()
        assert CONSOLIDATION_AGENT_NAME not in names
        assert not [n for n in names if n.startswith("__")]
        assert "main" in names
    finally:
        room.shutdown()


def test_official_room_without_the_feature_registers_nothing(tmp_path: Path) -> None:
    room = EngineRoom.official(
        provider=FakeLLMProvider(responses=[]), workspace_dir=tmp_path
    )
    try:
        assert CONSOLIDATION_AGENT_NAME not in room._client.registry.names()  # noqa: SLF001
    finally:
        room.shutdown()


# ---------------------------------------------------------------------------
# 3. Session-stop seams
# ---------------------------------------------------------------------------


def test_turn_boundary_triggers_consolidation_run(
    tmp_path: Path, memory_root: Path
) -> None:
    """First trailing next-goal suspend with no marker ⇒ due ⇒ the curation
    root task is enqueued, driven by the resident pool, and the marker lands
    in the memory root."""
    room = _room(
        tmp_path,
        [_end("turn reply"), _end("curation summary")],
        memory_consolidation=True,
    )
    try:
        room.start(goal="remember that we deploy on fridays")
        assert room.join_drives(timeout=10.0)
        task_id = _wait_for_consolidation_task(room)
        assert task_id is not None, "turn boundary must enqueue a curation run"
        assert room.join_drives(timeout=10.0)
        genesis = room.events(task_id)[0]
        assert genesis.payload.parent_task_id is None
        assert "remember that we deploy on fridays" in genesis.payload.goal
        # The run was actually driven (its trailing suspend committed) — and
        # its OWN boundary did not re-trigger (enqueue-time marker):
        types = [e.type for e in room.events(task_id)]
        assert "TaskSuspended" in types
        time.sleep(0.3)
        assert (
            sum(
                1
                for s in room.task_streams()
                if room.events(s.task_id)[0].payload.agent_name
                == CONSOLIDATION_AGENT_NAME
            )
            == 1
        )
        assert (memory_root / CONSOLIDATION_MARKER_FILENAME).is_file()
    finally:
        room.shutdown()


def test_close_seam_triggers_when_due(tmp_path: Path, memory_root: Path) -> None:
    """A fresh marker debounces the turn boundary; once the marker is stale,
    the explicit close cascade fires the run — the close ack never waits."""
    write_consolidation_marker(memory_root, now=datetime.now(timezone.utc))
    room = _room(
        tmp_path,
        [_end("turn reply"), _end("curation summary")],
        memory_consolidation=True,
    )
    try:
        task_id = room.start(goal="talk about the release")
        assert room.join_drives(timeout=10.0)
        time.sleep(0.3)  # the boundary tap ran and must have debounced
        assert _consolidation_task(room) is None
        # Age the marker past the threshold, then close.
        write_consolidation_marker(
            memory_root, now=datetime.now(timezone.utc) - timedelta(hours=48)
        )
        room.close(task_id)
        curation_id = _wait_for_consolidation_task(room)
        assert curation_id is not None, "close must fire the consolidation seam"
        assert room.join_drives(timeout=10.0)
        assert "talk about the release" in room.events(curation_id)[0].payload.goal
    finally:
        room.shutdown()


def test_session_list_hides_consolidation_runs(
    tmp_path: Path, memory_root: Path
) -> None:
    """A curation run is a root task on the same log, but it is host
    infrastructure — ``GET /tasks``' projection lists only user sessions."""
    from noeta.agent.backend.read_views import (
        _genesis_agent_name,
        _genesis_parent_task_id,
    )

    room = _room(
        tmp_path,
        [_end("turn reply"), _end("curation summary")],
        memory_consolidation=True,
    )
    try:
        task_id = room.start(goal="a real user session")
        assert room.join_drives(timeout=10.0)
        assert _wait_for_consolidation_task(room) is not None
        assert room.join_drives(timeout=10.0)
        # Reproduce the _handle_list_tasks row filter over the room's streams.
        rows = []
        for summary in room.task_streams():
            envelopes = room.events(summary.task_id)
            if _genesis_parent_task_id(envelopes):
                continue
            if _genesis_agent_name(envelopes).startswith("__"):
                continue
            rows.append(summary.task_id)
        assert rows == [task_id]
    finally:
        room.shutdown()


def test_config_off_close_is_a_noop(tmp_path: Path, memory_root: Path) -> None:
    room = _room(tmp_path, [_end("turn reply")], memory_consolidation=False)
    try:
        task_id = room.start(goal="hello")
        assert room.join_drives(timeout=10.0)
        room.close(task_id)
        time.sleep(0.3)
        assert _consolidation_task(room) is None
        assert len(room.task_streams()) == 1
        assert not (memory_root / CONSOLIDATION_MARKER_FILENAME).exists()
        # Off ⇒ the reserved agent is not even registered on this room.
        assert CONSOLIDATION_AGENT_NAME not in room._client.registry.names()  # noqa: SLF001
    finally:
        room.shutdown()
