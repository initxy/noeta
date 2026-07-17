"""Citation path resolution (resolve-paths): service-layer unit tests + API
end-to-end.

The service layer calls knowledge_resolve.resolve_paths directly (against a
fabricated materialization directory); the API layer runs on real uvicorn +
httpx (the conftest client fixture) — the source is created via the API, the
materialized directory and name symlink are fabricated by hand (the sync flow
is covered separately by test_knowledge_sync).
"""
from __future__ import annotations

import pytest

from noeta.agent.services import knowledge_resolve
from noeta.agent.services.knowledge_resolve import InvalidPathError, parse_citation_path
from tests.conftest import login

# ----------------------------------------------------------- helpers

DOC_MD = """\
---
title: "Video events"
origin-url: https://docs.example.com/objA
obj-token: objA
obj-type: docx
breadcrumb: ["tracking", "video"]
obj-edit-time: 1700000000
converted-at: 1700000001
---

# Video events

An overview paragraph.

## Video exposure

The video_show event, reported on room entry.

- param room_id
- param enter_from

### Exposure dedup

Deduplicated by room_id.

## Video click

The video_click event.
"""


def _make_source_dir(root, space_id, name, source_id, files):
    """Fabricate a materialization directory + name symlink:
    root/<space>/<id>/<files>, with <space>/<name> -> <id>."""
    src_dir = root / space_id / source_id
    src_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = src_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    link = root / space_id / name
    if not link.exists():
        link.symlink_to(source_id, target_is_directory=True)
    return src_dir


def _fake_get_source(sources: dict):
    """get_source_by_name stub: {(space_id, name): source dict}."""
    return lambda space_id, name: sources.get((space_id, name))


# ----------------------------------------------------- parse_citation_path


def test_parse_path_with_anchor():
    rel, anchor = parse_citation_path("knowledge/my-docs/video/video-events.md#Video exposure")
    assert rel == "my-docs/video/video-events.md"
    assert anchor == "Video exposure"


def test_parse_path_without_anchor():
    rel, anchor = parse_citation_path("knowledge/my-docs/INDEX.md")
    assert rel == "my-docs/INDEX.md"
    assert anchor is None


@pytest.mark.parametrize(
    "raw",
    [
        "workspace/out.md",  # not a knowledge/ prefix
        "knowledge/../secret.md",  # directory traversal
        "knowledge/docs/../../x.md",
        "knowledge/onlyname",  # missing the file segment
        "knowledge/a\\b.md",  # backslash
        "knowledge/" + "x" * 600,  # overlong
    ],
)
def test_parse_path_rejects_malformed(raw):
    with pytest.raises(InvalidPathError):
        parse_citation_path(raw)


# ------------------------------------------------------- resolve (service layer)


def test_resolve_doc_with_anchor(tmp_path):
    root = tmp_path / "knowledge"
    _make_source_dir(root, "sp1", "my-docs", "src1", {"video/video-events.md": DOC_MD})
    items = knowledge_resolve.resolve_paths(
        "sp1",
        ["knowledge/my-docs/video/video-events.md#Video exposure"],
        root,
        _fake_get_source({("sp1", "my-docs"): {"type": "local_dir"}}),
    )
    (it,) = items
    assert it["exists"] is True
    assert it["anchor_found"] is True
    assert it["title"] == "Video events"
    assert it["origin_url"] == "https://docs.example.com/objA"
    assert it["source_name"] == "my-docs"
    assert it["source_type"] == "local_dir"
    # The excerpt contains this section's body and sub-heading content, but
    # not the content of the next same-level heading
    assert "video_show" in it["excerpt"]
    assert "Exposure dedup" in it["excerpt"]
    assert "video_click" not in it["excerpt"]


