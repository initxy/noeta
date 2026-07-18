"""AuthProvider seam: the pluggable identity provider (auth/provider.py).

The open-source build ships only DevLoginProvider; deployments substitute
their own provider via build_auth_provider / app.state.auth_provider. These
tests exercise the seam mechanics with a scripted fake — no external identity
system is contacted:

- login_options() fields are merged into the public GET /auth/config payload;
- authenticate() returning a profile short-circuits the session cookie and
  drives upsert + email_prefix derivation + personal-space creation;
- authenticate() returning None falls through to the dev-login cookie path.
"""
from __future__ import annotations

from typing import Optional

from noeta.agent.auth.provider import (
    AuthUser,
    DevLoginProvider,
    build_auth_provider,
)
from noeta.agent.config import Settings
from tests.conftest import login, personal_space_id

# Header used by the scripted provider below to mark "this request carries a
# deployment SSO credential".
_HDR = "x-test-sso-user"


class ScriptedProvider:
    """Fake deployment provider: authenticates any request carrying the
    _HDR header (username taken from the header value) and contributes a
    login_url to /auth/config."""

    def login_options(self) -> dict:
        return {"login_url": "https://idp.example/login"}

    async def authenticate(self, request) -> Optional[AuthUser]:
        username = request.headers.get(_HDR)
        if not username:
            return None
        return AuthUser(
            username=username,
            email=f"{username}@example.com",
            name="Carbon Person",
            avatar="https://cdn/av.png",
        )


def _plug(monkeypatch, provider) -> None:
    """Substitute the provider factory before make_client boots the app
    (main.py wires build_auth_provider's result into app.state)."""
    monkeypatch.setattr(
        "noeta.agent.main.build_auth_provider", lambda settings: provider
    )


def test_build_auth_provider_returns_dev_login():
    provider = build_auth_provider(Settings(_env_file=None, session_secret="s"))
    assert isinstance(provider, DevLoginProvider)
    # The default provider contributes nothing and authenticates nobody
    assert provider.login_options() == {}


def test_login_options_merged_into_auth_config(make_client, monkeypatch):
    _plug(monkeypatch, ScriptedProvider())
    client = make_client()
    body = client.get("/api/v1/auth/config").json()
    assert body == {
        "dev_login_enabled": True,
        "login_url": "https://idp.example/login",
    }


def test_authenticate_short_circuits_cookie(make_client, monkeypatch):
    _plug(monkeypatch, ScriptedProvider())
    client = make_client()

    # No credential at all -> falls through to the (absent) cookie -> 401
    assert client.get("/api/v1/auth/me").status_code == 401

    # Header credential -> authenticated without any dev-login cookie; the
    # profile flows through upsert + email_prefix derivation
    me = client.get("/api/v1/auth/me", headers={_HDR: "carbon"}).json()["user"]
    assert me == {
        "username": "carbon",
        "email": "carbon@example.com",
        "email_prefix": "carbon",
        "name": "Carbon Person",
        "avatar": "https://cdn/av.png",
        "is_admin": False,
    }

    # The personal space is ensured on provider-authenticated requests too
    resp = client.get("/api/v1/spaces", headers={_HDR: "carbon"})
    assert resp.status_code == 200
    assert any(s["is_personal"] for s in resp.json()["spaces"])


def test_authenticate_none_falls_through_to_dev_login(make_client, monkeypatch):
    _plug(monkeypatch, ScriptedProvider())
    client = make_client()

    # Without the header the scripted provider declines, and the ordinary
    # dev-login cookie path still works end to end
    login(client, "alice")
    me = client.get("/api/v1/auth/me").json()["user"]
    assert me["username"] == "alice"
    assert personal_space_id(client)
