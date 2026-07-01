"""T6 acceptance — peripheral resource services (content / files / file), separate from the task
protocol.

Covers the data-plane core: deref a ContentRef by hash, the sandboxed workspace
file tree, single-file read, and that a sandbox escape is refused. The preview
gateway + MCP management are the remaining T6 services (engine wiring pending).
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import LLMResponse, TextBlock, Usage


def _room(workspace: Path) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="finish",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="ok")],
                    usage=Usage(uncached=1, output=1),
                )
            ]
        ),
        workspace_dir=workspace,
    )


def _get(host: str, port: int, path: str) -> tuple[int, bytes, str]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    ctype = resp.getheader("Content-Type", "")
    conn.close()
    return resp.status, body, ctype


def test_content_endpoint_derefs_by_hash(tmp_path: Path) -> None:
    room = _room(tmp_path)
    # Put a PNG blob straight into the content store the engine room serves.
    png = b"\x89PNG\r\n\x1a\n" + b"payload-bytes"
    ref = room._client._host.content_store.put(png, media_type="image/png")

    server, url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        status, body, ctype = _get(host, port, f"/content/{ref.hash}")
        assert status == 200
        assert body == png
        assert ctype == "image/png"  # sniffed from magic bytes

        missing, _, _ = _get(host, port, "/content/deadbeef")
        assert missing == 404
    finally:
        shutdown()


def test_files_tree_and_single_file_and_sandbox(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: x\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

    server, url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),
    )
    host, port = server.server_address[:2]
    try:
        # File tree: includes src/ + README.md, excludes .git (skip dir).
        status, body, _ = _get(host, port, "/files?task=t1")
        assert status == 200
        tree = json.loads(body)["tree"]
        names = {e["name"] for e in tree}
        assert "src" in names and "README.md" in names
        assert ".git" not in names

        # Single file read (sandboxed).
        status, body, _ = _get(host, port, "/file?task=t1&path=src/main.py")
        assert status == 200
        payload = json.loads(body)
        assert payload["content"] == "print('hi')\n"
        assert payload["truncated"] is False

        # Sandbox escape is refused.
        status, _, _ = _get(host, port, "/file?task=t1&path=../../../etc/passwd")
        assert status == 404
    finally:
        shutdown()


def test_resource_routes_decoupled_from_task_protocol(tmp_path: Path) -> None:
    # The resource services live on their own routes; the task protocol's
    # /health + /stream are unaffected by them (decoupling check).
    server, url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),
    )
    host, port = server.server_address[:2]
    try:
        assert _get(host, port, "/health")[0] == 200
        assert _get(host, port, "/files")[0] == 200
    finally:
        shutdown()
