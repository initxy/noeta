"""Admin console API: the admin gate (non-admin 404), read-only queries,
dynamic-config hot effect."""
from __future__ import annotations

import pytest

from tests.conftest import create_session, login, personal_space_id


@pytest.fixture
def admin_client(make_client):
    """A client where alice is admin."""
    client = make_client(ADMIN_USERS="alice")
    login(client, "alice")
    return client


# ----------------------------------------------------------------- gate
def test_admin_endpoints_require_admin(make_client):
    """Logged in but not admin: every admin endpoint uniformly 404s (hiding
    existence); unauthenticated 401."""
    client = make_client()  # ADMIN_USERS defaults empty -> nobody is admin
    # Unauthenticated
    assert client.get("/api/v1/admin/stats").status_code == 401
    # Logged in but not admin -> 404
    login(client, "alice")
    for path in (
        "/api/v1/admin/stats",
        "/api/v1/admin/users",
        "/api/v1/admin/sessions",
        "/api/v1/admin/spaces",
        "/api/v1/admin/config",
    ):
        assert client.get(path).status_code == 404, path


def test_me_reports_is_admin(make_client):
    client = make_client(ADMIN_USERS="alice,carol")
    login(client, "alice")
    assert client.get("/api/v1/auth/me").json()["user"]["is_admin"] is True
    login(client, "bob")
    assert client.get("/api/v1/auth/me").json()["user"]["is_admin"] is False


# ----------------------------------------------------------------- overview
def test_admin_stats(admin_client):
    space = personal_space_id(admin_client)
    create_session(admin_client, space)
    stats = admin_client.get("/api/v1/admin/stats").json()
    assert stats["users"] >= 1
    assert stats["spaces"] >= 1
    assert stats["sessions"]["total"] >= 1
    assert stats["sessions"]["by_status"].get("idle", 0) >= 1
    # All count fields are present
    for key in ("knowledge_sources", "builtin_skills", "space_skills"):
        assert key in stats


# ----------------------------------------------------------------- users
def test_admin_users_pagination_and_search(admin_client):
    # Create a few users (logging in upserts); switch back to alice (admin)
    # to read the list
    for name in ("bob", "carol", "dave"):
        login(admin_client, name)
    login(admin_client, "alice")

    first = admin_client.get("/api/v1/admin/users", params={"offset": 0, "limit": 2}).json()
    assert first["total"] >= 4
    assert len(first["users"]) == 2
    assert first["limit"] == 2
    # The second page does not repeat the first
    second = admin_client.get(
        "/api/v1/admin/users", params={"offset": 2, "limit": 2}
    ).json()
    ids1 = {u["username"] for u in first["users"]}
    ids2 = {u["username"] for u in second["users"]}
    assert ids1.isdisjoint(ids2)
    # Prefix search
    got = admin_client.get("/api/v1/admin/users", params={"q": "car"}).json()
    assert [u["username"] for u in got["users"]] == ["carol"]


# ----------------------------------------------------------------- tasks (sessions)
def test_admin_sessions_list_and_filter(admin_client):
    space = personal_space_id(admin_client)
    sid = create_session(admin_client, space)

    listing = admin_client.get("/api/v1/admin/sessions").json()
    assert listing["total"] >= 1
    row = next(r for r in listing["sessions"] if r["id"] == sid)
    assert row["user"] == "alice"
    assert row["space_name"]  # the space name is included

    # user filter hit / miss
    assert admin_client.get(
        "/api/v1/admin/sessions", params={"user": "alice"}
    ).json()["total"] >= 1
    assert admin_client.get(
        "/api/v1/admin/sessions", params={"user": "nobody"}
    ).json()["total"] == 0
    # status filter (a fresh session is idle)
    assert admin_client.get(
        "/api/v1/admin/sessions", params={"status": "idle"}
    ).json()["total"] >= 1


# ----------------------------------------------------------------- spaces + drilldown
def test_admin_spaces_and_drilldown(admin_client):
    space = personal_space_id(admin_client)
    create_session(admin_client, space)

    spaces = admin_client.get("/api/v1/admin/spaces").json()
    row = next(s for s in spaces["spaces"] if s["id"] == space)
    assert row["member_count"] >= 1
    assert row["session_count"] >= 1
    assert row["is_personal"] is True

    # Member drilldown
    members = admin_client.get(f"/api/v1/admin/spaces/{space}/members").json()
    assert any(m["username"] == "alice" for m in members["members"])
    # Knowledge-source drilldown (empty)
    assert admin_client.get(
        f"/api/v1/admin/spaces/{space}/knowledge"
    ).json()["sources"] == []
    # Skill drilldown (returns a list structure)
    skills = admin_client.get(f"/api/v1/admin/spaces/{space}/skills").json()
    assert isinstance(skills["skills"], list)

    # Nonexistent space -> 404
    assert admin_client.get("/api/v1/admin/spaces/nope/members").status_code == 404


# ----------------------------------------------------------------- dynamic config
def test_admin_config_get_lists_registered_items(admin_client):
    # The registry holds only dev_login_enabled in this app (the source also
    # registered a marketplace toggle; that surface is gone).
    items = {i["key"]: i for i in admin_client.get("/api/v1/admin/config").json()["items"]}
    assert "dev_login_enabled" in items
    dev = items["dev_login_enabled"]
    assert dev["value"] is True and dev["default"] is True
    assert dev["overridden"] is False


def test_admin_config_dev_login_hot_effect(admin_client):
    """Changing dev_login_enabled takes effect immediately: /auth/config
    reflects the new value and dev-login is blocked."""
    # Initially: the public config shows dev_login enabled
    assert admin_client.get("/api/v1/auth/config").json()["dev_login_enabled"] is True

    r = admin_client.put(
        "/api/v1/admin/config/dev_login_enabled", json={"value": False}
    )
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["value"] is False and item["overridden"] is True
    assert item["updated_by"] == "alice"

    # Hot effect: both the public config and the dev-login behavior change
    assert admin_client.get("/api/v1/auth/config").json()["dev_login_enabled"] is False
    # The logged-in admin's cookie is unaffected (still admin), but a new
    # dev-login is rejected
    fresh = admin_client.post("/api/v1/auth/dev-login", json={"username": "zoe"})
    assert fresh.status_code == 403


def test_admin_config_rejects_unknown_key_and_bad_value(admin_client):
    assert admin_client.put(
        "/api/v1/admin/config/no_such_key", json={"value": True}
    ).status_code == 404
    assert admin_client.put(
        "/api/v1/admin/config/dev_login_enabled", json={"value": "not-a-bool"}
    ).status_code == 422
