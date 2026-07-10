"""Issue 02 — per-task agent→engine resolver + authoritative
``agent_name``.

Covers:

* :func:`noeta.agent.session.build_engine_for_agent` — the extracted factory:
  the agent's ``allowed_tools`` filter the tool pack, the agent's system
  prompt reaches the policy, and the Engine stays single-policy. A
  ``policy_wrapper`` wraps the produced policy.
* :func:`noeta.runtime.worker.resolve_engine` — the L2 seam: prefer
  ``rt.resolve_engine(task)`` when present, else fall back to ``rt.engine``.
* :class:`noeta.agent.resolver.SdkHost` — folds a Task's recorded
  ``agent_name`` → the matching Agent's Engine; unknown agent is a HARD error
  at resolve/lease time; ``unnamed`` only resolves with an explicit fallback.
* **Demo** — a general-purpose-created task is driven by the general-purpose
  Engine and an explore-created task by the explore Engine, both leased through
  the single ``run_leased_task`` primitive against one resident resolver host.
* The SDK host/driver writes the driving agent name (not ``"unnamed"``) on the
  root ``TaskCreated``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.presets import official_specs
from noeta.client import SdkHost
from noeta.agent.registry import UnknownAgentError
from noeta.execution.resolver import agent_name_of
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.policies.react import ReActPolicy
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    Usage,
)
from noeta.runtime.worker import resolve_engine, run_leased_task
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)

from tests._sdk_session import (
    make_driver,
    make_host,
    official_registry as official_agent_registry,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _EndTurnProvider:
    """One-turn deterministic provider: end the turn immediately.

    Keeps the demo focused on *which Agent drove* rather than on tool
    semantics — the recorded run's ``LLMRequest.system`` carries the
    driving Agent's system prompt, which the test asserts."""

    def __init__(self) -> None:
        self.systems: list[str] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.systems.append(_system_text(request))
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
        )


def _system_text(request: LLMRequest) -> str:
    """The rendered system-segment text of an ``LLMRequest`` (segment 0)."""
    system = request.system
    if system is None:
        return ""
    out: list[str] = []
    for block in system.content:
        text = getattr(block, "text", None)
        if text is not None:
            out.append(text)
    return "\n".join(out)


def _storage() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    return event_log, content_store, dispatcher


def build_engine_for_agent(
    agent_spec: Any,
    model: str,
    *,
    event_log: InMemoryEventLog,
    content_store: InMemoryContentStore,
    provider: Any,
    workspace_dir: Path,
    policy_wrapper: Any = None,
) -> Engine:
    """Local helper: build an Engine for ``agent_spec.name`` via SdkHost.

    Note: the ``agent_spec`` object is not used directly — the Engine is built
    from its canonical name ``agent_spec.name`` through
    ``SdkHost.resolve_engine_for_agent``. So passing ``official_specs()["main"]``
    vs ``["explore"]`` yields Engines with the matching tool surface."""
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=InMemoryDispatcher(),  # never used for engine creation
        provider=provider,
        model=model,
        workspace_dir=workspace_dir,
        registry=official_agent_registry(),
        aliases={"default": "main"},
        policy_wrapper=policy_wrapper,
        require_approval_tools=(),  # match legacy default
    )
    # Resolve the Engine by the agent's canonical name — this is what drives
    # per-agent tool filtering.
    return host.resolve_engine_for_agent(agent_spec.name, model=model)


# ---------------------------------------------------------------------------
# build_engine_for_agent
# ---------------------------------------------------------------------------


