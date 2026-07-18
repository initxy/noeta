"""Knowledge-source sync: the git_repo / local_dir adapters against real
local fixtures (a bare git repository + a plain directory) — no network.

Rewritten from the source suite (which stubbed the wiki exporter and asserted
identity-token injection): the target's adapters authenticate from their own
source config, so the tests drive the real sync paths end to end at the
manager level.
"""
from __future__ import annotations

import subprocess
import uuid

import pytest

from noeta.agent.config import Settings
from noeta.agent.services.knowledge.repo_sync import _inject_token
from noeta.agent.services.knowledge_sync import KnowledgeSyncManager, _redact
from noeta.agent.store.knowledge import KnowledgeSourceStore


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        shared_data_dir=str(tmp_path / "shared"),
    )


def _git(*args, cwd=None) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
         *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


@pytest.fixture
def bare_repo(tmp_path):
    """A local bare repository with one commit (README.md + docs/guide.md);
    returns (bare path, working tree path) so tests can push more commits."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "--initial-branch=main", str(work))
    (work / "README.md").write_text("# demo repo\n", encoding="utf-8")
    (work / "docs").mkdir()
    (work / "docs" / "guide.md").write_text("guide v1\n", encoding="utf-8")
    _git("add", "-A", cwd=str(work))
    _git("commit", "-q", "-m", "init", cwd=str(work))
    bare = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    return bare, work


def _source(store: KnowledgeSourceStore, source_type: str, config: dict,
            name: str = "demo-source") -> dict:
    sid = uuid.uuid4().hex
    store.create_source(
        sid, "space1", name, source_type, config=config, created_by="alice",
    )
    return store.get_source(sid)


# ------------------------------------------------------------ git_repo


def test_sync_git_repo_materializes_and_ready(tmp_path, bare_repo):
    bare, _work = bare_repo
    settings = _settings(tmp_path)
    store = KnowledgeSourceStore(settings.app_db_path)
    mgr = KnowledgeSyncManager(store, settings)
    source = _source(store, "git_repo", {"url": str(bare)})

    mgr._run_sync(source["id"], "alice")

    out = settings.knowledge_path / "space1" / source["id"]
    assert (out / "README.md").is_file()
    assert (out / "docs" / "guide.md").is_file()
    # INDEX.md is generated with a uniform shape across source types
    assert "Code repository index" in (out / "INDEX.md").read_text("utf-8")
    # Source status becomes ready; the name → id display symlink is maintained
    assert store.get_source(source["id"])["status"] == "ready"
    link = settings.knowledge_path / "space1" / "demo-source"
    assert link.is_symlink() and link.resolve().name == source["id"]


def test_sync_git_repo_incremental_fetch(tmp_path, bare_repo):
    """Second sync of an existing clone goes through fetch + reset and picks
    up new commits."""
    bare, work = bare_repo
    settings = _settings(tmp_path)
    store = KnowledgeSourceStore(settings.app_db_path)
    mgr = KnowledgeSyncManager(store, settings)
    source = _source(store, "git_repo", {"url": str(bare)})

    mgr._run_sync(source["id"], "alice")
    (work / "docs" / "guide.md").write_text("guide v2\n", encoding="utf-8")
    _git("commit", "-aqm", "update guide", cwd=str(work))
    _git("push", "-q", str(bare), "main", cwd=str(work))

    mgr._run_sync(source["id"], "alice")
    out = settings.knowledge_path / "space1" / source["id"]
    assert (out / "docs" / "guide.md").read_text("utf-8") == "guide v2\n"
    assert store.get_source(source["id"])["status"] == "ready"


def test_sync_git_repo_bad_url_marks_failed(tmp_path):
    """A clone failure lands as status=failed with a readable last_error
    (_run_sync's backstop), never an exception out of the thread."""
    settings = _settings(tmp_path)
    store = KnowledgeSourceStore(settings.app_db_path)
    mgr = KnowledgeSyncManager(store, settings)
    source = _source(store, "git_repo", {"url": str(tmp_path / "no-such-repo.git")})

    mgr._run_sync(source["id"], "alice")

    after = store.get_source(source["id"])
    assert after["status"] == "failed"
    assert after["last_error"]


# ------------------------------------------------------------ local_dir


def test_sync_local_dir_copies_tree_and_propagates_deletions(tmp_path):
    settings = _settings(tmp_path)
    store = KnowledgeSourceStore(settings.app_db_path)
    mgr = KnowledgeSyncManager(store, settings)

    origin = tmp_path / "origin-docs"
    (origin / "sub").mkdir(parents=True)
    (origin / "a.md").write_text("alpha\n", encoding="utf-8")
    (origin / "sub" / "b.md").write_text("beta\n", encoding="utf-8")
    source = _source(store, "local_dir", {"path": str(origin)}, name="local-docs")

    mgr._run_sync(source["id"], "alice")
    out = settings.knowledge_path / "space1" / source["id"]
    assert (out / "a.md").read_text("utf-8") == "alpha\n"
    assert (out / "sub" / "b.md").is_file()
    assert store.get_source(source["id"])["status"] == "ready"

    # Full re-materialization: a deletion in the original propagates
    (origin / "a.md").unlink()
    mgr._run_sync(source["id"], "alice")
    assert not (out / "a.md").exists()
    assert (out / "sub" / "b.md").is_file()


def test_sync_local_dir_missing_path_marks_failed(tmp_path):
    settings = _settings(tmp_path)
    store = KnowledgeSourceStore(settings.app_db_path)
    mgr = KnowledgeSyncManager(store, settings)
    source = _source(store, "local_dir", {"path": str(tmp_path / "nope")})

    mgr._run_sync(source["id"], "alice")

    after = store.get_source(source["id"])
    assert after["status"] == "failed"
    assert "does not exist" in after["last_error"]


# ------------------------------------------------------------ claim + redaction


def test_start_sync_conflict_raises(tmp_path):
    """A source already syncing cannot be claimed again (the API layer maps
    the "syncing" ValueError to 409)."""
    settings = _settings(tmp_path)
    store = KnowledgeSourceStore(settings.app_db_path)
    mgr = KnowledgeSyncManager(store, settings)
    source = _source(store, "local_dir", {"path": str(tmp_path)})
    assert store.try_set_syncing(source["id"]) is True

    with pytest.raises(ValueError, match="syncing"):
        mgr.start_sync(source["id"], triggered_by="alice")


def test_redact_scrubs_bearer_and_url_credentials():
    """The last_error redaction backstop wipes bearer tokens and
    URL-embedded credentials (last_error is member-readable)."""
    assert "abc.def" not in _redact("auth failed: Bearer abc.def-123")
    out = _redact("fatal: unable to access 'https://oauth2:sekrit@host/repo.git'")
    assert "sekrit" not in out and "://***@" in out


def test_inject_token_only_for_http_urls():
    """Token transport: injected as basic-auth userinfo for http(s) URLs
    only; other URLs (and no token) pass through unchanged."""
    assert (
        _inject_token("https://git.example.com/org/repo.git", "tok")
        == "https://oauth2:tok@git.example.com/org/repo.git"
    )
    assert _inject_token("git@example.com:org/repo.git", "tok") == "git@example.com:org/repo.git"
    assert _inject_token("https://git.example.com/x.git", None) == "https://git.example.com/x.git"
