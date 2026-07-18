"""Per-session container full chain (docker-gated): really start an AIO
container, let the agent work inside it with the noeta standard tools, and get
the artifacts back to the frontend file panel through the host workspace.

Acceptance (matching the spec's acceptance criteria):
- the standard shell_run executes inside the session container; artifacts land
  in the session workspace and are visible via the frontend /files;
- a failing command → tool_result{success:false}, no error event, the turn
  wraps up normally (replaces the old test_tool_crash guarantee that a tool
  failure is synthesized and the turn continues);
- after convergence the tool surface is the standard tools; the old sandbox_*
  trio does not appear;
- two sessions each have an independent workspace (independent container
  isolation);
- deleting the session → the session disappears (container release + directory
  cleanup).
"""
from __future__ import annotations

from tests._docker_sandbox import DOCKER_SANDBOX_IMAGE, requires_docker_sandbox
from tests.conftest import create_session, login, personal_space_id, read_sse, wait_status


def _responder():
    from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage

    steps = [
        ("shell_run", {"command": "echo 'hello from sandbox' > result.txt"}),
        # A standard-tool failure inside the container (reading a missing
        # file) → tool_result success=false.
        # Note: shell_run still reports success=true on a non-zero exit (the
        # command ran; the error is in the output); to trigger a tool-level
        # failure, read a missing file.
        ("read", {"path": "does-not-exist-in-workspace.txt"}),
    ]

    def responder(request):
        done = sum(1 for m in (request.messages or []) if m.role == "tool")
        if done < len(steps):
            name, args = steps[done]
            return LLMResponse(
                stop_reason="tool_use",
                content=[ToolUseBlock(call_id=f"c{done}", tool_name=name, arguments=args)],
                usage=Usage(uncached=1, output=1),
            )
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Done.")],
            usage=Usage(uncached=1, output=1),
        )

    return responder


def _drive(client, sid: str, timeout: float = 120.0) -> list[dict]:
    resp = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "run it"})
    assert resp.status_code == 202
    wait_status(client, sid, {"idle"}, timeout=timeout)
    return read_sse(client, sid, stop_types=("turn_finished",), timeout=timeout)


@requires_docker_sandbox
def test_sandbox_e2e_turn(make_client, monkeypatch):
    from noeta.testing.fake_llm import FakeLLMProvider

    monkeypatch.setattr(
        "noeta.agent.host.service.build_provider",
        lambda settings: (FakeLLMProvider(responder=_responder()), "mock"),
    )
    client = make_client(SANDBOX_ENABLED="true", SANDBOX_IMAGE=DOCKER_SANDBOX_IMAGE)
    login(client)

    sid = create_session(client)
    events = _drive(client, sid)

    calls = {e["data"]["call_id"]: e["data"] for e in events if e["event"] == "tool_call"}
    results = {e["data"]["call_id"]: e["data"] for e in events if e["event"] == "tool_result"}
    # the standard tools really run inside the container; the old trio does
    # not appear
    assert any(d["tool_name"] == "shell_run" for d in calls.values()), list(calls.values())
    assert not any(d["tool_name"].startswith("sandbox_") for d in calls.values())
    # tool-level failure (read on a missing file) → tool_result success=false,
    # but the turn does not error and wraps up normally
    assert any(r["success"] is False for r in results.values()), list(results.values())
    assert not any(e["event"] == "error" for e in events)
    finish = [e for e in events if e["event"] == "turn_finished"]
    assert finish and finish[-1]["data"]["status"] in ("awaiting_input", "completed")

    # the file panel reads the host workspace: artifact visible, content right
    files = client.get(f"/api/v1/sessions/{sid}/files").json()["files"]
    assert any(f["path"] == "result.txt" for f in files), files
    body = client.get(
        f"/api/v1/sessions/{sid}/files/content", params={"path": "result.txt"}
    ).json()
    assert "hello from sandbox" in body["content"]

    # second session: independent workspace (independent container) — empty
    # before driving, its own artifact after
    sid2 = create_session(client)
    assert client.get(f"/api/v1/sessions/{sid2}/files").json()["files"] == []
    _drive(client, sid2)
    assert "result.txt" in [
        f["path"] for f in client.get(f"/api/v1/sessions/{sid2}/files").json()["files"]
    ]

    # delete the session → container release + directory cleanup (the session
    # disappears)
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 200
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
