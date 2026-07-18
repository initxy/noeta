"""Knowledge API: source CRUD, sync status progress, list doc_count,
permissions.

Runs on real uvicorn + httpx (the conftest client fixture). No real git runs
here: sync-manager entry points are monkeypatched where a syncing state is
needed; adapter-level sync behavior is covered by test_knowledge_sync.py.

Adapted from the source suite: the wiki-browse and docs-tree endpoints are
gone (the wiki exporter and its tree/manifest/sync-report surface were not
ported), so those tests are dropped; source types are now git_repo /
local_dir.
"""
from __future__ import annotations

from tests.conftest import login


# ----------------------------------------------------------- helpers


def _make_client_with_shared(make_client, tmp_path):
    """Start a server with SHARED_DATA_DIR pointed at tmp so materialized
    directories are easy to fabricate."""
    return make_client(SHARED_DATA_DIR=str(tmp_path / "shared"))


def _new_team(client, name="Team A") -> str:
    r = client.post("/api/v1/spaces", json={"name": name, "description": ""})
    assert r.status_code == 201, r.text
    return r.json()["space"]["id"]


def _create_git_source(client, sid, name="my-repo", config=None) -> str:
    cfg = config if config is not None else {"url": "https://git.example.com/org/repo.git"}
    r = client.post(
        f"/api/v1/spaces/{sid}/knowledge",
        json={"name": name, "type": "git_repo", "config": cfg},
    )
    assert r.status_code == 201, r.text
    return r.json()["source"]["id"]


# ----------------------------------------------------------- create validation


def test_create_validates_config(make_client, tmp_path):
    """Per-type required config fields: git_repo needs url, local_dir needs
    path (422 otherwise)."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)

    r = client.post(
        f"/api/v1/spaces/{sid}/knowledge",
        json={"name": "no-url", "type": "git_repo", "config": {}},
    )
    assert r.status_code == 422, r.text
    r = client.post(
        f"/api/v1/spaces/{sid}/knowledge",
        json={"name": "no-path", "type": "local_dir", "config": {}},
    )
    assert r.status_code == 422, r.text

    # Valid local_dir create passes
    r = client.post(
        f"/api/v1/spaces/{sid}/knowledge",
        json={"name": "docs", "type": "local_dir",
              "config": {"path": str(tmp_path / "docs")}},
    )
    assert r.status_code == 201, r.text
    assert r.json()["source"]["status"] == "pending"


# ----------------------------------------------------------- sync status progress


def test_sync_status_progress_when_syncing(make_client, tmp_path, monkeypatch):
    """While syncing, sync status carries progress (mock
    manager.get_progress); a second trigger during the run → 409 whose detail
    matches "syncing"."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    source_id = _create_git_source(client, sid)

    # Monkeypatch start_sync to only claim the syncing status + set progress
    # (no background thread): the claim keeps the real atomic try_set_syncing
    # semantics so the second POST still conflicts.
    from noeta.agent.services.knowledge_sync import KnowledgeSyncManager

    def fake_start_sync(self, s, triggered_by):
        if not self._store.try_set_syncing(s):
            raise ValueError("this knowledge source is already syncing")
        self._set_progress(s, {"phase": "cloning", "existing": False})
        return self._store.get_source(s)

    monkeypatch.setattr(KnowledgeSyncManager, "start_sync", fake_start_sync)

    r = client.post(f"/api/v1/spaces/{sid}/knowledge/{source_id}/sync")
    assert r.status_code == 202, r.text
    r = client.get(f"/api/v1/spaces/{sid}/knowledge/{source_id}/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "syncing"
    assert body["progress"]["phase"] == "cloning"

    # Sync already in progress → 409, detail matches the "syncing" substring
    r = client.post(f"/api/v1/spaces/{sid}/knowledge/{source_id}/sync")
    assert r.status_code == 409, r.text
    assert "syncing" in r.json()["detail"]


def test_sync_status_progress_null_when_not_syncing(make_client, tmp_path):
    """Not syncing → progress:null (and report:null; the target keeps the
    field for payload-shape compatibility)."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    source_id = _create_git_source(client, sid)  # status=pending

    r = client.get(f"/api/v1/spaces/{sid}/knowledge/{source_id}/sync")
    assert r.status_code == 200
    assert r.json()["progress"] is None
    assert r.json()["report"] is None


# ----------------------------------------------------------- list doc_count


def test_list_sources_has_doc_count(make_client, tmp_path):
    """Each listed source carries doc_count (read from config.doc_count)."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    _create_git_source(client, sid, name="s1", config={
        "url": "https://git.example.com/org/a.git", "doc_count": 7})
    _create_git_source(client, sid, name="s2", config={
        "url": "https://git.example.com/org/b.git"})  # no doc_count

    r = client.get(f"/api/v1/spaces/{sid}/knowledge")
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["sources"]}
    assert by_name["s1"]["doc_count"] == 7
    assert by_name["s2"]["doc_count"] is None


def test_list_sources_has_failed_count(make_client, tmp_path):
    """Each listed source carries failed_count (read from
    config.failed_count, a cache)."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    _create_git_source(client, sid, name="s1", config={
        "url": "https://git.example.com/org/a.git", "failed_count": 62})
    _create_git_source(client, sid, name="s2", config={
        "url": "https://git.example.com/org/b.git"})  # no failed_count

    r = client.get(f"/api/v1/spaces/{sid}/knowledge")
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["sources"]}
    assert by_name["s1"]["failed_count"] == 62
    assert by_name["s2"]["failed_count"] is None


# ----------------------------------------------------------- PATCH config


def test_patch_updates_config(make_client, tmp_path):
    """PATCH with a config body updates it (branch switch for a git_repo
    source; the source suite exercised the wiki selection here)."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    source_id = _create_git_source(client, sid)

    r = client.patch(
        f"/api/v1/spaces/{sid}/knowledge/{source_id}",
        json={"config": {"url": "https://git.example.com/org/repo.git",
                         "branch": "dev"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["source"]["config"]["branch"] == "dev"


def test_patch_non_owner_403(make_client, tmp_path):
    """PATCH by a non-owner → 403."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    source_id = _create_git_source(client, sid)
    client.post(f"/api/v1/spaces/{sid}/members", json={"username": "bob"})

    login(client, "bob")
    r = client.patch(
        f"/api/v1/spaces/{sid}/knowledge/{source_id}",
        json={"config": {"url": "https://git.example.com/org/repo.git"}},
    )
    assert r.status_code == 403


def test_member_can_read_list_and_sync_status(make_client, tmp_path):
    """Members can read (list / sync status); non-members get 404 (hiding
    existence)."""
    client = _make_client_with_shared(make_client, tmp_path)
    login(client, "alice")
    sid = _new_team(client)
    source_id = _create_git_source(client, sid)
    client.post(f"/api/v1/spaces/{sid}/members", json={"username": "bob"})

    login(client, "bob")  # member
    assert client.get(f"/api/v1/spaces/{sid}/knowledge").status_code == 200
    r = client.get(f"/api/v1/spaces/{sid}/knowledge/{source_id}/sync")
    assert r.status_code == 200

    login(client, "mallory")  # non-member
    assert client.get(f"/api/v1/spaces/{sid}/knowledge").status_code == 404
