"""Builtin-skill management API (admin /skills): list / upload / true delete /
enable-disable / preview / access control.

Builtins all live in the shared directory builtin-skills/; existence is
authoritatively decided by the skills-table rows with space_id="*" (no git /
`.builtin` detection). Tests use an isolated SHARED_DATA_DIR and create every
builtin through the API.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from tests.conftest import login


def _md(name: str, desc: str = "demo description") -> bytes:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n# Body\n".encode()


@pytest.fixture
def admin(make_client, tmp_path):
    """admin client (ADMIN_USERS=alice); builtin skills land in an isolated
    SHARED_DATA_DIR."""
    client = make_client(SHARED_DATA_DIR=str(tmp_path / "shared"), ADMIN_USERS="alice")
    login(client, "alice")
    return client


def _names(client) -> dict:
    return {s["name"]: s for s in client.get("/api/v1/skills").json()["skills"]}


def _upload_md(client, name: str, desc: str = "demo"):
    return client.post(
        "/api/v1/skills",
        files={"file": (f"{name}.md", _md(name, desc), "text/markdown")},
    )


def test_empty_by_default(admin):
    """The platform ships with zero builtins: empty shared directory -> empty
    list."""
    assert admin.get("/api/v1/skills").json()["skills"] == []


def test_upload_list_delete(admin):
    r = _upload_md(admin, "demo", "builtin demo")
    assert r.status_code == 200, r.text
    assert r.json()["skill"] == {
        "name": "demo",
        "description": "builtin demo",
        "source": "builtin",
        "enabled": True,
    }
    got = _names(admin)
    assert got["demo"]["source"] == "builtin"
    assert got["demo"]["enabled"] is True
    # True deletion (both the directory and the table row are gone)
    assert admin.delete("/api/v1/skills/demo").json() == {"ok": True}
    assert "demo" not in _names(admin)


def test_reupload_overwrites_description(admin):
    _upload_md(admin, "demo", "old")
    _upload_md(admin, "demo", "new")
    assert _names(admin)["demo"]["description"] == "new"


def test_toggle_enabled(admin):
    _upload_md(admin, "demo")
    r = admin.patch("/api/v1/skills/demo", json={"enabled": False})
    assert r.json() == {"ok": True, "name": "demo", "enabled": False}
    assert _names(admin)["demo"]["enabled"] is False
    admin.patch("/api/v1/skills/demo", json={"enabled": True})
    assert _names(admin)["demo"]["enabled"] is True


def test_reupload_resets_to_enabled(admin):
    _upload_md(admin, "demo")
    admin.patch("/api/v1/skills/demo", json={"enabled": False})
    _upload_md(admin, "demo")  # reinstall = back to enabled by default
    assert _names(admin)["demo"]["enabled"] is True


def test_upload_zip_with_subdir(admin):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/SKILL.md", _md("zipsk", "from the zip").decode())
        zf.writestr("pkg/reference.md", "# companion file\n")
    r = admin.post(
        "/api/v1/skills", files={"file": ("pkg.zip", buf.getvalue(), "application/zip")}
    )
    assert r.status_code == 200, r.text
    assert r.json()["skill"]["name"] == "zipsk"
    assert _names(admin)["zipsk"]["source"] == "builtin"


def test_preview(admin):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/SKILL.md", _md("pv", "preview").decode())
        zf.writestr("pkg/ref.md", "# companion file\n")
    admin.post(
        "/api/v1/skills", files={"file": ("pkg.zip", buf.getvalue(), "application/zip")}
    )
    tree = admin.get("/api/v1/skills/pv/preview").json()["entries"]
    paths = {e["path"] for e in tree}
    assert "SKILL.md" in paths and "ref.md" in paths
    content = admin.get("/api/v1/skills/pv/preview?path=ref.md").json()
    assert "companion file" in content["content"]
    assert content["binary"] is False
    # Previewing a nonexistent builtin 404s
    assert admin.get("/api/v1/skills/nope/preview").status_code == 404


def test_delete_missing_and_bad_name(admin):
    assert admin.delete("/api/v1/skills/nope").status_code == 404
    # An illegal name (contains a dot, outside [A-Za-z0-9_-]) -> 400, blocking
    # directory-traversal shapes
    assert admin.delete("/api/v1/skills/foo.bar").status_code == 400
    assert admin.patch("/api/v1/skills/nope", json={"enabled": False}).status_code == 404


def test_upload_rejects_bad_name_and_type(admin):
    assert (
        admin.post(
            "/api/v1/skills",
            files={"file": ("x.md", _md("bad name"), "text/markdown")},
        ).status_code
        == 400
    )
    assert (
        admin.post(
            "/api/v1/skills", files={"file": ("x.txt", b"hello", "text/plain")}
        ).status_code
        == 400
    )


def test_requires_auth(make_client, tmp_path):
    anon = make_client(SHARED_DATA_DIR=str(tmp_path / "shared"))
    assert anon.get("/api/v1/skills").status_code == 401


def test_requires_admin(make_client, tmp_path):
    """Non-admins always get 404 (including list / upload / delete / patch)."""
    client = make_client(SHARED_DATA_DIR=str(tmp_path / "shared"))  # no ADMIN_USERS
    login(client, "bob")
    assert client.get("/api/v1/skills").status_code == 404
    assert (
        client.post(
            "/api/v1/skills", files={"file": ("x.md", _md("x"), "text/markdown")}
        ).status_code
        == 404
    )
    assert client.delete("/api/v1/skills/x").status_code == 404
    assert client.patch("/api/v1/skills/x", json={"enabled": False}).status_code == 404


def test_upload_plain_skill_still_ok(admin):
    r = admin.post(
        "/api/v1/skills",
        files={"file": ("plain.md", _md("plain", "plain description"), "text/markdown")},
    )
    assert r.status_code == 200, r.text
