"""Space memory management API: list / get / put / archive / delete + the
permission model.

Storage is plain directory files (DATA_DIR/memories/<space_id>/); the tests
read/write through the API and then check the materialized files under tmp
directly (archive move / physical delete). The e2e at the end uses the mock
LLM's memory chain (goal contains "remember" → memory_write) to verify that
an agent session persists through the resolver into the same space directory.
"""
from __future__ import annotations

from tests.conftest import (
    create_session,
    login,
    personal_space_id,
    read_sse,
    types,
    wait_status,
)


_TEXT = """---
description: User prefers concise replies
type: user
---

The user asks that all replies stay concise.
"""


def _new_team(client, name="Team A") -> str:
    r = client.post("/api/v1/spaces", json={"name": name, "description": ""})
    assert r.status_code == 201, r.text
    return r.json()["space"]["id"]


def test_memories_crud_roundtrip(client, tmp_path):
    login(client)
    sid = personal_space_id(client)

    # empty pool: returns an empty list even before the directory exists
    r = client.get(f"/api/v1/spaces/{sid}/memories")
    assert r.status_code == 200 and r.json()["memories"] == []

    # write (with frontmatter) → list entries carry description/type
    r = client.put(
        f"/api/v1/spaces/{sid}/memories/user-prefers-concise",
        json={"text": _TEXT},
    )
    assert r.status_code == 200, r.text
    r = client.get(f"/api/v1/spaces/{sid}/memories")
    (entry,) = r.json()["memories"]
    assert entry["name"] == "user-prefers-concise"
    assert entry["description"] == "User prefers concise replies"
    assert entry["type"] == "user"
    assert entry["updated_at"] is not None

    # single read returns the full text (frontmatter included, same semantics
    # as the memory_write tool)
    r = client.get(f"/api/v1/spaces/{sid}/memories/user-prefers-concise")
    assert r.status_code == 200 and r.json()["text"] == _TEXT

    # materialized location = the same directory the resolver uses
    path = tmp_path / "data" / "memories" / sid / "user-prefers-concise.md"
    assert path.is_file()

    # archive: disappears from the list, file moves into archive/
    r = client.post(f"/api/v1/spaces/{sid}/memories/user-prefers-concise/archive")
    assert r.status_code == 200
    assert client.get(f"/api/v1/spaces/{sid}/memories").json()["memories"] == []
    assert not path.is_file()
    assert (path.parent / "archive" / "user-prefers-concise.md").is_file()


def test_memories_validation_and_404(client):
    login(client)
    sid = personal_space_id(client)

    # invalid slug name → 422
    r = client.put(f"/api/v1/spaces/{sid}/memories/Bad Name!", json={"text": "x"})
    assert r.status_code == 422

    # nonexistent memory: get / archive → 404
    assert client.get(f"/api/v1/spaces/{sid}/memories/nope").status_code == 404
    assert client.post(f"/api/v1/spaces/{sid}/memories/nope/archive").status_code == 404


def test_memories_membership_hidden(make_client):
    client = make_client()
    login(client, "alice")
    sid = personal_space_id(client)
    client.put(f"/api/v1/spaces/{sid}/memories/note", json={"text": "secret"})

    # non-member (bob) → 404 hides existence
    other = make_client()
    login(other, "bob")
    assert other.get(f"/api/v1/spaces/{sid}/memories").status_code == 404
    assert other.get(f"/api/v1/spaces/{sid}/memories/note").status_code == 404


def test_memories_delete_owner_only(make_client):
    client = make_client()
    login(client, "alice")
    sid = _new_team(client)
    client.put(f"/api/v1/spaces/{sid}/memories/team-fact", json={"text": "a fact"})
    r = client.post(
        f"/api/v1/spaces/{sid}/members", json={"username": "bob", "role": "member"}
    )
    assert r.status_code == 201, r.text

    # member: can read, edit, and archive, but not physically delete
    other = make_client()
    login(other, "bob")
    assert other.get(f"/api/v1/spaces/{sid}/memories/team-fact").status_code == 200
    r = other.put(f"/api/v1/spaces/{sid}/memories/team-fact", json={"text": "corrected"})
    assert r.status_code == 200
    assert (
        other.delete(f"/api/v1/spaces/{sid}/memories/team-fact").status_code == 403
    )

    # owner: physical delete
    assert client.delete(f"/api/v1/spaces/{sid}/memories/team-fact").status_code == 200
    assert client.get(f"/api/v1/spaces/{sid}/memories/team-fact").status_code == 404