def test_build_engine_for_agent_filters_tools_to_allowlist(tmp_path: Path) -> None:
    """The agent's ``allowed_tools`` are the ONLY tools on the Engine —
    explore is provably write-free (no write / edit / apply_patch reach the
    pack). After the CC alignment it does carry read-only shell."""
    event_log, content_store, _ = _storage()
    engine = build_engine_for_agent(
        official_specs()["explore"],
        "test-model",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    tools = set(engine._tools)
    assert "read" in tools
    assert "glob" in tools
    assert "shell_run" in tools  # read-only shell, CC-aligned
    # Provably absent — the moat: a write-tempting goal cannot reach these.
    assert "write" not in tools
    assert "edit" not in tools
    assert "apply_patch" not in tools


def test_build_engine_for_agent_carries_agent_system_prompt(tmp_path: Path) -> None:
    """The agent's system prompt reaches the single ReActPolicy."""
    event_log, content_store, _ = _storage()
    engine = build_engine_for_agent(
        official_specs()["main"],
        "test-model",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    assert isinstance(engine, Engine)
    policy = engine._policy
    assert isinstance(policy, ReActPolicy)
    assert policy._system_prompt == official_specs()["main"].instructions


def test_build_engine_for_agent_distinct_agents_distinct_tool_sets(
    tmp_path: Path,
) -> None:
    """Different Agents build Engines with different tool surfaces — the
    selector is load-bearing."""
    event_log, content_store, _ = _storage()
    explorer = build_engine_for_agent(
        official_specs()["explore"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    general = build_engine_for_agent(
        official_specs()["general-purpose"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    assert set(explorer._tools) != set(general._tools)
    # general-purpose carries the write family; explore does not (the
    # load-bearing distinction after the CC alignment, since both now have shell).
    assert "edit" in set(general._tools)
    assert "edit" not in set(explorer._tools)


def test_build_engine_for_agent_applies_policy_wrapper(tmp_path: Path) -> None:
    """``policy_wrapper`` wraps the produced policy before the Engine sees it."""
    event_log, content_store, _ = _storage()
    sentinel = object()

    def wrap(_policy: Any) -> Any:
        return sentinel

    engine = build_engine_for_agent(
        official_specs()["main"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
        policy_wrapper=wrap,
    )
    assert engine._policy is sentinel


# ---------------------------------------------------------------------------
# resolve_engine (L2 seam)
# ---------------------------------------------------------------------------


class _FallbackRuntime:
    """Single-Engine runtime — no ``resolve_engine`` method."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.event_log = None
        self.content_store = None
        self.dispatcher = None


class _ResolvingRuntime(_FallbackRuntime):
    """Multi-Agent runtime — supplies a ``resolve_engine`` method."""

    def __init__(self, engine: Any, resolved: Any) -> None:
        super().__init__(engine)
        self._resolved = resolved
        self.seen: list[Any] = []

    def resolve_engine(self, task: Any) -> Any:
        self.seen.append(task)
        return self._resolved


def test_resolve_engine_falls_back_to_single_engine() -> None:
    """A runtime without ``resolve_engine`` keeps the single-Engine view."""
    eng = object()
    rt = _FallbackRuntime(eng)
    assert resolve_engine(rt, task=object()) is eng


def test_resolve_engine_prefers_runtime_method() -> None:
    """A runtime exposing ``resolve_engine(task)`` drives per-task."""
    single = object()
    per_task = object()
    rt = _ResolvingRuntime(single, per_task)
    task = object()
    assert resolve_engine(rt, task) is per_task
    assert rt.seen == [task]


# ---------------------------------------------------------------------------
# SdkHost — fold agent_name → Engine
# ---------------------------------------------------------------------------


def _resolver(
    event_log: InMemoryEventLog,
    content_store: InMemoryContentStore,
    dispatcher: InMemoryDispatcher,
    tmp_path: Path,
    *,
    provider: Any = None,
    unnamed_fallback: Any = None,
) -> SdkHost:
    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=provider or _EndTurnProvider(),
        model="test-model",
        workspace_dir=tmp_path,
        unnamed_fallback=unnamed_fallback,
        registry=official_agent_registry(),
        aliases={"default": "main"},
        require_approval_tools=(),
    )


def test_resolver_dispatches_on_recorded_agent_name(tmp_path: Path) -> None:
    """Each task resolves to ITS OWN Agent's Engine (general-purpose vs
    explore) by its recorded ``agent_name``."""
    event_log, content_store, dispatcher = _storage()
    seed = build_engine_for_agent(
        official_specs()["main"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    gp_task = seed.create_task(
        goal="do it", policy_name="react", agent_name="general-purpose"
    )
    exp_task = seed.create_task(
        goal="look into it", policy_name="react", agent_name="explore"
    )

    resolver = _resolver(event_log, content_store, dispatcher, tmp_path)
    gp_engine = resolver.resolve_engine(fold(event_log, content_store, gp_task.task_id))
    exp_engine = resolver.resolve_engine(
        fold(event_log, content_store, exp_task.task_id)
    )

    assert gp_engine is not exp_engine
    assert gp_engine._policy._system_prompt == official_specs()["general-purpose"].instructions
    assert exp_engine._policy._system_prompt == official_specs()["explore"].instructions
    assert "edit" in set(gp_engine._tools)
    assert "edit" not in set(exp_engine._tools)


def test_resolver_caches_one_engine_per_agent(tmp_path: Path) -> None:
    """Two tasks of the same Agent share one cached Engine."""
    event_log, content_store, dispatcher = _storage()
    seed = build_engine_for_agent(
        official_specs()["main"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    t1 = seed.create_task(goal="a", policy_name="react", agent_name="general-purpose")
    t2 = seed.create_task(goal="b", policy_name="react", agent_name="general-purpose")
    resolver = _resolver(event_log, content_store, dispatcher, tmp_path)
    e1 = resolver.resolve_engine(fold(event_log, content_store, t1.task_id))
    e2 = resolver.resolve_engine(fold(event_log, content_store, t2.task_id))
    assert e1 is e2


def test_resolver_unknown_agent_is_hard_error(tmp_path: Path) -> None:
    """An unknown ``agent_name`` is a HARD error at resolve/lease time —
    never a silent no-op."""
    event_log, content_store, dispatcher = _storage()
    seed = build_engine_for_agent(
        official_specs()["main"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    task = seed.create_task(
        goal="x", policy_name="react", agent_name="nonesuch"
    )
    resolver = _resolver(event_log, content_store, dispatcher, tmp_path)
    folded = fold(event_log, content_store, task.task_id)
    with pytest.raises(UnknownAgentError) as exc:
        resolver.resolve_engine(folded)
    assert exc.value.agent_name == "nonesuch"
    assert "general-purpose" in exc.value.available


def test_resolver_unnamed_hard_errors_without_fallback(tmp_path: Path) -> None:
    """The legacy ``unnamed`` default is a hard error unless an explicit
    fallback Agent is configured (new tasks MUST name a resolvable Agent)."""
    event_log, content_store, dispatcher = _storage()
    seed = build_engine_for_agent(
        official_specs()["main"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    # Engine.create_task defaults to "unnamed" (legacy / low-level path).
    task = seed.create_task(goal="legacy", policy_name="react")
    assert agent_name_of(event_log, task.task_id) == "unnamed"

    no_fallback = _resolver(event_log, content_store, dispatcher, tmp_path)
    with pytest.raises(UnknownAgentError):
        no_fallback.resolve_engine(fold(event_log, content_store, task.task_id))

    with_fallback = _resolver(
        event_log, content_store, dispatcher, tmp_path,
        unnamed_fallback=official_specs()["main"],
    )
    engine = with_fallback.resolve_engine(
        fold(event_log, content_store, task.task_id)
    )
    assert engine._policy._system_prompt == official_specs()["main"].instructions


# ---------------------------------------------------------------------------
# Demo — run_leased_task drives each task with its own Agent
# ---------------------------------------------------------------------------


def test_demo_two_agents_one_host_one_primitive(tmp_path: Path) -> None:
    """The acceptance demo: a general-purpose-created task and an explore-
    created task, both leased through the SINGLE ``run_leased_task``
    primitive against ONE resident resolver host, are each driven by their
    OWN Agent's Engine — proven by the system prompt recorded on each task's
    LLM request."""
    event_log, content_store, dispatcher = _storage()
    provider = _EndTurnProvider()
    resolver = _resolver(
        event_log, content_store, dispatcher, tmp_path, provider=provider
    )

    # Two tasks created naming two different Agents.
    gp_task = resolver.engine.create_task(  # the seed Engine writes TaskCreated
        goal="work", policy_name="react", agent_name="general-purpose"
    )
    exp_task = resolver.engine.create_task(
        goal="review", policy_name="react", agent_name="explore"
    )
    for tid in (gp_task.task_id, exp_task.task_id):
        # Seed each conversation's first user message via that task's Engine.
        engine = resolver.resolve_engine(fold(event_log, content_store, tid))
        dispatcher.enqueue(tid)
        lease = dispatcher.lease(worker_id="host", lease_seconds=60.0, task_id=tid)
        assert lease is not None
        task = fold(event_log, content_store, tid)
        engine.append_user_message(task, content=[TextBlock(text="go")], lease_id=lease.lease_id)
        # Drain through the SHARED primitive against the resolver host.
        outcome = run_leased_task(resolver, lease)
        assert outcome == "drained"

    # The general-purpose task ran under the general-purpose prompt; the
    # explorer under the explore prompt — captured in request order.
    assert official_specs()["general-purpose"].instructions in provider.systems[0]
    assert official_specs()["explore"].instructions in provider.systems[1]
    # Cross-check: each request carries ITS agent's prompt, not the other's.
    assert official_specs()["explore"].instructions not in provider.systems[0]
    assert official_specs()["general-purpose"].instructions not in provider.systems[1]

    gp = fold(event_log, content_store, gp_task.task_id)
    exp = fold(event_log, content_store, exp_task.task_id)
    assert gp.status == "terminal"
    assert exp.status == "terminal"


def test_demo_unknown_agent_hard_errors_at_lease(tmp_path: Path) -> None:
    """A leased Task naming an unknown Agent fails LOUD through
    ``run_leased_task`` (not a silent no-op) — the resolver raises at lease
    time."""
    event_log, content_store, dispatcher = _storage()
    resolver = _resolver(event_log, content_store, dispatcher, tmp_path)
    task = resolver.engine.create_task(
        goal="x", policy_name="react", agent_name="ghost"
    )
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="host", lease_seconds=60.0, task_id=task.task_id)
    assert lease is not None
    with pytest.raises(UnknownAgentError):
        run_leased_task(resolver, lease)


# ---------------------------------------------------------------------------
# The SDK host/driver writes the driving agent name on TaskCreated (D2)
# ---------------------------------------------------------------------------


def test_session_runner_records_agent_name(tmp_path: Path) -> None:
    """An SDK-host root task records the driving agent name (a resolvable
    Agent — ``"explore"``), NOT the legacy ``"unnamed"`` default."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "x.py").write_text("print('hi')\n")

    host = make_host(
        official_agent_registry(),
        workspace_dir=workspace,
        provider=_EndTurnProvider(),
        model="test-model",
        multi_turn=False,
    )
    out = make_driver(host).start(goal="look around", agent="explore")
    assert agent_name_of(host.event_log, out.task_id) == "explore"


# ---------------------------------------------------------------------------
# Resident multi-worker path: a subtask claimed by a worker's untargeted
# tick() (rather than the drain's targeted descent) resolves through
# ``resolve_engine`` and must NOT inherit the top-level multi-turn wrapper.
# ---------------------------------------------------------------------------


def _multi_turn_resolver(
    event_log: InMemoryEventLog,
    content_store: InMemoryContentStore,
    dispatcher: InMemoryDispatcher,
    tmp_path: Path,
) -> SdkHost:
    """A resident host wired with the multi-turn wrapper (the ``Client``
    interactive shape), so a normally-finishing ROOT turn suspends on the
    next-goal handle. This is the wiring under which a subtask must NOT be
    wrapped."""
    from noeta.execution.driver import multi_turn_policy_wrapper

    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=_EndTurnProvider(),
        model="test-model",
        workspace_dir=tmp_path,
        registry=official_agent_registry(),
        aliases={"default": "main"},
        policy_wrapper=multi_turn_policy_wrapper,
        require_approval_tools=(),
    )


def _is_multi_turn_wrapped(engine: Engine) -> bool:
    """True iff the Engine's policy is (or is nested under) the multi-turn
    wrapper that turns ``FinishDecision`` into a next-goal suspend."""
    from noeta.execution.multi_turn import MultiTurnReActPolicy

    policy = engine._policy
    return isinstance(policy, MultiTurnReActPolicy)


def test_subtask_engine_is_not_multi_turn_wrapped(tmp_path: Path) -> None:
    """A child task (has a ``parent_task_id``) resolved through the resident
    per-task ``resolve_engine`` must NOT carry the multi-turn wrapper — it is
    one-shot and must finish with a real ``TaskCompleted`` so the
    ``ChildLifecycleObserver`` fires the parent's wake. The root task on the
    SAME agent + model still IS wrapped, and the two live in DISTINCT cache
    slots (the wrapper-is-None dimension keys them apart), so the root's
    wrapper cannot leak to the child via the cache.

    This is the regression guard for the deadlock where a resident worker's
    untargeted ``tick()`` claimed a spawned explorer child ahead of the
    drain, drove it through ``resolve_engine`` (which used to wrap it
    unconditionally), and the child's ``FinishDecision`` became a next-goal
    suspend → no ``TaskCompleted`` → the parent's
    ``SubtaskGroupCompleted`` barrier never fired → deadlock."""
    from noeta.policies.control_tools import WORKFLOW_AGENT_NAME  # noqa: F401

    event_log, content_store, dispatcher = _storage()
    seed = build_engine_for_agent(
        official_specs()["main"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    # Two tasks on the SAME agent + model — the cache-collision case. One is a
    # root task, the other a delegated child (parent_task_id set).
    root_task = seed.create_task(
        goal="root", policy_name="react", agent_name="general-purpose"
    )
    child_task = seed.create_task(
        goal="child",
        policy_name="react",
        agent_name="general-purpose",
        parent_task_id=root_task.task_id,
    )

    resolver = _multi_turn_resolver(event_log, content_store, dispatcher, tmp_path)
    root_engine = resolver.resolve_engine(
        fold(event_log, content_store, root_task.task_id)
    )
    child_engine = resolver.resolve_engine(
        fold(event_log, content_store, child_task.task_id)
    )

    # The root keeps the wrapper; the child does not.
    assert _is_multi_turn_wrapped(root_engine), (
        "root engine lost its multi-turn wrapper"
    )
    assert not _is_multi_turn_wrapped(child_engine), (
        "child engine inherited the multi-turn wrapper — a subtask must be "
        "one-shot (FinishDecision → TaskCompleted), not a next-goal suspend"
    )
    # Same agent + model, but the wrapper-is-None cache dimension keeps them in
    # SEPARATE slots — so resolving the child never returns the root's wrapped
    # Engine (the fix is not masked by the cache).
    assert root_engine is not child_engine, (
        "root and child shared one cached Engine — the wrapper-is-None cache "
        "dimension failed to key them apart"
    )
    assert len(resolver._engines) == 2, (
        f"expected 2 cached engines (root wrapped + child unwrapped), "
        f"got {len(resolver._engines)}: {list(resolver._engines.keys())}"
    )


def test_subtask_engine_cache_isolates_wrapped_from_unwrapped(tmp_path: Path) -> None:
    """Direct unit guard on ``_engine_for_agent``: the same agent resolved once
    with the host wrapper and once with ``policy_wrapper=None`` lands in two
    cache entries (the 10th key dimension). Without this, a child resolved
    AFTER its same-agent root would reuse the root's wrapped Engine and the
    subtask fix would be silently masked."""
    event_log, content_store, dispatcher = _storage()
    resolver = _multi_turn_resolver(event_log, content_store, dispatcher, tmp_path)
    agent = resolver._lookup_agent("general-purpose", task_id="<unit>")

    wrapped = resolver._engine_for_agent(agent, policy_wrapper=resolver.policy_wrapper)
    unwrapped = resolver._engine_for_agent(agent, policy_wrapper=None)

    assert _is_multi_turn_wrapped(wrapped)
    assert not _is_multi_turn_wrapped(unwrapped)
    assert wrapped is not unwrapped
    assert len(resolver._engines) == 2


def test_resolve_engine_routes_workflow_child(tmp_path: Path) -> None:
    """A ``__workflow__`` child (the orchestration interpreter, not a roster
    agent) resolved through the resident per-task ``resolve_engine`` must route
    to the orchestration Engine — NOT raise ``UnknownAgentError``. Same root
    cause as the wrapper bug: a worker's untargeted ``tick()`` can claim the
    child ahead of the drain's targeted descent, and ``resolve_engine`` (unlike
    the drain's ``_build_subtask_engine``) used to have no ``__workflow__``
    branch."""
    event_log, content_store, dispatcher = _storage()
    seed = build_engine_for_agent(
        official_specs()["main"],
        "m",
        event_log=event_log,
        content_store=content_store,
        provider=_EndTurnProvider(),
        workspace_dir=tmp_path,
    )
    parent = seed.create_task(
        goal="run a workflow", policy_name="react", agent_name="general-purpose"
    )
    wf_child = seed.create_task(
        goal="orchestrate",
        policy_name="react",
        agent_name="__workflow__",
        parent_task_id=parent.task_id,
        inputs={"script": "pass", "args": {}},
    )

    resolver = _multi_turn_resolver(event_log, content_store, dispatcher, tmp_path)
    # Must NOT raise UnknownAgentError; must return a real Engine.
    engine = resolver.resolve_engine(
        fold(event_log, content_store, wf_child.task_id)
    )
    assert engine is not None
    # The orchestration child is one-shot too — never multi-turn wrapped.
    assert not _is_multi_turn_wrapped(engine)


def test_worker_driven_subtask_completes_not_suspends(tmp_path: Path) -> None:
    """End-to-end guard for the deadlock root cause. Under a multi-turn host
    (the resident ``Client`` shape, where a ROOT turn's ``FinishDecision``
    becomes a next-goal suspend), a delegated CHILD driven through the worker
    primitive ``run_leased_task`` — i.e. claimed by a resident worker's
    untargeted ``tick()`` ahead of the drain's targeted descent — must reach a
    genuine ``TaskCompleted``, NOT a ``TaskSuspended`` on the next-goal handle.

    Before the fix, ``resolve_engine`` wrapped the child unconditionally, so the
    child's ``FinishDecision`` became a next-goal suspend: no ``TaskCompleted``
    fired, the ``ChildLifecycleObserver`` never woke the parent, and the parent's
    ``SubtaskGroupCompleted`` barrier deadlocked."""
    from noeta.protocols.messages import TextBlock

    event_log, content_store, dispatcher = _storage()
    resolver = _multi_turn_resolver(event_log, content_store, dispatcher, tmp_path)

    # A delegated child: parent_task_id set ⇒ resolve_engine builds it unwrapped.
    parent = resolver.engine.create_task(
        goal="parent", policy_name="react", agent_name="general-purpose"
    )
    child = resolver.engine.create_task(
        goal="child",
        policy_name="react",
        agent_name="general-purpose",
        parent_task_id=parent.task_id,
    )
    dispatcher.enqueue(child.task_id)
    lease = dispatcher.lease(
        worker_id="resident-worker", lease_seconds=60.0, task_id=child.task_id
    )
    assert lease is not None
    # Seed the child's opening user message (the worker's goal-seeding path does
    # this too; do it explicitly so the test is independent of that ordering).
    engine = resolver.resolve_engine(fold(event_log, content_store, child.task_id))
    seeded = fold(event_log, content_store, child.task_id)
    engine.append_user_message(
        seeded, content=[TextBlock(text="do it")], lease_id=lease.lease_id
    )

    outcome = run_leased_task(resolver, lease)
    assert outcome == "drained"

    types = [e.type for e in event_log.read(child.task_id)]
    assert "TaskCompleted" in types, (
        f"child did not complete (the wrapper turned FinishDecision into a "
        f"next-goal suspend): {types}"
    )
    assert "TaskSuspended" not in types, (
        f"child suspended instead of completing: {types}"
    )
    child_folded = fold(event_log, content_store, child.task_id)
    assert child_folded.status == "terminal"
