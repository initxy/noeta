"""Spaces and members: personal space auto-creation, team space CRUD, member
permissions, session-to-space scoping."""
from __future__ import annotations

from tests.conftest import login, personal_space_id


def _new_team(client, name="Team A", description="") -> str:
    resp = client.post(
        "/api/v1/spaces", json={"name": name, "description": description}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["space"]["id"]


def test_personal_space_auto_created(client):
    login(client, "alice")
    spaces = client.get("/api/v1/spaces").json()["spaces"]
    personal = [s for s in spaces if s["is_personal"]]
    assert len(personal) == 1
    p = personal[0]
    assert p["owner"] == "alice"
    assert p["name"] == "My Space"
    assert p["my_role"] == "owner"
    assert p["member_count"] == 1
    # The personal space always sorts first
    assert spaces[0]["is_personal"] is True


def test_create_team_space_owner(client):
    login(client, "alice")
    resp = client.post("/api/v1/spaces", json={"name": "Team A", "description": "d"})
    assert resp.status_code == 201
    space = resp.json()["space"]
    assert space["is_personal"] is False
    assert space["owner"] == "alice"
    assert space["my_role"] == "owner"
    assert space["member_count"] == 1
    ids = [s["id"] for s in client.get("/api/v1/spaces").json()["spaces"]]
    assert space["id"] in ids


def test_add_member_visibility_and_nonmember_404(client):
    login(client, "alice")
    sid = _new_team(client)
    r = client.post(f"/api/v1/spaces/{sid}/members", json={"username": "bob"})
    assert r.status_code == 201
    assert {m["username"] for m in r.json()["members"]} == {"alice", "bob"}

    # bob sees the team space in his own space list
    login(client, "bob")
    ids = [s["id"] for s in client.get("/api/v1/spaces").json()["spaces"]]
    assert sid in ids
    detail = client.get(f"/api/v1/spaces/{sid}").json()["space"]
    assert detail["my_role"] == "member"
    assert {m["username"] for m in detail["members"]} == {"alice", "bob"}

    # Non-member carol -> 404 (hiding existence)
    login(client, "carol")
    assert client.get(f"/api/v1/spaces/{sid}").status_code == 404


def test_non_owner_forbidden(client):
    login(client, "alice")
    sid = _new_team(client)
    client.post(f"/api/v1/spaces/{sid}/members", json={"username": "bob"})

    login(client, "bob")  # member but not owner
    assert client.patch(f"/api/v1/spaces/{sid}", json={"name": "x"}).status_code == 403
    assert (
        client.post(
            f"/api/v1/spaces/{sid}/members", json={"username": "carol"}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/spaces/{sid}/members/alice").status_code == 403
    )
    assert client.delete(f"/api/v1/spaces/{sid}").status_code == 403


def test_owner_can_update_and_manage_members(client):
    login(client, "alice")
    sid = _new_team(client)
    # Update the info
    r = client.patch(f"/api/v1/spaces/{sid}", json={"name": "New name", "description": "x"})
    assert r.status_code == 200
    assert r.json()["space"]["name"] == "New name"
    # Add a member and promote to owner
    client.post(f"/api/v1/spaces/{sid}/members", json={"username": "bob"})
    r = client.patch(f"/api/v1/spaces/{sid}/members/bob", json={"role": "owner"})
    assert r.status_code == 200
    roles = {m["username"]: m["role"] for m in r.json()["members"]}
    assert roles["bob"] == "owner"
    # With two owners, one of them can be removed
    assert client.delete(f"/api/v1/spaces/{sid}/members/alice").status_code == 200


def test_last_owner_protected(client):
    login(client, "alice")
    sid = _new_team(client)
    # Removing the only owner -> 400
    assert client.delete(f"/api/v1/spaces/{sid}/members/alice").status_code == 400
    # Demoting the only owner -> 400
    assert (
        client.patch(
            f"/api/v1/spaces/{sid}/members/alice", json={"role": "member"}
        ).status_code
        == 400
    )


def test_personal_space_immutable(client):
    login(client, "alice")
    pid = personal_space_id(client)
    assert client.patch(f"/api/v1/spaces/{pid}", json={"name": "x"}).status_code == 400
    assert (
        client.post(
            f"/api/v1/spaces/{pid}/members", json={"username": "bob"}
        ).status_code
        == 400
    )
    assert client.delete(f"/api/v1/spaces/{pid}").status_code == 400


def test_delete_team_space(client):
    login(client, "alice")
    sid = _new_team(client)
    assert client.delete(f"/api/v1/spaces/{sid}").status_code == 200
    ids = [s["id"] for s in client.get("/api/v1/spaces").json()["spaces"]]
    assert sid not in ids


def test_sessions_scoped_to_space(client):
    login(client, "alice")
    team = _new_team(client)
    sess = client.post("/api/v1/sessions", json={"space_id": team}).json()["session"]
    assert sess["space_id"] == team

    pid = personal_space_id(client)
    personal = client.get("/api/v1/sessions", params={"space_id": pid}).json()[
        "sessions"
    ]
    assert sess["id"] not in [s["id"] for s in personal]
    team_list = client.get("/api/v1/sessions", params={"space_id": team}).json()[
        "sessions"
    ]
    assert sess["id"] in [s["id"] for s in team_list]

    # Non-member bob accessing team-space sessions -> 403
    login(client, "bob")
    assert (
        client.get("/api/v1/sessions", params={"space_id": team}).status_code == 403
    )
    assert (
        client.post("/api/v1/sessions", json={"space_id": team}).status_code == 403
    )


def test_create_session_requires_space_id(client):
    login(client, "alice")
    assert client.post("/api/v1/sessions", json={}).status_code == 422


def test_add_member_by_email_creates_stub(client):
    login(client, "alice")
    sid = _new_team(client)
    r = client.post(
        f"/api/v1/spaces/{sid}/members",
        json={"email": "zhang.san@example.com"},
    )
    assert r.status_code == 201
    members = {m["username"]: m for m in r.json()["members"]}
    assert "zhang.san" in members
    assert members["zhang.san"]["email"] == "zhang.san@example.com"  # stub kept the email
    found = client.get("/api/v1/users/search", params={"q": "zhang"}).json()["users"]
    assert any(u["username"] == "zhang.san" for u in found)


def test_email_invite_merges_on_login(client):
    """A stub created by email invite (username = email local part) merges with
    that user's login record under the same username."""
    login(client, "alice")
    sid = _new_team(client)
    r = client.post(
        f"/api/v1/spaces/{sid}/members", json={"email": "zhang.san@example.com"}
    )
    assert r.status_code == 201
    # The invitee logs in under the email-local-part username (dev-login goes
    # through upsert_user + ensure_personal_space) and should see the invited
    # space — proving both records merged under the same key
    login(client, "zhang.san")
    assert sid in [s["id"] for s in client.get("/api/v1/spaces").json()["spaces"]]


def test_sessions_nonexistent_space_404(client):
    login(client, "alice")
    assert (
        client.get(
            "/api/v1/sessions", params={"space_id": "does-not-exist"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/sessions", json={"space_id": "does-not-exist"}
        ).status_code
        == 404
    )


def test_update_space_returns_none_when_missing(tmp_path):
    """The premise for PATCH translating update_space's None (concurrent
    deletion) into a 404: a missing space returns None."""
    from noeta.agent.store.spaces import SpaceStore

    store = SpaceStore(tmp_path / "app.db")
    try:
        assert store.update_space("nope", name="x") is None
    finally:
        store.close()


def test_add_member_stub_does_not_clobber_profile(tmp_path):
    """Adding an already-logged-in user to a space must not wipe their
    email/name/avatar with the stub.

    Adapted from the source: the original drove this through the external SSO
    login path (dropped surface); the invariant lives in the store layer —
    add_member uses ensure_user (INSERT OR IGNORE), which must not overwrite a
    profile that upsert_user (login) already completed.
    """
    from noeta.agent.store.users import UserStore

    store = UserStore(tmp_path / "app.db")
    try:
        # bob logs in first; the profile lands in the table
        store.upsert_user("bob", email="bob@x.com", name="Bob", avatar="av")
        # bob is then added to another space (the stub path)
        store.ensure_user("bob")
        user = store.get_user("bob")
        assert user is not None
        assert user.name == "Bob"  # profile kept, not wiped to None
        assert user.email == "bob@x.com"
        assert user.avatar == "av"
    finally:
        store.close()


def test_backfill_migrates_legacy_sessions(tmp_path):
    """A legacy DB (no space_id column) migrates on open; backfill assigns
    existing sessions to the personal space."""
    import sqlite3

    from noeta.agent.store.sessions import SessionStore
    from noeta.agent.store.spaces import SpaceStore

    db = tmp_path / "app.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user TEXT NOT NULL,"
        " title TEXT NOT NULL, model TEXT NOT NULL, task_id TEXT,"
        " status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('s1','alice','t','m',NULL,'idle',1,1)"
    )
    conn.commit()
    conn.close()

    store = SessionStore(db)  # __init__ ALTERs the space_id column in
    spaces = SpaceStore(db)
    try:
        store.backfill_space_ids(spaces.ensure_personal_space)
        row = store.get("s1")
        assert row is not None
        assert row.space_id == spaces.ensure_personal_space("alice")
    finally:
        store.close()
        spaces.close()
