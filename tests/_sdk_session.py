"""SdkHost / InteractionDriver assembly helpers for engine-behaviour tests.

These replace the deleted ``noeta.agent.host.session.AgentSessionRunner`` /
``noeta.agent.api`` fixture.
The engine-behaviour suite used to drive the now-deleted product runner; it now
drives the **production** SDK assembly directly — the same path the shipping
``noeta.agent`` backend uses via :class:`noeta.sdk.Client` / :class:`SdkHost` /
:class:`InteractionDriver`.

There is no second engine-assembly here: :class:`SdkHost._build_engine` does all
the wiring. These helpers only translate the old ``CodeSessionConfig`` knobs onto
their real homes:

* **per-agent behaviour flags** (``todo_write`` / ``ask_user_question`` /
  ``delegation`` / ``skill_invocation`` / ``memory`` / ``mcp`` / ``spawnable``)
  → :class:`~noeta.agent.spec.Capabilities` on the registered :class:`AgentSpec`
  (the SDK host treats ``spec.capabilities`` as the source of truth);
* **host knobs** (``require_approval_tools`` / ``write_mode`` / ``shell_mode`` /
  ``skill_tool_enforcement`` / ``allow_skill_scripts`` / ``repetition_*`` /
  ``hooks_pre_tool_use`` / ``mcp_server_resolver`` / ``budget`` / …) → fields on
  :class:`SdkHost`.

``runner_main_spec`` seeds the capability defaults to the OLD ``CodeSessionConfig``
*effective* flags (todo_write / ask_user / delegation OFF, skill_invocation ON,
memory follows the preset) rather than the fully-capable official ``main`` spec,
so a migrated ``CodeSessionConfig(agent="main")`` test keeps the same tool set.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Optional

from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import AgentSpec
from noeta.client.host import SdkHost
from noeta.core.wiring import wire_default_observers
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.guards.budget import Budget
from noeta.presets import official_specs
from noeta.protocols.messages import LLMProvider
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.fs import FsWriteMode, ShellMode

#: Legacy-recording alias the runner used (``TaskCreated.agent_name="default"``
#: maps to the canonical ``"main"``); the SdkHost resolves it via ``aliases``.
DEFAULT_ALIASES = {"default": "main"}


def default_coding_budget() -> Budget:
    """The v1 interactive-coding-session budget (inlined from the deleted
    ``noeta.agent.host.session``). Sized so a long session does not trip a cap
    while still bounding a runaway loop; ``max_spawned_subtasks=None`` (not 0)
    keeps the BudgetGuard from denying the first tool call."""
    return Budget(
        max_iterations=200,
        max_tool_calls=400,
        max_cost_usd=None,
        max_spawned_subtasks=None,
    )


def coding_replay_budget(max_subtask_depth: Optional[int]) -> Budget:
    """The coding default plus a ``max_subtask_depth`` cap (inlined from the
    deleted ``noeta.agent.host.session``)."""
    return dataclasses.replace(
        default_coding_budget(), max_subtask_depth=max_subtask_depth
    )


def resolve_write_mode(
    *, allow_write: bool, yes: bool, read_only: bool
) -> FsWriteMode:
    """Map CLI-style flags → ``FsWriteMode`` (inlined from the deleted
    ``noeta.agent.host.session``). ``--read-only`` always wins; otherwise a write
    requires BOTH ``--allow-write`` and ``--yes``."""
    if read_only:
        return FsWriteMode.DRY_RUN
    if allow_write and yes:
        return FsWriteMode.APPLY
    return FsWriteMode.DRY_RUN


def resolve_shell_mode(
    *, allow_shell: bool, shell_off: bool = False
) -> ShellMode:
    """Map CLI-style flags → ``ShellMode`` (inlined from the deleted
    ``noeta.agent.host.session``). Default ``ALLOWLIST``; ``--allow-shell`` opts
    into arbitrary commands; ``shell_off`` removes ``shell_run`` entirely."""
    if shell_off:
        return ShellMode.OFF
    if allow_shell:
        return ShellMode.ARBITRARY
    return ShellMode.ALLOWLIST


def make_registry(*specs: AgentSpec) -> AgentRegistry:
    """An :class:`AgentRegistry` containing exactly ``specs`` (in given order)."""
    registry = AgentRegistry()
    for spec in specs:
        registry.add(spec)
    return registry


def official_registry(*extra: AgentSpec) -> AgentRegistry:
    """The four official preset specs (+ any ``extra``), mirroring the deleted
    ``official_agent_registry()``. Aliases are applied at :attr:`SdkHost.aliases`,
    not here (same split as the product)."""
    registry = AgentRegistry()
    specs = official_specs()
    for name in sorted(specs):
        registry.add(specs[name])
    for spec in extra:
        registry.add(spec)
    return registry


def preset_spec(name: str) -> AgentSpec:
    """The official preset :class:`AgentSpec` for ``name`` (fully capable)."""
    return official_specs()[name]


def runner_main_spec(name: str = "main", **caps_overrides: Any) -> AgentSpec:
    """An official spec whose capabilities default to the OLD ``CodeSessionConfig``
    *effective* flags, overridable per test.

    The runner read its own ``CodeSessionConfig`` fields (not ``spec.capabilities``)
    so a ``CodeSessionConfig(agent="main")`` ran with todo_write / ask_user /
    delegation OFF even though the ``main`` preset enables them. The SDK host reads
    ``spec.capabilities``, so to keep the same engine we replace the preset caps
    with the runner defaults:

    * ``todo_write`` / ``ask_user_question`` / ``delegation`` → False
    * ``skill_invocation`` → True (the ``CodeSessionConfig`` default)
    * ``memory`` / ``mcp`` → follow the preset (``memory_enabled=None`` followed
      the spec; ``mcp`` was inert without ``mcp_servers``)
    * ``spawnable`` → () (delegation off by default)

    Pass keyword overrides (e.g. ``todo_write=True``, ``delegation=True,
    spawnable=("explore",)``) to mirror a test that flipped a flag.
    """
    base = official_specs()[name]
    caps = base.capabilities
    defaults: dict[str, Any] = dict(
        todo_write=False,
        ask_user_question=False,
        delegation=False,
        skill_invocation=True,
        memory=caps.memory,
        mcp=caps.mcp,
        spawnable=(),
    )
    defaults.update(caps_overrides)
    return dataclasses.replace(
        base, capabilities=dataclasses.replace(caps, **defaults)
    )


def make_host(
    registry: AgentRegistry,
    *,
    workspace_dir: Path,
    provider: LLMProvider,
    model: str = "gpt-test",
    multi_turn: bool = True,
    aliases: Optional[dict[str, str]] = None,
    sqlite_path: Optional[str] = None,
    **knobs: Any,
) -> SdkHost:
    """Build a production :class:`SdkHost` over a fresh L0 triple.

    ``multi_turn=True`` wires :func:`multi_turn_policy_wrapper` so a normally
    finishing turn suspends on the next-goal handle (the interactive ``Client``
    shape); ``False`` lets a turn reach a terminal ``TaskCompleted`` (the
    one-shot ``query`` shape). ``sqlite_path`` swaps the default in-memory triple
    for a durable sqlite one (via the retained ``noeta.agent.host.storage``).
    Every other ``**knobs`` keyword is forwarded verbatim to :class:`SdkHost`
    (``require_approval_tools`` / ``write_mode`` / ``shell_mode`` / ``budget`` /
    ``mcp_*`` / …).
    """
    if sqlite_path is not None:
        from noeta.agent.host.storage import open_sqlite_storage

        (event_log, content_store, dispatcher), _close = open_sqlite_storage(
            sqlite_path
        )
    else:
        dispatcher = InMemoryDispatcher()
        event_log = InMemoryEventLog(lease_validator=dispatcher)
        content_store = InMemoryContentStore()
    # The ChildLifecycleObserver that enqueues spawned subtasks rides the
    # default observer set — the ``Client`` wires it around the host, so a bare
    # SdkHost test must wire it too or subtask drains find no ready child. The
    # subscription rides the per-test in-memory event log (GC'd with it); tests
    # never need to unsubscribe.
    wire_default_observers(event_log, dispatcher)
    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=provider,
        model=model,
        workspace_dir=workspace_dir,
        registry=registry,
        aliases=aliases if aliases is not None else DEFAULT_ALIASES,
        policy_wrapper=(multi_turn_policy_wrapper if multi_turn else None),
        **knobs,
    )


def session_result(
    host: SdkHost, out: Any, *, after_seq: Optional[int] = None
) -> Any:
    """Project the deleted ``AgentSessionRunner._build_result`` shape off the
    durable EventLog of a ``driver.start`` / ``driver.send_goal`` outcome.

    Reuses the surviving ``noeta.agent.read_models.result`` helpers (the same
    read-model the noeta-agent backend uses), so a migrated test can assert
    ``files_changed`` / ``selected_skills`` / ``last_shell`` / ``failed_edits``
    exactly as it did against the runner's ``CodeSessionResult``.

    ``after_seq`` windows the projection to events strictly past that seq — the
    per-turn slice the old runner returned from each ``execute`` / ``resume``
    (pass the last seq seen before the turn).
    """
    from noeta.agent.read_models.result import (
        CodeSessionResult,
        _collect_failed_edits,
        _collect_files_changed,
        _last_selected_skills,
        _last_shell_result,
    )

    events = host.event_log.read(out.task_id, after_seq=after_seq)
    cs = host.content_store
    return CodeSessionResult(
        task_id=out.task_id,
        status=out.status,
        events=len(events),
        selected_skills=_last_selected_skills(events, cs),
        files_changed=_collect_files_changed(events, cs),
        failed_edits=_collect_failed_edits(events, cs),
        last_shell=_last_shell_result(events, cs),
    )


def make_driver(host: SdkHost, **kwargs: Any) -> InteractionDriver:
    """An :class:`InteractionDriver` over ``host`` (defaults match the ``Client``:
    ``default_model=None`` falls back to ``host.model`` and avoids the
    selector allowlist when no per-turn selector is passed)."""
    kwargs.setdefault("default_model", None)
    return InteractionDriver(host, **kwargs)
