"""Space-skill registry (space_skills single-table authority) regressions.

Covers the core invariant of that change: assembly reads the registry and does
not scan directories; a registry row whose directory is missing (a half-write
leftover) -> assembly defensively skips it without crashing the session, and
the remaining skills symlink as usual.

The source repo also had a marketplace-install dedup regression here; the
marketplace surface does not exist in this app, so that case is not ported.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from tests.conftest import create_session, login, personal_space_id


def _md(name: str, desc: str = "demo") -> bytes:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\nBody\n".encode()


@pytest.fixture
def reg_client(make_client, tmp_path):
    """One builtin skill (created through the admin /skills, landing in
    builtin-skills/) + an isolated shared_data_dir."""
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
        f"/api/v1/spaces/{sid}/skills",
        files={"file": (f"{name}.md", _md(name), "text/markdown")},
    )
    assert r.status_code == 201, r.text


def _list(client, sid: str) -> list[dict]:
    r = client.get(f"/api/v1/spaces/{sid}/skills")
    assert r.status_code == 200, r.text
    return r.json()["skills"]


# ------------------------------------------------------------ defensive assembly skip


def _wait_skill_links(data_dir: Path, session_id: str, timeout: float = 15.0) -> set[str]:
    """Poll the .noeta/skills symlink-name set (wait for the builtin entry to
    appear stably, then return a snapshot)."""
    skills_dir = data_dir / "workspaces" / session_id / ".noeta" / "skills"
    deadline = time.time() + timeout
    names: set[str] = set()
    while time.time() < deadline:
        if skills_dir.is_dir():
            names = {p.name for p in skills_dir.iterdir() if p.is_symlink()}
            if "builtin-demo" in names:
                return names
        time.sleep(0.05)
    return names


def test_assembly_skips_row_without_dir(reg_client, tmp_path):
    """A registry row whose directory was deleted (a half-write leftover) ->
    assembly skips the bad row; the intact skill and the builtin symlink as
    usual."""
    sid = personal_space_id(reg_client)
    _upload(reg_client, sid, "intact")
    _upload(reg_client, sid, "broken")
    # Both rows are in the list (the list reads the table, unaffected by the
    # directory)
    names = {s["name"] for s in _list(reg_client, sid)}
    assert {"intact", "broken"} <= names

    # Destroy broken's source directory (simulating a lost directory with the
    # row still present)
    broken_dir = tmp_path / "shared" / "space-skills" / sid / "broken"
    assert broken_dir.is_dir()
    shutil.rmtree(broken_dir)

    # Drive assembly: broken is skipped, intact + the builtin symlink
    # normally, and the session does not error
    session_id = create_session(reg_client, sid)
    r = reg_client.post(
        f"/api/v1/sessions/{session_id}/messages", json={"content": "hello"}
    )
    assert r.status_code in (200, 202), r.text
    links = _wait_skill_links(tmp_path / "data", session_id)
    assert "intact" in links
    assert "broken" not in links
    assert "builtin-demo" in links
