"""The host side of the skill-menu budget: the per-task rank resolver.

``HostConfig.skill_menu_rank_resolver`` is the same tenancy seam as
``memory_root_resolver`` — the SDK hands over task ids, the product maps them
to tenants. The resolved rank reaches the ``skills`` pack as ``menu_rank``;
the Engine is built per turn, so two tenants never share a roster; the
budget itself is derived by the host from the bound model's catalog window.
"""

from __future__ import annotations


import pytest
from pathlib import Path
from typing import Any, Optional

from noeta.client.host import (
    SKILL_MENU_BUDGET_FRACTION,
    skill_menu_budget_tokens,
)
from noeta.client.parts import derive_compaction_config
from noeta.core.fold import fold
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.builtins.skills.impl import SKILL_TOOL

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec
from tests._skill_fixtures import write_skill


def _skill_host(tmp_path: Path, **knobs: Any):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    # Three long summaries; the override budget below fits exactly one.
    for name in ("aaa", "bbb", "ccc"):
        write_skill(ws, name, name[0] * 400)
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=[]),
        model="stub-model",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        **knobs,
    )
    return host, make_driver(host)


def _roster(engine: Any) -> dict[str, str]:
    for schema in engine._composer._control_action_schemas:
        if isinstance(schema, dict) and schema["function"]["name"] == SKILL_TOOL:
            desc = schema["function"]["parameters"]["properties"]["skill"]["description"]
            out: dict[str, str] = {}
            for entry in desc.split("Available: ", 1)[1].split("; "):
                name, _, summary = entry.partition(" — ")
                out[name] = summary
            return out
    raise AssertionError("skill schema missing")


def test_bare_host_derives_the_budget_and_no_rank(tmp_path: Path) -> None:
    """Without the seam the skills bag carries the model-derived budget and no
    ``menu_rank`` key — a single-tenant host builds the bag it always did,
    plus the one number."""
    host, _ = _skill_host(tmp_path)
    skills = host._plugin_config(shell_mode=ShellMode.OFF)["skills"]
    window = derive_compaction_config("stub-model").context_window
    assert skills["menu_budget_tokens"] == int(window * SKILL_MENU_BUDGET_FRACTION)
    assert skills["menu_budget_tokens"] == skill_menu_budget_tokens("stub-model")
    assert "menu_rank" not in skills
    # The override channel replaces the derived number per key.
    host2, _ = _skill_host(
        tmp_path, plugin_config_overrides={"skills": {"menu_budget_tokens": 77}}
    )
    assert host2._plugin_config(shell_mode=ShellMode.OFF)["skills"]["menu_budget_tokens"] == 77


def test_rank_resolver_reaches_the_roster(tmp_path: Path) -> None:
    """Two tasks equal on every binding but ranked differently compose
    different rosters; a task composes the same roster on every build."""
    mapping: dict[str, dict[str, float]] = {}
    host, driver = _skill_host(
        tmp_path,
        skill_menu_rank_resolver=mapping.get,
        plugin_config_overrides={"skills": {"menu_budget_tokens": 130}},
    )
    # The product pattern: seed (task id minted), bind the tenant's rank,
    # then resolve — the driving Engine composes with that rank.
    seeded_a = driver.seed_start(goal="g", agent="main")
    mapping[seeded_a.task_id] = {"aaa": 1.0}
    seeded_b = driver.seed_start(goal="g", agent="main")
    mapping[seeded_b.task_id] = {"ccc": 1.0}

    task_a = fold(host.event_log, host.content_store, seeded_a.task_id)
    task_b = fold(host.event_log, host.content_store, seeded_b.task_id)
    engine_a = host.resolve_engine(task_a)
    engine_b = host.resolve_engine(task_b)

    assert _roster(engine_a) == {"aaa": "a" * 400, "bbb": "", "ccc": ""}
    assert _roster(engine_b) == {"aaa": "", "bbb": "", "ccc": "c" * 400}
    assert _roster(host.resolve_engine(task_a)) == _roster(engine_a)


def test_host_config_forwards_the_resolver_to_the_host(tmp_path: Path) -> None:
    """``HostConfig.skill_menu_rank_resolver`` rides ``Client`` onto ``SdkHost``
    like ``memory_root_resolver``; a bare ``HostConfig`` carries none."""
    from noeta.sdk import Client, HostConfig, Options

    def resolver(task_id: str) -> Optional[dict[str, float]]:
        return {"aaa": 1.0}

    options = Options(
        system_prompt="you finish immediately",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )
    client = Client(
        options,
        provider=FakeLLMProvider(responses=[]),
        workspace_dir=tmp_path,
        host_config=HostConfig(skill_menu_rank_resolver=resolver),
    )
    assert client._host.skill_menu_rank_resolver is resolver
    assert HostConfig().skill_menu_rank_resolver is None


def test_rank_is_resolved_once_per_task_in_this_process(tmp_path: Path) -> None:
    """The first non-empty answer sticks for the task's life: a resolver whose
    score decays with the clock (``rank_skills_by_usage(now=...)``) cannot
    rotate a running task's roster from one per-turn build to the next. A
    declining resolver is asked again; its later answer then sticks."""
    calls: list[str] = []
    tick = {"n": 0}
    answers: dict[str, Optional[dict[str, float]]] = {}

    def resolver(task_id: str) -> Optional[dict[str, float]]:
        calls.append(task_id)
        tick["n"] += 1
        answer = answers.get(task_id)
        if answer is None:
            return None
        # A different score on every call, like a fresh ``now``.
        return {name: score + tick["n"] for name, score in answer.items()}

    host, driver = _skill_host(
        tmp_path,
        skill_menu_rank_resolver=resolver,
        plugin_config_overrides={"skills": {"menu_budget_tokens": 130}},
    )
    seeded = driver.seed_start(goal="g", agent="main")
    answers[seeded.task_id] = {"ccc": 1.0}
    task = fold(host.event_log, host.content_store, seeded.task_id)
    engine = host.resolve_engine(task)
    # ``seed_start`` already resolved once (declined: no answer yet) and the
    # build above asked once more; from here on the memo answers.
    asked = calls.count(seeded.task_id)
    roster = _roster(engine)
    for _ in range(3):
        assert _roster(host.resolve_engine(task)) == roster
    assert roster == {"aaa": "", "bbb": "", "ccc": "c" * 400}
    assert calls.count(seeded.task_id) == asked

    # A task the resolver declines at first is asked again on the next
    # build, and the answer it then gives is the one that sticks.
    late = driver.seed_start(goal="g", agent="main")
    late_task = fold(host.event_log, host.content_store, late.task_id)
    unranked = host.resolve_engine(late_task)
    # Unranked: tier + priority + name order keeps the first name's summary.
    assert _roster(unranked) == {"aaa": "a" * 400, "bbb": "", "ccc": ""}
    answers[late.task_id] = {"aaa": 1.0}
    ranked = host.resolve_engine(late_task)
    assert _roster(ranked) == {"aaa": "a" * 400, "bbb": "", "ccc": ""}
    assert _roster(host.resolve_engine(late_task)) == _roster(ranked)


def test_static_menu_rank_and_a_resolver_together_are_refused(tmp_path: Path) -> None:
    """The static override would replace every resolved rank while the cache
    still partitioned by it — refused at construction, not silently."""
    with pytest.raises(ValueError, match="menu_rank"):
        _skill_host(
            tmp_path,
            skill_menu_rank_resolver=lambda tid: {"aaa": 1.0},
            plugin_config_overrides={"skills": {"menu_rank": {"aaa": 1.0}}},
        )