def test_resolve_doc_anchor_missing(tmp_path):
    """File present, anchor absent: exists=True + anchor_found=False (the
    frontend shows "the original has been updated")."""
    root = tmp_path / "knowledge"
    _make_source_dir(root, "sp1", "my-docs", "src1", {"video/video-events.md": DOC_MD})
    items = knowledge_resolve.resolve_paths(
        "sp1",
        ["knowledge/my-docs/video/video-events.md#Nonexistent heading"],
        root,
        _fake_get_source({("sp1", "my-docs"): {"type": "local_dir"}}),
    )
    (it,) = items
    assert it["exists"] is True
    assert it["anchor_found"] is False
    assert it["excerpt"] is None
    assert it["origin_url"]  # the document-level jump still works


def test_resolve_missing_file(tmp_path):
    root = tmp_path / "knowledge"
    _make_source_dir(root, "sp1", "my-docs", "src1", {})
    items = knowledge_resolve.resolve_paths(
        "sp1",
        ["knowledge/my-docs/made-up.md#whatever"],
        root,
        _fake_get_source({("sp1", "my-docs"): {"type": "local_dir"}}),
    )
    (it,) = items
    assert it["exists"] is False
    assert it["title"] is None and it["origin_url"] is None


def test_resolve_no_anchor_no_excerpt(tmp_path):
    root = tmp_path / "knowledge"
    _make_source_dir(root, "sp1", "my-docs", "src1", {"video/video-events.md": DOC_MD})
    (it,) = knowledge_resolve.resolve_paths(
        "sp1",
        ["knowledge/my-docs/video/video-events.md"],
        root,
        _fake_get_source({("sp1", "my-docs"): {"type": "local_dir"}}),
    )
    assert it["exists"] is True
    assert it["anchor_found"] is None
    assert it["excerpt"] is None


def test_resolve_git_repo_degraded(tmp_path):
    """git_repo source: exists + filename title, no url and no excerpt (the
    v1 degradation)."""
    root = tmp_path / "knowledge"
    _make_source_dir(root, "sp1", "tracking-sdk", "src2", {"src/track.py": "def track(): ..."})
    (it,) = knowledge_resolve.resolve_paths(
        "sp1",
        ["knowledge/tracking-sdk/src/track.py"],
        root,
        _fake_get_source({("sp1", "tracking-sdk"): {"type": "git_repo"}}),
    )
    assert it["exists"] is True
    assert it["source_type"] == "git_repo"
    assert it["title"] == "track"
    assert it["origin_url"] is None and it["excerpt"] is None


def test_resolve_id_path_falls_back_to_source_name(tmp_path):
    """The agent cites id paths when it searched the materialized id
    directory: look up by id, and source_name echoes the display name."""
    root = tmp_path / "knowledge"
    _make_source_dir(root, "sp1", "my-docs", "src1", {"video/video-events.md": DOC_MD})
    (it,) = knowledge_resolve.resolve_paths(
        "sp1",
        ["knowledge/src1/video/video-events.md#Video exposure"],
        root,
        _fake_get_source({}),
        get_source_by_id=lambda sid: (
            {"id": "src1", "space_id": "sp1", "name": "my-docs", "type": "local_dir"}
            if sid == "src1"
            else None
        ),
    )
    assert it["exists"] is True
    assert it["source_name"] == "my-docs"
    assert it["source_type"] == "local_dir"
    assert it["anchor_found"] is True
    # path keeps the raw shape — the frontend uses it as the match key for
    # resolve results
    assert it["path"] == "knowledge/src1/video/video-events.md"


def test_resolve_id_path_other_space_not_matched(tmp_path):
    """An id hit belonging to another space is not trusted (no leaking of
    other spaces' source names); it stays unresolved."""
    root = tmp_path / "knowledge"
    _make_source_dir(root, "sp1", "my-docs", "src1", {})
    (it,) = knowledge_resolve.resolve_paths(
        "sp1",
        ["knowledge/src9/x.md"],
        root,
        _fake_get_source({}),
        get_source_by_id=lambda sid: {
            "id": "src9", "space_id": "sp2", "name": "someone-elses-docs", "type": "local_dir"
        },
    )
    assert it["exists"] is False
    assert it["source_name"] == "src9"
    assert it["source_type"] is None


