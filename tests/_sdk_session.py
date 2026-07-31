"""SdkHost / InteractionDriver assembly helpers for the engine-behaviour tests.

The suite drives the production SDK assembly — :class:`noeta.sdk.Client` /
:class:`SdkHost` / :class:`InteractionDriver` — rather than a test-only engine,
so a wiring bug cannot hide behind a parallel assembly path. There is
deliberately no second engine build here: :meth:`SdkHost._build_engine` does all
of it, and these helpers only put each knob on its real home:

* **per-agent behaviour flags** (``todo_write`` / ``ask_user_question`` /
  ``delegation`` / ``skill_invocation`` / ``memory`` / ``mcp`` / ``spawnable``)
  live in the ``plugins`` activation tuple + ``spawnable`` of the registered
  :class:`AgentSpec`, which the host reads through
  :func:`~noeta.agent.spec.agent_activates` as the single source of truth;
* **host knobs** (``require_approval_tools`` / ``write_mode`` / ``shell_mode`` /
  ``skill_tool_enforcement`` / ``allow_skill_scripts`` / ``repetition_*`` /
  ``hooks_pre_tool_use`` / ``mcp_server_resolver`` / ``budget`` / …) are fields
  on :class:`SdkHost`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Optional

from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import AgentSpec, agent_activates
from noeta.client.host import SdkHost
from noeta.core.wiring import wire_default_observers
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.runtime.governance import Budget
from noeta.presets import official_specs
from noeta.protocols.messages import LLMProvider
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

#: Recording alias: a stream whose ``TaskCreated.agent_name`` is ``"default"``
#: resolves to the canonical ``"main"``. SdkHost applies it via ``aliases``.
DEFAULT_ALIASES = {"default": "main"}


def default_coding_budget() -> Budget:
    """Budget for an interactive coding session.

    Sized so a long session never trips a cap while a runaway loop still gets
    bounded. ``max_spawned_subtasks=None`` disables that cap outright — ``0``
    would make BudgetGuard deny every spawn instead."""
    return Budget(
        max_iterations=200,
        max_tool_calls=400,
        max_cost_usd=None,
        max_spawned_subtasks=None,
    )


def coding_replay_budget(max_subtask_depth: Optional[int]) -> Budget:
    """The coding default plus a ``max_subtask_depth`` cap."""
    return dataclasses.replace(
        default_coding_budget(), max_subtask_depth=max_subtask_depth
    )


def resolve_write_mode(
    *, allow_write: bool, yes: bool, read_only: bool
) -> FsWriteMode:
    """Map operator-style flags → ``FsWriteMode``.

    ``read_only`` always wins; otherwise a write needs BOTH ``allow_write`` and
    ``yes``, so neither flag alone can turn a dry run into a real write."""
    if read_only:
        return FsWriteMode.DRY_RUN
    if allow_write and yes:
        return FsWriteMode.APPLY
    return FsWriteMode.DRY_RUN


def resolve_shell_mode(
    *, allow_shell: bool, shell_off: bool = False
) -> ShellMode:
    """Map operator-style flags → ``ShellMode``.

    Default ``ALLOWLIST``; ``allow_shell`` opts into arbitrary commands;
    ``shell_off`` removes ``shell_run`` entirely."""
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
    """The official preset specs, plus any ``extra``.

    Aliases belong on :attr:`SdkHost.aliases`, not in the registry — the
    registry keys on canonical names only."""
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


#: The identity feature names an activation tuple can carry. Used by
#: :func:`runner_main_spec` to rebuild ``AgentSpec.plugins`` from a flag dict
#: while preserving the preset's identity-inert packs (``fs`` / ``web`` …).
_FEATURE_FLAG_NAMES = (
    "todo_write",
    "ask_user_question",
    "delegation",
    "skill_invocation",
    "memory",
    "mcp",
    "browser",
)


def runner_main_spec(name: str = "main", **caps_overrides: Any) -> AgentSpec:
    """An official preset spec narrowed to a plain coding-session activation.

    Most engine-behaviour tests want a minimal, predictable tool set rather than
    the fully-capable ``main`` preset, so the ``plugins`` tuple is rebuilt with:

    * ``todo_write`` / ``ask_user_question`` / ``delegation`` → False
    * ``skill_invocation`` → True
    * ``memory`` / ``mcp`` / ``browser`` → whatever the preset activates
    * ``spawnable`` → ``()``

    Pass keyword overrides (``todo_write=True``, ``delegation=True,
    spawnable=("explore",)``, …) for a test that needs one of them back.
    """
    base = official_specs()[name]
    flags: dict[str, bool] = dict(
        todo_write=False,
        ask_user_question=False,
        delegation=False,
        skill_invocation=True,
        memory=agent_activates(base, "memory"),
        mcp=agent_activates(base, "mcp"),
        browser=agent_activates(base, "browser"),
    )
    spawnable: tuple[str, ...] = ()
    for key, value in caps_overrides.items():
        if key == "spawnable":
            spawnable = tuple(value)
        else:
            flags[key] = bool(value)
    # Preserve the preset's identity-inert packs (fs / web / …) and layer the
    # effective feature flags on top; membership in the tuple is the capability.
    carried = tuple(p for p in base.plugins if p not in _FEATURE_FLAG_NAMES)
    enabled = tuple(f for f in _FEATURE_FLAG_NAMES if flags[f])
    return dataclasses.replace(
        base, plugins=tuple(sorted(set(carried) | set(enabled))), spawnable=spawnable
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
        from tests._host_storage import open_sqlite_storage

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
    from tests._read_models.result import (
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
