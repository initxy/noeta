"""Pytest collection guard + global-dir hermeticity.

``tests/fixtures/`` holds data-only trees (including a repo whose test is
deliberately broken) that tests copy into a ``tmp_path``. Pytest discovery must
skip them, or the suite would fail on that deliberately broken test.

Skills and memory live under a fixed global directory
(``~/.noeta/{skills,memories}``) rather than the per-session workspace, so a
test that drives ``main`` (memory on) or activates a global skill would
otherwise read from and write to the developer's real home. The autouse fixture
below pins both roots into a per-test ``tmp_path``.
"""

from __future__ import annotations

import pytest


# Pytest honours this module-level name and skips the listed directories
# before traversing them.
collect_ignore_glob = ["fixtures/*"]


@pytest.fixture(autouse=True)
def _isolate_global_noeta_dirs(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect the global skill / memory dirs into a temp dir.

    Both roots are resolved at call time off module-level constants (the host
    leaves the field ``None`` and falls back to the constant), so patching the
    source bindings keeps the whole suite off the real ``~/.noeta``. A test that
    passes an explicit ``global_memory_dir`` / ``global_skills_dir`` still wins —
    the override short-circuits the fallback.
    """
    root = tmp_path_factory.mktemp("noeta-home")
    skills = root / "skills"
    memories = root / "memories"

    # The memory built-in's store module holds the ONE binding every consumer
    # resolves late, so patching it here covers the whole fallback chain.
    monkeypatch.setattr(
        "noeta.builtins.memory.impl.store.DEFAULT_GLOBAL_MEMORY_DIR",
        memories,
        raising=False,
    )
    # ``tests/_session_inputs`` reads the global skills tier off
    # ``tests._builtin_skills`` at call time. The import is guarded so a
    # collection that cannot load it still gets the memory redirect.
    try:
        import tests._builtin_skills as _agent_skills
    except Exception:
        _agent_skills = None  # type: ignore[assignment]
    if _agent_skills is not None:
        monkeypatch.setattr(
            _agent_skills, "DEFAULT_GLOBAL_SKILLS_DIR", skills, raising=False
        )


@pytest.fixture(autouse=True)
def _deterministic_subtask_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin sub-task group drain to **sequential** across the suite.

    An unset ``NOETA_SUBTASK_CONCURRENCY`` fans a one-turn ``spawn_subagent``
    batch (and a workflow ``parallel()`` group) onto the bounded executor, but
    almost every end-to-end test scripts its ``FakeLLMProvider`` with a
    POSITIONAL response list whose global cursor is order-dependent: concurrent
    group members would race that cursor and pick each other's answers. Pinning
    the escape valve to ``0`` makes the drain deterministic. A test that
    specifically exercises concurrency opts back in by ``setenv``-ing ``1`` or
    ``delenv``-ing the var, and drives its members through a content
    ``responder`` instead of the positional cursor.
    """
    monkeypatch.setenv("NOETA_SUBTASK_CONCURRENCY", "0")
