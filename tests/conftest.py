"""Pytest collection guard + global-dir hermeticity.

``tests/fixtures/`` carries data-only tree(s) (e.g. the Phase 4 I6
``bugfix_repo`` with its known-failing test). They are read by the
coding-Agent tests after a `shutil.copytree` into a `tmp_path`; they
must NOT be picked up by pytest discovery here, or the parent suite
would fail on the deliberately broken test.

The workspace/session-path refactor moved skills/memory off the per-session workspace onto a
**fixed global directory** (``~/.noeta/{skills,memories}``). A test that drives
``main`` (memory on) or activates a global skill would otherwise touch (and
recall from) the developer's actual ``~/.noeta``. The autouse fixture below pins
both global dirs to a per-test ``tmp_path`` (patching the **source** module
constants the SDK builder + the ``tests/_session_inputs`` replay helper read)
so the whole suite stays hermetic; a test that explicitly passes its own
``global_memory_dir`` / ``global_skills_dir`` still wins (the override is
honoured downstream).
"""

from __future__ import annotations

import pytest


# Pytest honours this module-level name and skips the listed
# directories before traversing.
collect_ignore_glob = ["fixtures/*"]


@pytest.fixture(autouse=True)
def _isolate_global_noeta_dirs(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect the global skill / memory dirs into a temp dir.

    The SDK builder + the ``tests/_session_inputs`` replay helper resolve their
    global skill / memory roots at call time from these module-level constants
    (the host leaves the fields ``None`` and falls back to the constant), so
    patching the source bindings here keeps the whole suite off the real
    ``~/.noeta``. A test that passes an explicit ``global_memory_dir`` /
    ``global_skills_dir`` still wins (the override short-circuits the fallback).
    """
    root = tmp_path_factory.mktemp("noeta-home")
    skills = root / "skills"
    memories = root / "memories"

    # SDK builder reads the memory constant at call time on the None-fallback.
    monkeypatch.setattr(
        "noeta.execution.memory.DEFAULT_GLOBAL_MEMORY_DIR", memories, raising=False
    )
    monkeypatch.setattr(
        "noeta.execution.builder.DEFAULT_GLOBAL_MEMORY_DIR", memories, raising=False
    )
    # The global skills tier is a noeta-agent product concept (``noeta.agent.skills``
    # owns ``DEFAULT_GLOBAL_SKILLS_DIR``); ``tests/_session_inputs`` reads it at
    # call time. Guard the import for a pure-SDK collection run where noeta-agent
    # is absent.
    try:
        import tests._builtin_skills as _agent_skills
    except Exception:  # noeta-agent not importable
        _agent_skills = None  # type: ignore[assignment]
    if _agent_skills is not None:
        monkeypatch.setattr(
            _agent_skills, "DEFAULT_GLOBAL_SKILLS_DIR", skills, raising=False
        )


@pytest.fixture(autouse=True)
def _deterministic_subtask_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin sub-task group drain to **sequential** across the suite.

    The PRODUCTION default was flipped to concurrent: an unset ``NOETA_SUBTASK_CONCURRENCY``
    now fans a one-turn ``spawn_subagent`` batch (and the workflow
    ``parallel()`` group) onto the bounded executor. But almost every
    end-to-end test scripts its ``FakeLLMProvider`` with a POSITIONAL response
    list whose global cursor is order-dependent — and therefore unusable under
    concurrent drain (see the ``FakeLLMProvider`` docstring): a concurrent
    group's members would race the cursor and pick each other's answers.

    So the suite pins the escape valve to ``0`` (sequential, deterministic),
    which is exactly the pre-flip global default ⇒ zero drift for existing
    tests. A test that specifically exercises concurrency opts back in by
    ``setenv``-ing ``1`` or ``delenv``-ing the var, and drives its members
    through a content ``responder`` instead of the positional cursor.
    """
    monkeypatch.setenv("NOETA_SUBTASK_CONCURRENCY", "0")
