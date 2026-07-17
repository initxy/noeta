"""Auth and isolation: dev-login, auth config, cross-user visibility."""
from __future__ import annotations

from tests.conftest import create_session, login, personal_space_id


def test_unauthenticated_401(client):
    assert client.get("/api/v1/sessions").status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401


def test_dev_login_me_logout(client):
    login(client, "alice")
    me = client.get("/api/v1/auth/me").json()["user"]
    assert me["username"] == "alice"
    assert me["email_prefix"] == "alice"
    assert me["email"] is None and me["name"] is None and me["avatar"] is None
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_user_isolation(make_client):
    client = make_client()
    login(client, "alice")
    sid = create_session(client)

    client.post("/api/v1/auth/logout")
    login(client, "bob")
    # bob's personal space has none of alice's sessions; cross-space sessions
    # are neither visible nor deletable
    bob_space = personal_space_id(client)
    assert (
        client.get("/api/v1/sessions", params={"space_id": bob_space}).json()[
            "sessions"
        ]
        == []
    )
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 404


def test_dev_login_disabled_blocks_login(make_client):
    # Deliberate difference from the source: the SSO/external-auth surface is
    # gone; the only flow is dev-login, gated by DEV_LOGIN_ENABLED.
    client = make_client(DEV_LOGIN_ENABLED="false")

    resp = client.post("/api/v1/auth/dev-login", json={"username": "alice"})
    assert resp.status_code == 403

    # No session cookie -> 401
    assert client.get("/api/v1/sessions").status_code == 401


def test_auth_config_public(make_client):
    # Public endpoint: reachable without login. The payload is the dev-login
    # toggle plus provider-contributed login options (empty for the default
    # DevLoginProvider).
    c = make_client()
    body = c.get("/api/v1/auth/config").json()
    assert body == {"dev_login_enabled": True}

    c2 = make_client(DEV_LOGIN_ENABLED="false")
    body2 = c2.get("/api/v1/auth/config").json()
    assert body2 == {"dev_login_enabled": False}
