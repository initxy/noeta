"""The ``noeta`` command ranks the skill listing by use across the store's
tasks by default, as Claude Code ranks it across its sessions."""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.cli import home_dir
from noeta.cli.config import from_settings, load_settings
from noeta.plugins.skills import UsageRank


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(home_dir, "HOME", home)
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n')
    return home


async def test_every_agent_ranks_skills_by_use_in_the_store(home: Path,
                                                            tmp_path: Path) -> None:
    app = from_settings(load_settings(), cwd=str(tmp_path))
    try:
        ranks = [e.rank for a in app.runtime.agents.values()  # type: ignore[attr-defined]
                 for e in a.extensions if e.name == "skills"]
        assert ranks and all(isinstance(r, UsageRank) for r in ranks)
        # Over this command's own store (its tasks are the sessions).
        assert all(r.client.store is app.store for r in ranks)  # type: ignore[attr-defined]
    finally:
        await app.store.close()
