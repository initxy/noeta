"""Skill enable toggles + assembly filtering.

- Space skills: the owner disables via PATCH /spaces/{id}/skills/{name}
  (updating this space's row); once disabled, new sessions do not assemble it.
- Builtin skills: global. A space cannot disable them (the space PATCH
  endpoint 404s for builtins because they are not rows of this space); after a
  global disable through the admin PATCH /skills, no session assembles them and
  they disappear from every space list.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.conftest import create_session, login, personal_space_id

SPACE = "/api/v1/spaces/{sid}/skills"
STOGGLE = "/api/v1/spaces/{sid}/skills/{name}"


def _md(name: str, desc: str = "demo") -> bytes:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\nBody\n".encode()


@pytest.fixture
def admin(make_client, tmp_path):
    """admin client: builtin skills land in an isolated shared directory, and
    the user is also the personal-space owner."""
    client = make_client(SHARED_DATA_DIR=str(tmp_path / "shared"), ADMIN_USERS="alice")
    login(client, "alice")
    return client


def _upload_builtin(client, name: str) -> None:
    r = client.post(
        "/api/v1/skills",
        files={"file": (f"{name}.md", _md(name), "text/markdown")},
    )
    assert r.status_code == 200, r.text


def _upload_space(client, sid: str, name: str) -> None:
    r = client.post(
        SPACE.format(sid=sid),
        files={"file": (f"{name}.md", _md(name), "text/markdown")},
    )
    assert r.status_code == 201, r.text


def _space_map(client, sid: str) -> dict:
    r = client.get(SPACE.format(sid=sid))
    assert r.status_code == 200, r.text
    return {s["name"]: s for s in r.json()["skills"]}


# ------------------------------------------------------------ toggle endpoints


def test_disable_then_enable_space_skill(admin):
    sid = personal_space_id(admin)
    _upload_space(admin, sid, "mysk")
    assert _space_map(admin, sid)["mysk"]["enabled"] is True

    r = admin.patch(STOGGLE.format(sid=sid, name="mysk"), json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "mysk", "enabled": False}
    assert _space_map(admin, sid)["mysk"]["enabled"] is False

    r = admin.patch(STOGGLE.format(sid=sid, name="mysk"), json={"enabled": True})
    assert r.status_code == 200, r.text
    assert _space_map(admin, sid)["mysk"]["enabled"] is True


def test_builtin_read_only_in_space(admin):
    """A builtin shows read-only in the space list (enabled True); the space
    cannot disable it (not a row of this space -> 404)."""
    sid = personal_space_id(admin)
    _upload_builtin(admin, "bsk")
    assert _space_map(admin, sid)["bsk"]["enabled"] is True
    r = admin.patch(STOGGLE.format(sid=sid, name="bsk"), json={"enabled": False})
    assert r.status_code == 404


def test_builtin_global_disable_hides_from_space(admin):
    sid = personal_space_id(admin)
    _upload_builtin(admin, "bsk")
    assert "bsk" in _space_map(admin, sid)
    # Global disable via the admin console -> no longer shown in the space
    # list (the list = the skills sessions actually assemble)
    r = admin.patch("/api/v1/skills/bsk", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert "bsk" not in _space_map(admin, sid)


def test_toggle_unknown_space_skill_404(admin):
    sid = personal_space_id(admin)
    r = admin.patch(STOGGLE.format(sid=sid, name="no-such"), json={"enabled": False})
    assert r.status_code == 404


def test_toggle_requires_owner(admin):
    resp = admin.post("/api/v1/spaces", json={"name": "Team"})
    team_id = resp.json()["space"]["id"]
    admin.post(f"/api/v1/spaces/{team_id}/members", json={"username": "bob"})
    _upload_space(admin, team_id, "mysk")

    login(admin, "bob")
    r = admin.patch(STOGGLE.format(sid=team_id, name="mysk"), json={"enabled": False})
    assert r.status_code == 403


def test_reupload_clears_disabled_state(admin):
    sid = personal_space_id(admin)
    _upload_space(admin, sid, "mysk")
    admin.patch(STOGGLE.format(sid=sid, name="mysk"), json={"enabled": False})
    assert _space_map(admin, sid)["mysk"]["enabled"] is False

    _upload_space(admin, sid, "mysk")  # reinstall -> back to enabled by default
    assert _space_map(admin, sid)["mysk"]["enabled"] is True


def test_delete_clears_disabled_state(admin):
    sid = personal_space_id(admin)
    _upload_space(admin, sid, "mysk")
    admin.patch(STOGGLE.format(sid=sid, name="mysk"), json={"enabled": False})
    assert admin.delete(STOGGLE.format(sid=sid, name="mysk")).status_code == 200

    _upload_space(admin, sid, "mysk")
    assert _space_map(admin, sid)["mysk"]["enabled"] is True


# ------------------------------------------------------------ assembly filtering


def _wait_skill_links(
    data_dir: Path, session_id: str, want: set[str], timeout: float = 15.0
) -> None:
    """Poll the .noeta/skills symlink-name set until it equals want (assembly
    is rebuilt at drive time; this avoids reading the clear-then-rebuild
    intermediate state)."""
    skills_dir = data_dir / "workspaces" / session_id / ".noeta" / "skills"
    deadline = time.time() + timeout
    names: set[str] = set()
    while time.time() < deadline:
        if skills_dir.is_dir():
            names = {p.name for p in skills_dir.iterdir() if p.is_symlink()}
            if names == want:
                return
        time.sleep(0.05)
    raise AssertionError(f"skills symlinks {names!r}, wanted {want!r}")


def test_assembly_excludes_disabled(admin, tmp_path):
    """Assembly = global builtins(enabled) ∪ this space's skills(enabled); a
    globally disabled builtin and a space-disabled skill are each excluded."""
    sid = personal_space_id(admin)
    _upload_builtin(admin, "b-on")
    _upload_builtin(admin, "b-off")
    _upload_space(admin, sid, "s-on")
    _upload_space(admin, sid, "s-off")
    admin.patch("/api/v1/skills/b-off", json={"enabled": False})  # global builtin disable
    r = admin.patch(STOGGLE.format(sid=sid, name="s-off"), json={"enabled": False})
    assert r.status_code == 200, r.text

    session_id = create_session(admin, sid)
    admin.post(f"/api/v1/sessions/{session_id}/messages", json={"content": "hello"})
    _wait_skill_links(tmp_path / "data", session_id, {"b-on", "s-on"})
