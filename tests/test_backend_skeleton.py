"""T4 acceptance — the new noeta.sdk-eating backend skeleton.

Proves the new ``noeta.agent.backend`` boots, drives a turn through ``noeta.sdk``
only, and serves HTTP — alongside the retained legacy stack.
"""

from __future__ import annotations

import ast
import urllib.request
from pathlib import Path

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.backend import engine_room as engine_room_mod
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider


def _finishing_provider() -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )


def _minimal_room(workspace: Path) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="you finish immediately",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=_finishing_provider(),
        workspace_dir=workspace,
    )


def test_engine_room_drives_a_turn(tmp_path: Path) -> None:
    room = _minimal_room(tmp_path)
    try:
        task_id = room.start(goal="hello")
        events = room.events(task_id)
        messages = room.messages(task_id)
    finally:
        room.shutdown()
    assert task_id
    # A multi-turn conversation turn ran: the scripted assistant reply was
    # recorded (the task then suspends awaiting the next goal — not terminal).
    assert any(e.type == "MessagesAppended" for e in events), [e.type for e in events]
    assert any("done" in str(getattr(m, "text", getattr(m, "answer", ""))) for m in messages), messages


def test_engine_room_official_registry_builds(tmp_path: Path) -> None:
    # The official preset registry (main + subagents) compiles and wires.
    room = EngineRoom.official(
        provider=_finishing_provider(), workspace_dir=tmp_path
    )
    try:
        assert room.main_agent_name == "main"
    finally:
        room.shutdown()


def test_engine_room_imports_only_noeta_sdk() -> None:
    """The backend's engine room must not name any runtime internal — it drives
    agents through the public ``noeta.sdk`` surface only (D5/T4; T8 welds this
    with an import-linter contract)."""
    source = Path(engine_room_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    noeta_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("noeta"):
            noeta_imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            noeta_imports.extend(
                a.name for a in node.names if a.name.startswith("noeta")
            )
    assert noeta_imports, "expected at least the noeta.sdk import"
    for mod in noeta_imports:
        assert mod == "noeta.sdk" or mod.startswith("noeta.sdk."), (
            f"engine_room imports {mod!r}; only noeta.sdk is allowed"
        )


def test_backend_serves_health(tmp_path: Path) -> None:
    room = _minimal_room(tmp_path)
    config = BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path)
    server, url, shutdown = serve_backend(config, engine_room=room)
    try:
        with urllib.request.urlopen(url + "health", timeout=5) as resp:
            assert resp.status == 200
            import json

            body = json.loads(resp.read())
        assert body == {"status": "ok", "backend": "new"}
    finally:
        shutdown()