def test_agent_memory_write_lands_in_space_dir(client, tmp_path):
    """e2e: the agent calls memory_write inside a session → the resolver
    persists to this space's directory.

    Evidence chain: SSE emits a memory_op(write) event (translator folding) →
    the file materializes under DATA_DIR/memories/<space_id>/ → the management
    API list shows it (same directory, same pool).
    """
    login(client)
    sid = create_session(client)

    resp = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "Please remember: I prefer concise replies"},
    )
    assert resp.status_code == 202
    wait_status(client, sid, {"idle"})

    events = read_sse(client, sid, stop_types=("turn_finished",), timeout=10)
    mem_ops = [e for e in events if e["event"] == "memory_op"]
    assert mem_ops and mem_ops[0]["data"]["op"] == "write"
    assert mem_ops[0]["data"]["name"] == "user-preference-demo"
    assert "memory_op" in types(events)

    space_id = personal_space_id(client)
    path = tmp_path / "data" / "memories" / space_id / "user-preference-demo.md"
    assert path.is_file(), (
        "memory did not land in the space directory (resolver not effective?)"
    )
    assert "prefer concise replies" in path.read_text(encoding="utf-8")

    r = client.get(f"/api/v1/spaces/{space_id}/memories")
    names = [m["name"] for m in r.json()["memories"]]
    assert "user-preference-demo" in names


def test_consolidation_runs_per_space(make_client, tmp_path):
    """e2e: turn end triggers background consolidation; the curation output
    and the debounce marker land in this space's directory.

    The debounce threshold is set to 0 so the first turn boundary triggers
    immediately; the mock's __consolidation__ branch (goal starts with the
    SDK preamble) writes one consolidated-note. Evidence chain: the curation
    task does not leak into the user session (no extra SSE events) → marker +
    consolidated memory materialize under DATA_DIR/memories/<space_id>/ → the
    quarantine directory has no stray writes.
    """
    import time as _time

    client = make_client(
        MEMORY_CONSOLIDATION="true", MEMORY_CONSOLIDATION_DEBOUNCE_HOURS="0"
    )
    login(client)
    sid = create_session(client)

    resp = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "Please remember: I prefer concise replies"},
    )
    assert resp.status_code == 202
    wait_status(client, sid, {"idle"})

    space_id = personal_space_id(client)
    space_dir = tmp_path / "data" / "memories" / space_id
    marker = space_dir / ".consolidation-state.json"
    note = space_dir / "consolidated-note.md"

    # the background chain (jobs worker dispatch → seed → resident worker
    # drives it) completes asynchronously: poll for the artifacts
    deadline = _time.time() + 15
    while _time.time() < deadline and not (marker.is_file() and note.is_file()):
        _time.sleep(0.1)
    assert marker.is_file(), (
        "debounce marker not written (consolidation not triggered?)"
    )
    assert note.is_file(), (
        "consolidated memory did not land in the space directory "
        "(on_seeded registration broken?)"
    )
    assert "prefers concise replies" in note.read_text(encoding="utf-8")

    # the curation task belongs to no session: the quarantine fallback
    # directory must stay clean
    quarantine = tmp_path / "data" / "memories" / "_quarantine"
    assert not quarantine.exists() or not list(quarantine.glob("*.md"))

    # the user session's event stream contains none of the curation agent's
    # events (the only memory_op is the in-session write)
    events = read_sse(client, sid, stop_types=("turn_finished",), timeout=10)
    mem_ops = [e for e in events if e["event"] == "memory_op"]
    assert [m["data"]["name"] for m in mem_ops] == ["user-preference-demo"]
