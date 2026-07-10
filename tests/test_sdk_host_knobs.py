"""Tests for the new SdkHost knob fields (step one).

Covers:
1. aliases — alias resolution through _lookup_agent
2. require_approval_tools explicit override — bypasses permission_mode inference
3. budget explicit override — overrides the spec default
4. with all defaults, _build_engine is byte-for-byte equivalent to the pre-change behavior
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from noeta.agent.registry import AgentRegistry, UnknownAgentError
from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    Capabilities,
    ComponentRef,
    ToolRef,
)
from noeta.client.host import SdkHost
from noeta.execution.builder import (
    build_session_inputs,
    derive_compaction_config,
)
from noeta.guards.budget import Budget
from noeta.guards.permission import PermissionGuard
from noeta.protocols.messages import Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode


# ---------------------------------------------------------------------------
# Construction helpers — kept consistent with test_spawn_subagent_directory.py
# ---------------------------------------------------------------------------


def _stub_provider() -> FakeLLMProvider:
    from noeta.protocols.messages import LLMResponse, TextBlock

    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r1"},
            )
        ]
    )


def _make_host(
    registry: AgentRegistry, tmp_path: Path, **host_kwargs: Any
) -> SdkHost:
    """Build an SdkHost; host_kwargs override the default parameters."""
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    ws = tmp_path / "ws"
    ws.mkdir()
    kwargs: dict[str, Any] = dict(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=_stub_provider(),
        model="stub-model",
        workspace_dir=ws,
        registry=registry,
    )
    kwargs.update(host_kwargs)
    return SdkHost(**kwargs)


def _simple_main_spec(
    tools: tuple[ToolRef, ...] = ()
) -> AgentSpec:
    """Minimal main AgentSpec the Engine can build, mixing low- and high-risk tools to exercise permission tests."""
    if not tools:
        tools = (
            ToolRef(name="read_file", risk_level="low", version="1"),
            ToolRef(name="write", risk_level="high", version="1"),
            ToolRef(name="shell_run", risk_level="high", version="1"),
        )
    return AgentSpec(
        name="main",
        instructions="You are the main agent.",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=tools,
        capabilities=Capabilities(),
        default_budget=BudgetSpec(max_iterations=20),
        metadata={},
    )


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 1. aliases — _lookup_agent alias resolution
# ---------------------------------------------------------------------------


def test_lookup_aliases_default_to_main(tmp_path: Path) -> None:
    """With aliases={"default": "main"}, _lookup_agent("default") returns the main spec."""
    main_spec = _simple_main_spec()
    registry = AgentRegistry()
    registry.add(main_spec)

    host = _make_host(registry, tmp_path, aliases={"default": "main"})
    got = host._lookup_agent("default", task_id="t")
    # Same object, not a copy
    assert got is main_spec


def test_lookup_aliases_unknown_raises(tmp_path: Path) -> None:
    """A name in neither aliases nor the registry still raises UnknownAgentError."""
    registry = AgentRegistry()
    registry.add(_simple_main_spec())

    host = _make_host(registry, tmp_path, aliases={"default": "main"})
    with pytest.raises(UnknownAgentError) as exc_info:
        host._lookup_agent("ghost", task_id="t")
    # The exception carries task_id
    assert exc_info.value.task_id == "t"


def test_lookup_aliases_canonical_name_still_works(tmp_path: Path) -> None:
    """Even with aliases set, looking up by canonical name still works."""
    main_spec = _simple_main_spec()
    registry = AgentRegistry()
    registry.add(main_spec)

    host = _make_host(registry, tmp_path, aliases={"default": "main"})
    got = host._lookup_agent("main", task_id="t")
    assert got is main_spec


# ---------------------------------------------------------------------------
# 2. require_approval_tools explicit override
# ---------------------------------------------------------------------------


def test_require_approval_tools_explicit_empty(tmp_path: Path) -> None:
    """With explicit require_approval_tools=(), the PermissionGuard's require_approval
    must be empty, even when permission_mode="default" (which would otherwise gate
    every high-risk tool)."""
    main_spec = _simple_main_spec()
    registry = AgentRegistry()
    registry.add(main_spec)

    host = _make_host(
        registry,
        tmp_path,
        permission_mode="default",
        require_approval_tools=(),
    )
    engine = host._build_engine(
        main_spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )
    # The second guard in HookManager is the PermissionGuard (via _GuardEntry)
    perm_guard = engine._hooks._guards[1].guard
    assert isinstance(perm_guard, PermissionGuard)
    require_approve_set = perm_guard._policy.require_approval_tools
    # Explicit =() → no tool is gated
    assert require_approve_set == frozenset()


def test_require_approval_tools_none_follows_permission_mode(tmp_path: Path) -> None:
    """With require_approval_tools=None (the default), inference follows permission_mode="default":
    write stays in the static approval set; shell_run is instead gated by the per-call
    conditional_approval closure (allowlist
    hits pass silently, only unknown commands need approval), so it is not in the static set."""
    main_spec = _simple_main_spec()
    registry = AgentRegistry()
    registry.add(main_spec)

    host = _make_host(
        registry,
        tmp_path,
        permission_mode="default",
        # Default None, goes through _approval_set_for
    )
    engine = host._build_engine(
        main_spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )
    perm_guard = engine._hooks._guards[1].guard
    assert isinstance(perm_guard, PermissionGuard)
    require_approve_set = perm_guard._policy.require_approval_tools
    # Low-risk tools are out; the high-risk write stays in the static set
    assert "read_file" not in require_approve_set
    assert "write" in require_approve_set
    # shell_run moves to the per-call closure, no longer in the static set
    assert "shell_run" not in require_approve_set
    pred = perm_guard._policy.conditional_approval
    assert pred is not None
    # Unknown command → needs approval; built-in allowlist hit → passes; non-shell tools unaffected
    assert pred("shell_run", {"command": "rm -rf /"}) is True
    assert pred("shell_run", {"command": "git status"}) is False
    assert pred("write", {"path": "x"}) is False


# ---------------------------------------------------------------------------
# 3. budget override
# ---------------------------------------------------------------------------


def test_budget_override_max_iterations(tmp_path: Path) -> None:
    """SdkHost(budget=Budget(max_iterations=7)) → the built BudgetGuard._budget.max_iterations == 7."""
    main_spec = _simple_main_spec()
    registry = AgentRegistry()
    registry.add(main_spec)

    host = _make_host(
        registry,
        tmp_path,
        budget=Budget(max_iterations=7),
    )
    engine = host._build_engine(
        main_spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )
    # The first guard in HookManager is the BudgetGuard (via _GuardEntry)
    budget_guard = engine._hooks._guards[0].guard
    assert budget_guard._budget.max_iterations == 7


def test_budget_none_falls_back_to_spec_default(tmp_path: Path) -> None:
    """With budget=None (the default), inference uses spec.default_budget."""
    main_spec = _simple_main_spec()
    registry = AgentRegistry()
    registry.add(main_spec)

    host = _make_host(registry, tmp_path)  # budget=None default
    engine = host._build_engine(
        main_spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )
    budget_guard = engine._hooks._guards[0].guard
    # spec.default_budget.max_iterations = 20 → Budget max_iterations=20
    assert budget_guard._budget.max_iterations == 20


# ---------------------------------------------------------------------------
# 4. all-default byte equivalence — calling build_session_inputs directly vs SdkHost defaults
# ---------------------------------------------------------------------------


def test_default_host_byte_equal_to_direct_builder(tmp_path: Path) -> None:
    """The composer provider_tool_schemas built by a default SdkHost must match those from
    calling build_session_inputs directly (with the pre-change SdkHost parameter set). This
    is the core "zero byte change" invariant."""
    ws = tmp_path / "ws2"
    ws.mkdir()
    main_spec = _simple_main_spec()
    spec_tool_names = frozenset(r.name for r in main_spec.tools)

    # Build the engine with SdkHost defaults
    registry = AgentRegistry()
    registry.add(main_spec)
    host = _make_host(registry, tmp_path)
    engine_host = host._build_engine(
        main_spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    # Call build_session_inputs directly with the pre-change host parameter set
    content_store = InMemoryContentStore()
    from noeta.client.host import _approval_set_for

    direct_inputs = build_session_inputs(
        workspace_dir=host.workspace_dir,
        system_prompt=main_spec.instructions,
        allowed_tools=spec_tool_names,
        content_store=content_store,
        model="stub-model",
        compaction=derive_compaction_config("stub-model"),
        budget=host._budget_for(main_spec.default_budget),
        allowed_subtask_agents=frozenset(),
        # New fields, all using build_session_inputs defaults
        max_steps=20,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.ALLOWLIST,
        skills_dir=None,
        require_approval_tools=_approval_set_for("default", main_spec.tools),
        skill_tool_enforcement="off",
        delegation_enabled=False,
        allow_skill_scripts=False,
        todo_write_enabled=False,
        ask_user_question_enabled=False,
        skill_invocation_enabled=False,
        hooks_pre_tool_use=(),
        repetition_threshold=0,
        repetition_action="require_approval",
        repetition_window=8,
        subtask_agent_directory=(),
    )

    # The serialized provider_tool_schemas on both sides must be identical (no byte drift)
    view_host = _canonical(engine_host._composer._render_provider_tool_schemas())
    view_direct = _canonical(direct_inputs.composer._render_provider_tool_schemas())
    assert view_host == view_direct, (
        f"byte drift between default SdkHost and direct builder:\n"
        f"  host  ={view_host}\n"
        f"  direct={view_direct}"
    )
    # The guard count must also match (BudgetGuard + PermissionGuard = 2)
    assert len(engine_host._hooks._guards) == len(direct_inputs.hooks._guards)


# ---------------------------------------------------------------------------
# 5. plan preset aligned with Claude Code: read-only, never writes files
# ---------------------------------------------------------------------------


def test_sdk_host_plan_pack_has_no_write(tmp_path: Path) -> None:
    """CC alignment: plan is now fully read-only — its engine pack carries NO
    write family at all (the old restricted plans/*.md write was dropped, plan
    returns the plan as its message). The generic write_path_globs injection
    still lives in the host for custom agents, but no official preset uses it.
    This is the production server path (SdkHost is the resolver)."""
    from noeta.presets import official_specs

    specs = official_specs()
    plan_spec = specs["plan"]
    registry = AgentRegistry()
    registry.add(plan_spec)
    host = _make_host(registry, tmp_path)
    engine = host._build_engine(
        plan_spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=plan_spec.capabilities.ask_user_question,
        policy_wrapper=None,
    )
    # The whole write family is physically absent from plan's pack.
    for absent in ("write", "edit", "apply_patch"):
        assert absent not in engine._tools
    # Read-only scout tools (incl. read-only shell) are present.
    for present in ("read", "glob", "grep", "shell_run"):
        assert present in engine._tools


def test_sdk_host_main_write_is_unrestricted(tmp_path: Path) -> None:
    """A spec without write_path_globs metadata (main) keeps the unrestricted
    write — the injection is plan-specific, no behaviour change elsewhere."""
    from noeta.presets import official_specs

    specs = official_specs()
    main_spec = specs["main"]
    registry = AgentRegistry()
    registry.add(main_spec)
    host = _make_host(registry, tmp_path)
    engine = host._build_engine(
        main_spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )
    assert engine._tools["write"].allowed_path_globs == ()  # type: ignore[attr-defined]
