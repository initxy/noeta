"""Skill user-group tests (PUT /spaces/{id}/skills/{name}/group).

Covers: group / regroup / ungroup reflected in the list's group field, builtins
cannot be grouped (not a row of this space -> 404), unknown skill 404,
non-owner 403, over-long group name 400, delete / reinstall clears the group.
"""
from __future__ import annotations

import pytest

from tests.conftest import login, personal_space_id

SKILLS = "/api/v1/spaces/{sid}/skills"
GROUP = "/api/v1/spaces/{sid}/skills/{name}/group"


def _md(name: str, desc: str = "demo") -> bytes:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\nBody\n".encode()


@pytest.fixture
def skills_client(make_client, tmp_path):
    """One builtin skill (created through the admin /skills) + an isolated
    shared_data_dir."""
    client = make_client(
        SHARED_DATA_DIR=str(tmp_path / "shared"), ADMIN_USERS="alice"
    )
    login(client, "alice")
    r = client.post(
        "/api/v1/skills",
        files={
            "file": ("builtin-demo.md", _md("builtin-demo", "builtin demo"), "text/markdown")
        },
    )
    assert r.status_code == 200, r.text
    return client


def _upload(client, sid: str, name: str) -> None:
    r = client.post(
        SKILLS.format(sid=sid),
        files={"file": (f"{name}.md", _md(name), "text/markdown")},
    )
    assert r.status_code == 201, r.text


def _group_map(client, sid: str) -> dict[str, object]:
    r = client.get(SKILLS.format(sid=sid))
    assert r.status_code == 200, r.text
    return {s["name"]: s.get("group") for s in r.json()["skills"]}


def test_assign_reassign_and_remove_group(skills_client):
    sid = personal_space_id(skills_client)
    _upload(skills_client, sid, "mysk")
    assert _group_map(skills_client, sid)["mysk"] is None

    # Assign a group
    r = skills_client.put(GROUP.format(sid=sid, name="mysk"), json={"group": "data"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "mysk", "group": "data"}
    assert _group_map(skills_client, sid)["mysk"] == "data"

    # Regroup (with surrounding whitespace; the server strips it)
    r = skills_client.put(GROUP.format(sid=sid, name="mysk"), json={"group": "  ops "})
    assert r.status_code == 200, r.text
    assert _group_map(skills_client, sid)["mysk"] == "ops"

    # Remove from the group (empty string is equivalent to null)
    r = skills_client.put(GROUP.format(sid=sid, name="mysk"), json={"group": ""})
    assert r.status_code == 200, r.text
    assert r.json()["group"] is None
    assert _group_map(skills_client, sid)["mysk"] is None


def test_builtin_cannot_be_grouped(skills_client):
    """A builtin is a global row, not owned by this space -> the space grouping
    endpoint 404s; in the list a builtin's group is always None."""
    sid = personal_space_id(skills_client)
    r = skills_client.put(
        GROUP.format(sid=sid, name="builtin-demo"), json={"group": "system"}
    )
    assert r.status_code == 404
    assert _group_map(skills_client, sid)["builtin-demo"] is None


def test_group_unknown_skill_404(skills_client):
    sid = personal_space_id(skills_client)
    r = skills_client.put(GROUP.format(sid=sid, name="no-such"), json={"group": "x"})
    assert r.status_code == 404


def test_group_name_too_long_400(skills_client):
    sid = personal_space_id(skills_client)
    _upload(skills_client, sid, "mysk")
    r = skills_client.put(
        GROUP.format(sid=sid, name="mysk"), json={"group": "x" * 33}
    )
    assert r.status_code == 400
    assert _group_map(skills_client, sid)["mysk"] is None


def test_group_requires_owner(skills_client):
    login(skills_client, "alice")
    resp = skills_client.post("/api/v1/spaces", json={"name": "Team"})
    team_id = resp.json()["space"]["id"]
    _upload(skills_client, team_id, "teamsk")
    skills_client.post(f"/api/v1/spaces/{team_id}/members", json={"username": "bob"})

    login(skills_client, "bob")
    r = skills_client.put(
        GROUP.format(sid=team_id, name="teamsk"), json={"group": "x"}
    )
    assert r.status_code == 403


def test_delete_clears_group(skills_client):
    sid = personal_space_id(skills_client)
    _upload(skills_client, sid, "mysk")
    skills_client.put(GROUP.format(sid=sid, name="mysk"), json={"group": "data"})
    assert (
        skills_client.delete(
            SKILLS.format(sid=sid) + "/mysk"
        ).status_code
        == 200
    )
    # Reinstalling the same-named skill: no leftover group
    _upload(skills_client, sid, "mysk")
    assert _group_map(skills_client, sid)["mysk"] is None
