"""_knowledge_mounts_for_space: knowledge is always mounted per source (the
mount point is the source name).

A whole-directory mount would expose the materialization id directories
inside the container; after the agent searches with rg/find (which does not
follow the name symlinks) it would cite the id paths and the citations UI
would show bare UUIDs. With no selection configured, every ready source is
mounted; with a selection configured, only the chosen subset; None is
returned only when the stores are not attached (the provider falls back to
mounting the whole directory).

Tests AgentService._knowledge_mounts_for_space directly (no startup, no
sandbox connection).
"""
from __future__ import annotations

import pytest

from noeta.agent.config import Settings
from noeta.agent.host.service import AgentService
from noeta.agent.store.sessions import SessionStore


class _FakeConfigStore:
    def __init__(self, selected):
        self._selected = selected

    def get(self, space_id):
        return {"knowledge_sources": self._selected}


class _FakeKnowledgeStore:
    def __init__(self, sources):
        self._sources = sources

    def list_sources(self, space_id):
        return self._sources


@pytest.fixture
def svc_env(tmp_path):
    settings = Settings(
        llm_provider="mock",
        data_dir=str(tmp_path / "data"),
        shared_data_dir=str(tmp_path / "shared"),
    )
    store = SessionStore(tmp_path / "app.db")
    service = AgentService(settings, store)
    yield settings, service
    store.close()


def _materialize(settings, space_id, source_id):
    d = settings.knowledge_path / space_id / source_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_unselected_mounts_all_ready_sources_by_name(svc_env):
    """No selection configured: every ready source is mounted by name;
    non-ready / unmaterialized ones are excluded."""
    settings, service = svc_env
    _materialize(settings, "sp1", "src1")
    _materialize(settings, "sp1", "src2")
    service.attach_agent_config_store(_FakeConfigStore(selected=None))
    service.attach_knowledge_store(
        _FakeKnowledgeStore([
            {"id": "src1", "name": "my-docs", "status": "ready"},
            {"id": "src2", "name": "tracking-sdk", "status": "ready"},
            {"id": "src3", "name": "still-syncing", "status": "syncing"},
            {"id": "src4", "name": "unmaterialized", "status": "ready"},
        ])
    )
    mounts = service._knowledge_mounts_for_space("sp1")
    assert mounts == [
        ("my-docs", str(settings.knowledge_path / "sp1" / "src1")),
        ("tracking-sdk", str(settings.knowledge_path / "sp1" / "src2")),
    ]


def test_selected_subset_filters(svc_env):
    """A selection is configured: only the chosen sources are mounted;
    [] = nothing participates."""
    settings, service = svc_env
    _materialize(settings, "sp1", "src1")
    _materialize(settings, "sp1", "src2")
    sources = [
        {"id": "src1", "name": "my-docs", "status": "ready"},
        {"id": "src2", "name": "tracking-sdk", "status": "ready"},
    ]
    service.attach_knowledge_store(_FakeKnowledgeStore(sources))

    service.attach_agent_config_store(_FakeConfigStore(selected=["src2"]))
    assert service._knowledge_mounts_for_space("sp1") == [
        ("tracking-sdk", str(settings.knowledge_path / "sp1" / "src2")),
    ]

    service.attach_agent_config_store(_FakeConfigStore(selected=[]))
    assert service._knowledge_mounts_for_space("sp1") == []


def test_stores_missing_returns_none(svc_env):
    """Stores not attached: None (the provider falls back to mounting the
    whole directory)."""
    _, service = svc_env
    assert service._knowledge_mounts_for_space("sp1") is None


def _materialize_derived(settings, space_id):
    d = settings.knowledge_path / space_id / "_derived"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_derived_mounted_when_not_narrowed(svc_env):
    """The derived layer sits beside the source directories and is not a row
    in knowledge_sources, so the per-source loop cannot reach it: when the
    selection is not narrowed it must be mounted extra, otherwise the skill
    contract's knowledge/_derived/ would never exist."""
    settings, service = svc_env
    _materialize(settings, "sp1", "src1")
    derived = _materialize_derived(settings, "sp1")
    sources = [{"id": "src1", "name": "tracking-events", "status": "ready"}]
    service.attach_knowledge_store(_FakeKnowledgeStore(sources))

    # No selection configured
    service.attach_agent_config_store(_FakeConfigStore(selected=None))
    assert service._knowledge_mounts_for_space("sp1") == [
        ("tracking-events", str(settings.knowledge_path / "sp1" / "src1")),
        ("_derived", str(derived)),
    ]

    # A selection covering all ready sources = not narrowed
    service.attach_agent_config_store(_FakeConfigStore(selected=["src1"]))
    assert service._knowledge_mounts_for_space("sp1")[-1] == ("_derived", str(derived))


def test_derived_skipped_when_selection_narrowed(svc_env):
    """Not mounted when the selection is narrowed: the unified view is a
    cross-source join; mounting it would leak deselected sources' content
    back into the container."""
    settings, service = svc_env
    _materialize(settings, "sp1", "src1")
    _materialize(settings, "sp1", "src2")
    _materialize_derived(settings, "sp1")
    service.attach_knowledge_store(_FakeKnowledgeStore([
        {"id": "src1", "name": "tracking-events", "status": "ready"},
        {"id": "src2", "name": "code", "status": "ready"},
    ]))
    service.attach_agent_config_store(_FakeConfigStore(selected=["src1"]))
    assert service._knowledge_mounts_for_space("sp1") == [
        ("tracking-events", str(settings.knowledge_path / "sp1" / "src1")),
    ]


def test_derived_skipped_when_absent_or_no_sources(svc_env):
    """Derived layer not generated → not mounted (the skill degrades to
    grep); nothing participates → not even the derived layer is mounted."""
    settings, service = svc_env
    _materialize(settings, "sp1", "src1")
    sources = [{"id": "src1", "name": "tracking-events", "status": "ready"}]
    service.attach_knowledge_store(_FakeKnowledgeStore(sources))

    # The _derived directory does not exist
    service.attach_agent_config_store(_FakeConfigStore(selected=None))
    assert service._knowledge_mounts_for_space("sp1") == [
        ("tracking-events", str(settings.knowledge_path / "sp1" / "src1")),
    ]

    # selected=[] nothing participates: a lone knowledge/ must not appear just
    # because the derived layer exists
    _materialize_derived(settings, "sp1")
    service.attach_agent_config_store(_FakeConfigStore(selected=[]))
    assert service._knowledge_mounts_for_space("sp1") == []
