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


def test_handler_error_after_response_started_does_not_double_send() -> None:
    """When a handler raises AFTER a response is already committed (e.g. an SSE
    stream faults mid-body), ``_handle_handler_error`` must NOT ``send_json`` a
    second status line into the same socket. It logs and bails instead."""
    from noeta.agent.backend.app import BackendHandler

    handler = BackendHandler.__new__(BackendHandler)
    handler._response_started = True
    sent: list[Any] = []
    handler.send_json = lambda *a, **k: sent.append((a, k))  # type: ignore[method-assign]

    handler._handle_handler_error(RuntimeError("mid-stream boom"))

    assert sent == []  # no second response written


def test_handler_error_before_response_maps_already_terminal_to_409() -> None:
    """Before any bytes are sent, a ``TaskAlreadyTerminalError`` — the typed,
    coded replacement for the old bare ``RuntimeError('...already terminal')``
    — maps to 409 via its stable ``code``, matched structurally (not by message
    substring). The response echoes the ``code`` for the client."""
    from noeta.agent.backend.app import BackendHandler
    from noeta.sdk import TaskAlreadyTerminalError

    handler = BackendHandler.__new__(BackendHandler)
    handler._response_started = False
    sent: list[tuple[Any, int]] = []
    handler.send_json = lambda payload, status=200: sent.append((payload, status))  # type: ignore[method-assign]

    handler._handle_handler_error(
        TaskAlreadyTerminalError(task_id="t1", verb="cancel")
    )

    assert sent and sent[-1][1] == 409
    assert sent[-1][0]["code"] == "task_already_terminal"


def test_handler_error_uncoded_exception_maps_to_opaque_500() -> None:
    """An exception WITHOUT a stable ``code`` (any bare error, incl. a plain
    ``RuntimeError`` whose message happens to say 'already terminal') is now an
    unexpected 500 with an opaque body — the fragile message-substring branch
    is gone, so only a typed ``CodedError`` can map to a 4xx."""
    from noeta.agent.backend.app import BackendHandler

    handler = BackendHandler.__new__(BackendHandler)
    handler._response_started = False
    sent: list[tuple[Any, int]] = []
    handler.send_json = lambda payload, status=200: sent.append((payload, status))  # type: ignore[method-assign]

    handler._handle_handler_error(RuntimeError("task t1 is already terminal"))

    assert sent and sent[-1][1] == 500
    assert sent[-1][0] == {"error": "internal error"}  # no internals leaked


def test_dispatch_error_returns_status_not_dropped_connection(tmp_path: Path) -> None:
    """A handler that raises a typed engine error (here: approving a task that
    is NOT awaiting approval → NotResumableError) must return an HTTP status,
    not drop the socket. ``BaseHTTPRequestHandler`` would otherwise close the
    connection on an unhandled raise."""
    import json
    import urllib.error
    import urllib.request

    room = _minimal_room(tmp_path)
    config = BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path)
    server, url, shutdown = serve_backend(config, engine_room=room)
    try:
        task_id = room.start(goal="hello")  # suspends at next-goal, not approval
        req = urllib.request.Request(
            url + f"tasks/{task_id}/approve",
            data=json.dumps({"call_id": "nope"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        raised: urllib.error.HTTPError | None = None
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            raised = exc
        assert raised is not None, "expected an HTTP error status, not success"
        assert raised.code in (400, 409), raised.code
    finally:
        shutdown()
        room.shutdown()


def test_files_service_resolves_per_session_workspace(tmp_path: Path) -> None:
    """``/files`` / ``/file`` serve the tree of the requested session's
    workspace, not the host-fixed default."""
    default_ws = tmp_path / "default"
    default_ws.mkdir()
    other_ws = tmp_path / "project-x"
    other_ws.mkdir()
    room = _minimal_room(default_ws)
    try:
        task_id = room.start(goal="hi", workspace_dir=str(other_ws))
        assert room.workspace_dir_for(task_id) == other_ws
        # Unknown task / no task both fall back to the host-fixed default.
        assert room.workspace_dir_for("unknown-task") == default_ws
        assert room.workspace_dir_for(None) == default_ws
    finally:
        room.shutdown()


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
