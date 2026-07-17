"""Test fixtures: an app with an isolated data directory + the mock provider,
running on a real uvicorn.

Why not starlette's TestClient: it does not truly stream response bodies, the
SSE endpoint is an infinite stream, and consuming it synchronously would block
forever. Here we start a real uvicorn in a thread (random port) and consume it
with httpx real streaming — behavior matches production.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

import httpx
import pytest
import uvicorn

from noeta.agent.config import Settings
from noeta.agent.main import create_app


class LiveServer:
    """In-thread uvicorn: random port, lifespan fully executed."""

    def __init__(self, app) -> None:
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
        )
        self.server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self._thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn startup timed out")
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Factory: customize env, then start (uvicorn + httpx.Client)."""
    servers: list[LiveServer] = []
    clients: list[httpx.Client] = []

    def _make(**env: str) -> httpx.Client:
        defaults = {
            "DATA_DIR": str(tmp_path / "data"),
            "LLM_PROVIDER": "mock",
            "DEV_LOGIN_ENABLED": "true",
            "SESSION_SECRET": "test-secret",
            # Isolate from the developer's project-root .env: a locally
            # enabled sandbox must not affect the test baseline.
            "SANDBOX_ENABLED": "false",
            # Background memory curation off by default: the first turn
            # boundary is immediately due (no marker), so leaving it on would
            # make every case spawn a background curation task and add noise
            # (the e2e case turns it on explicitly).
            "MEMORY_CONSOLIDATION": "false",
            # The global tool-surface switches (production default off in
            # config.py) are on in the test baseline: the full pre-change
            # roster, keeping e2e coverage of the memory / delegation /
            # collab mechanisms. These tools are lazy once registered — the
            # mock produces no background noise unless explicitly triggered
            # ("remember" / "parallel search" / board calls) — so this does
            # not conflict with the "quiet baseline".
            "MEMORY_TOOLS_ENABLED": "true",
            "COLLAB_TOOLS_ENABLED": "true",
            "SUBAGENT_ENABLED": "true",
        }
        defaults.update(env)
        for key, value in defaults.items():
            monkeypatch.setenv(key, value)
        server = LiveServer(create_app(Settings()))
        base_url = server.start()
        servers.append(server)
        client = httpx.Client(base_url=base_url, timeout=10.0)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.close()
    for server in servers:
        server.stop()


@pytest.fixture
def client(make_client) -> httpx.Client:
    return make_client()


def login(client: httpx.Client, username: str = "alice") -> None:
    resp = client.post("/api/v1/auth/dev-login", json={"username": username})
    assert resp.status_code == 200, resp.text


def personal_space_id(client: httpx.Client) -> str:
    """The logged-in user's personal space id (auto-created on login)."""
    resp = client.get("/api/v1/spaces")
    assert resp.status_code == 200, resp.text
    for space in resp.json()["spaces"]:
        if space["is_personal"]:
            return space["id"]
    raise AssertionError("personal space not found")


def create_session(client: httpx.Client, space_id: Optional[str] = None) -> str:
    if space_id is None:
        space_id = personal_space_id(client)
    resp = client.post("/api/v1/sessions", json={"space_id": space_id})
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]["id"]


def wait_status(client: httpx.Client, sid: str, want: set[str], timeout: float = 15.0) -> str:
    deadline = time.time() + timeout
    status = "?"
    while time.time() < deadline:
        status = client.get(f"/api/v1/sessions/{sid}").json()["session"]["status"]
        if status in want:
            return status
        time.sleep(0.05)
    raise AssertionError(f"session status stuck at {status!r}, wanted {want}")


def read_sse(
    client: httpx.Client,
    sid: str,
    since_seq: Optional[int] = None,
    stop_types: tuple[str, ...] = ("turn_finished",),
    timeout: float = 20.0,
    task_id: Optional[str] = None,
) -> list[dict]:
    """Consume SSE until one of stop_types appears (or return what has been
    received on timeout)."""
    url = f"/api/v1/sessions/{sid}/events"
    query = []
    if since_seq is not None:
        query.append(f"since_seq={since_seq}")
    if task_id:
        query.append(f"task_id={task_id}")
    if query:
        url += "?" + "&".join(query)
    events: list[dict] = []
    deadline = time.time() + timeout
    try:
        with client.stream(
            "GET", url, timeout=httpx.Timeout(5.0, read=timeout)
        ) as resp:
            assert resp.status_code == 200
            cur: dict = {}
            for line in resp.iter_lines():
                if time.time() > deadline:
                    break
                if line == "":
                    if cur.get("event"):
                        # Synthetic frames (e.g. replay_done) have no id line;
                        # fill seq with None
                        cur.setdefault("seq", None)
                        events.append(cur)
                        if cur["event"] in stop_types:
                            return events
                    cur = {}
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(": ")
                if key == "id":
                    cur["seq"] = int(value)
                elif key == "event":
                    cur["event"] = value
                elif key == "data":
                    cur["data"] = json.loads(value)
    except httpx.ReadTimeout:
        pass
    return events


def types(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]