def test_resolve_symlink_escape_blocked(tmp_path):
    """A source-name symlink pointing outside the space directory is caught
    by the realpath check backstop: exists=False."""
    root = tmp_path / "knowledge"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("secret", encoding="utf-8")
    space_dir = root / "sp1"
    space_dir.mkdir(parents=True)
    (space_dir / "evil-source").symlink_to(outside, target_is_directory=True)
    (it,) = knowledge_resolve.resolve_paths(
        "sp1",
        ["knowledge/evil-source/leak.md"],
        root,
        _fake_get_source({("sp1", "evil-source"): {"type": "local_dir"}}),
    )
    assert it["exists"] is False


def test_resolve_batch_cap(tmp_path):
    with pytest.raises(InvalidPathError):
        knowledge_resolve.resolve_paths(
            "sp1",
            [f"knowledge/w/{i}.md" for i in range(knowledge_resolve.MAX_PATHS + 1)],
            tmp_path,
            _fake_get_source({}),
        )


# ------------------------------------------------------------- API layer


def _make_client_with_shared(make_client, tmp_path):
    return make_client(SHARED_DATA_DIR=str(tmp_path / "shared"))


def _new_team(client, name="Team A") -> str:
    r = client.post("/api/v1/spaces", json={"name": name, "description": ""})
    assert r.status_code == 201, r.text
    return r.json()["space"]["id"]


def _create_dir_source(client, tmp_path, sid, name="my-docs") -> str:
    origin = tmp_path / "origin-docs"
    origin.mkdir(exist_ok=True)
    r = client.post(
        f"/api/v1/spaces/{sid}/knowledge",
        json={"name": name, "type": "local_dir",
              "config": {"path": str(origin)}},
    )
    assert r.status_code == 201, r.text
    return r.json()["source"]["id"]


def _materialize(tmp_path, space_id, name, source_id, files):
    from pathlib import Path

    return _make_source_dir(
        Path(tmp_path) / "shared" / "knowledge", space_id, name, source_id, files
    )


def test_api_resolve_member_ok(make_client, tmp_path):
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    source_id = _create_dir_source(client, tmp_path, sid)
    _materialize(tmp_path, sid, "my-docs", source_id, {"video/video-events.md": DOC_MD})

    r = client.post(
        f"/api/v1/spaces/{sid}/knowledge/resolve-paths",
        json={"paths": [
            "knowledge/my-docs/video/video-events.md#Video exposure",
            "knowledge/my-docs/nonexistent.md",
        ]},
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["exists"] is True and items[0]["anchor_found"] is True
    assert items[0]["origin_url"].endswith("/objA")
    assert items[1]["exists"] is False


def test_api_resolve_id_path_returns_source_name(make_client, tmp_path):
    """API layer: an id path also resolves, and source_name echoes the
    display name (compatibility with historical messages)."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    source_id = _create_dir_source(client, tmp_path, sid)
    _materialize(tmp_path, sid, "my-docs", source_id, {"video/video-events.md": DOC_MD})

    r = client.post(
        f"/api/v1/spaces/{sid}/knowledge/resolve-paths",
        json={"paths": [f"knowledge/{source_id}/video/video-events.md#Video exposure"]},
    )
    assert r.status_code == 200, r.text
    (it,) = r.json()["items"]
    assert it["exists"] is True and it["anchor_found"] is True
    assert it["source_name"] == "my-docs"
    assert it["source_type"] == "local_dir"


def test_api_resolve_traversal_422(make_client, tmp_path):
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    for bad in ("knowledge/../x.md", "/etc/passwd", "knowledge/w/../../x.md"):
        r = client.post(
            f"/api/v1/spaces/{sid}/knowledge/resolve-paths", json={"paths": [bad]}
        )
        assert r.status_code == 422, (bad, r.text)


def test_api_resolve_non_member_404(make_client, tmp_path):
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    login(client, "mallory")
    r = client.post(
        f"/api/v1/spaces/{sid}/knowledge/resolve-paths",
        json={"paths": ["knowledge/my-docs/x.md"]},
    )
    assert r.status_code == 404, r.text
