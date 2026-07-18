"""Sandbox preview + browser tool full chain (docker-gated): really start an
AIO container and verify the three panels plus the browser wire.

Acceptance:
- after driving one turn (container allocate) the discovery endpoint returns
  ``{token, port, panels}``;
- the three panels' HTTP faces are reachable on the preview origin: the noVNC
  page / the terminal page / the code-server page;
- the websockify WS reverse-proxy full chain: 101 handshake + the first frame
  is VNC's RFB banner;
- noeta ``AioBrowserBackend`` wire check against the local custom image:
  navigate / extract / screenshot really run through (a sentinel for browser
  perception-surface and wire drift);
- after deleting the session the discovery endpoint 404s and the old token
  404s on the preview origin.
"""
from __future__ import annotations

import http.client
import socket
import subprocess

from tests._docker_sandbox import DOCKER_SANDBOX_IMAGE, requires_docker_sandbox
from tests.conftest import create_session, login, read_sse, wait_status


def _responder():
    from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage

    def responder(request):
        done = sum(1 for m in (request.messages or []) if m.role == "tool")
        if done < 1:
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="c0",
                        tool_name="shell_run",
                        arguments={"command": "echo preview-e2e"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            )
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Done.")],
            usage=Usage(uncached=1, output=1),
        )

    return responder


def _get(port: int, path: str, timeout: float = 30.0) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _container_base_url(session_id: str) -> str:
    """Reverse-decode the host-mapped port by container name
    (noeta-sbx-<app session id>)."""
    out = subprocess.run(
        ["docker", "port", f"noeta-sbx-{session_id}", "8080"],
        capture_output=True, text=True, timeout=15,
    )
    assert out.returncode == 0, out.stderr
    line = out.stdout.strip().splitlines()[0]
    return f"http://127.0.0.1:{int(line.rsplit(':', 1)[1])}"


@requires_docker_sandbox
def test_preview_panels_and_browser_wire(make_client, monkeypatch):
    from noeta.testing.fake_llm import FakeLLMProvider

    monkeypatch.setattr(
        "noeta.agent.host.service.build_provider",
        lambda settings: (FakeLLMProvider(responder=_responder()), "mock"),
    )
    client = make_client(SANDBOX_ENABLED="true", SANDBOX_IMAGE=DOCKER_SANDBOX_IMAGE)
    login(client)

    sid = create_session(client)
    resp = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "run it"})
    assert resp.status_code == 202
    wait_status(client, sid, {"idle"}, timeout=120.0)
    read_sse(client, sid, stop_types=("turn_finished",), timeout=120.0)

    # -- discovery endpoint -------------------------------------------------
    resp = client.get(f"/api/v1/sessions/{sid}/preview")
    assert resp.status_code == 200, resp.text
    info = resp.json()
    token, port = info["token"], info["port"]
    assert token and isinstance(port, int)
    assert set(info["panels"]) == {"browser", "terminal", "code"}

    # -- the three panels' HTTP faces (via the preview origin to the container)
    status, body = _get(port, f"/sandbox-preview/{token}/{info['panels']['browser']}")
    assert status == 200, (status, body[:200])
    assert b"<html" in body.lower() or b"novnc" in body.lower()

    status, body = _get(port, f"/sandbox-preview/{token}/{info['panels']['terminal']}")
    assert status == 200, (status, body[:200])

    status, body = _get(port, f"/sandbox-preview/{token}/{info['panels']['code']}")
    assert status == 200, (status, body[:200])

    # -- the websockify WS reverse-proxy full chain: 101 + first-frame RFB banner
    from noeta.agent.host.preview_ws import read_frame

    with socket.create_connection(("127.0.0.1", port), timeout=30) as s:
        s.sendall(
            (
                f"GET /sandbox-preview/{token}/websockify HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "Sec-WebSocket-Protocol: binary\r\n"
                "\r\n"
            ).encode("ascii")
        )
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            assert chunk, "connection closed during the handshake"
            buf.extend(chunk)
        head = buf.decode("latin-1")
        assert head.startswith("HTTP/1.1 101"), head.splitlines()[:1]
        # bytes left over after the handshake may already hold part of the
        # first frame; top up and read the RFB banner from the frame boundary
        rest = bytes(buf[buf.index(b"\r\n\r\n") + 4:])
        if rest:
            # partial frame data already present: look for the banner in the
            # raw bytes directly (simplified handling)
            payload = rest
            while b"RFB " not in payload:
                chunk = s.recv(4096)
                assert chunk, "no VNC banner received"
                payload += chunk
            assert b"RFB " in payload
        else:
            frame = read_frame(s)
            assert frame is not None, "no VNC banner frame received"
            _fin, _op, payload = frame
            assert payload.startswith(b"RFB "), payload[:16]

    # -- the noeta browser tool wire (pinned against the local custom image) --
    from noeta.tools.browser._backend import AioBrowserBackend

    backend = AioBrowserBackend(base_url=_container_base_url(sid), timeout_s=60.0)
    out = backend.navigate("http://127.0.0.1:8080/v1/sandbox")
    assert isinstance(out, str)
    extracted = backend.extract()
    assert isinstance(extracted, str) and extracted.strip()
    png = backend.screenshot()
    assert png[:4] == b"\x89PNG"

    # -- delete the session: discovery 404 + the old token dies ---------------
    resp = client.delete(f"/api/v1/sessions/{sid}")
    assert resp.status_code == 200
    assert client.get(f"/api/v1/sessions/{sid}/preview").status_code == 404
    status, _ = _get(port, f"/sandbox-preview/{token}/terminal")
    assert status == 404
