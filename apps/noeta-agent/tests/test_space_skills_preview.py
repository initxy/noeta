"""Space-skill preview endpoint tests (GET /spaces/{id}/skills/{name}/preview).

Covers: no path returns the file tree, with path returns file contents,
builtins cannot be previewed through the space endpoint (builtins do not land
in the space directory -> 404), path traversal is blocked, nonexistent skill /
file 404.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from tests.conftest import login, personal_space_id

PREVIEW = "/api/v1/spaces/{sid}/skills/{name}/preview"


def _md(name: str, desc: str = "demo") -> bytes:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\nBody\n".encode()


def _zip_multi_file(name: str) -> bytes:
    """A skill zip with SKILL.md + references/ref.md (multiple files, to
    verify the file tree)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}/SKILL.md", _md(name, "multi-file skill"))
        z.writestr(f"{name}/references/ref.md", "# ref\nreference file\n".encode())
    return buf.getvalue()


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


def _upload(client, sid: str, zip_bytes: bytes, filename: str) -> None:
    r = client.post(
        f"/api/v1/spaces/{sid}/skills",
        files={"file": (filename, zip_bytes, "application/zip")},
    )
    assert r.status_code == 201, r.text


# ------------------------------------------------------------ file tree / file contents


def test_preview_returns_file_tree(skills_client):
    sid = personal_space_id(skills_client)
    _upload(skills_client, sid, _zip_multi_file("mysk"), "mysk.zip")
    r = skills_client.get(PREVIEW.format(sid=sid, name="mysk"))
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    paths = {e["path"] for e in entries}
    assert "SKILL.md" in paths
    assert "references/ref.md" in paths
    # System junk is excluded from the directory entries
    assert not any("__MACOSX" in p or p.startswith("._") for p in paths)
    # is_dir / size fields are present
    skill_md = next(e for e in entries if e["path"] == "SKILL.md")
    assert skill_md["is_dir"] is False and skill_md["size"] > 0


def test_preview_returns_file_content(skills_client):
    sid = personal_space_id(skills_client)
    _upload(skills_client, sid, _zip_multi_file("mysk"), "mysk.zip")
    r = skills_client.get(
        PREVIEW.format(sid=sid, name="mysk") + "?path=references/ref.md"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "references/ref.md"
    assert "reference file" in body["content"]
    assert body["binary"] is False
    assert body["truncated"] is False


# ------------------------------------------------------------ builtins not via the space endpoint


def test_preview_builtin_not_via_space(skills_client):
    """Builtins do not land in the space directory -> the space preview
    endpoint 404s (previewing builtins goes through the admin /skills)."""
    sid = personal_space_id(skills_client)
    r = skills_client.get(PREVIEW.format(sid=sid, name="builtin-demo"))
    assert r.status_code == 404
    r2 = skills_client.get(
        PREVIEW.format(sid=sid, name="builtin-demo") + "?path=SKILL.md"
    )
    assert r2.status_code == 404


def test_preview_missing_skill_404(skills_client):
    sid = personal_space_id(skills_client)
    r = skills_client.get(PREVIEW.format(sid=sid, name="nope"))
    assert r.status_code == 404


def test_preview_missing_file_404(skills_client):
    sid = personal_space_id(skills_client)
    _upload(skills_client, sid, _zip_multi_file("mysk"), "mysk.zip")
    r = skills_client.get(
        PREVIEW.format(sid=sid, name="mysk") + "?path=nonexistent.md"
    )
    assert r.status_code == 404


# ------------------------------------------------------------ path traversal


def test_preview_rejects_path_traversal(skills_client):
    """A path escaping the skill directory -> 400 (is_relative_to check)."""
    sid = personal_space_id(skills_client)
    _upload(skills_client, sid, _zip_multi_file("mysk"), "mysk.zip")
    for bad in ["../etc/passwd", "../../SKILL.md", "/etc/passwd"]:
        r = skills_client.get(
            PREVIEW.format(sid=sid, name="mysk") + f"?path={bad}"
        )
        assert r.status_code == 400, (bad, r.status_code, r.text)


def test_preview_rejects_bad_skill_name(skills_client):
    """A name with illegal characters (a dot etc.) -> 400 (_validate_name
    blocks directory traversal)."""
    sid = personal_space_id(skills_client)
    r = skills_client.get(PREVIEW.format(sid=sid, name="foo.bar"))
    assert r.status_code == 400
