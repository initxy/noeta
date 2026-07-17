"""UserStore upsert semantics + email_prefix derivation guard + the
/users/search API.

Adapted from the source: the remote user-search service is dropped in this
build — /users/search is backed only by the local users table, so the
remote-merge / fallback / filtering cases (and the fake httpx.AsyncClient
machinery serving them) have no target and were removed.
"""
from __future__ import annotations

import time

from noeta.agent.auth.deps import _email_prefix
from noeta.agent.store.users import UserStore
from tests.conftest import login


def test_upsert_updated_at_stable_until_profile_changes(tmp_path):
    store = UserStore(tmp_path / "app.db")
    try:
        store.upsert_user("alice", email="a@x.com", name="Alice", avatar="av1")
        u1 = store.get_user("alice")
        assert u1 is not None

        # repeated upsert with the same profile (happens on every request):
        # updated_at does not refresh
        time.sleep(0.01)
        store.upsert_user("alice", email="a@x.com", name="Alice", avatar="av1")
        u2 = store.get_user("alice")
        assert u2 is not None
        assert u2.updated_at == u1.updated_at
        assert u2.created_at == u1.created_at

        # dev-login stub (all None) upserted repeatedly does not refresh either
        store.upsert_user("bob")
        b1 = store.get_user("bob")
        time.sleep(0.01)
        store.upsert_user("bob")
        b2 = store.get_user("bob")
        assert b2 is not None and b1 is not None
        assert b2.updated_at == b1.updated_at

        # profile change: updated_at refreshes, created_at is preserved
        time.sleep(0.01)
        store.upsert_user("alice", email="a@x.com", name="Alice B", avatar="av1")
        u3 = store.get_user("alice")
        assert u3 is not None
        assert u3.updated_at > u1.updated_at
        assert u3.name == "Alice B"
        assert u3.created_at == u1.created_at
    finally:
        store.close()


def test_users_search_api_prefix_match(client):
    # logging in writes the user into the users table; prefix search matches
    # by username/email/name
    login(client, "alice")
    login(client, "alicia")
    login(client, "bob")
    res = client.get("/api/v1/users/search", params={"q": "ali"}).json()["users"]
    names = {u["username"] for u in res}
    assert "alice" in names and "alicia" in names
    assert "bob" not in names
    # no match returns empty
    assert client.get("/api/v1/users/search", params={"q": "zzz"}).json()["users"] == []
    # unauthenticated 401
    fresh = client
    fresh.post("/api/v1/auth/logout")
    assert fresh.get("/api/v1/users/search", params={"q": "a"}).status_code == 401


def test_email_prefix_guards_non_string():
    assert _email_prefix("carbon@example.com", "u") == "carbon"
    assert _email_prefix("plainname", "u") == "plainname"
    assert _email_prefix(None, "u") == "u"
    assert _email_prefix("", "u") == "u"
    # an identity provider returning an unexpected type (non-str) must not
    # crash; fall back to username
    assert _email_prefix(12345, "u") == "u"  # type: ignore[arg-type]
    assert _email_prefix({"a": 1}, "u") == "u"  # type: ignore[arg-type]
