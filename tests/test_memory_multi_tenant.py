"""Issue #53 — per-task memory-root resolution + consolidation digest scoping.

Two host-side seams let a multi-tenant product split the Memory v2 store per
tenant while the SDK stays tenancy-agnostic (it hands over task ids, never
users):

* ``HostConfig.memory_dir`` / ``global_memory_dir`` / ``memory_root_resolver``
  forward through ``Client`` to ``SdkHost``; every consumer of the resolution
  chain (``memory_root`` / ``memory_recall_context`` / the engine build's tool
  pack + resident index) resolves the per-task resolver FIRST, falling back to
  the ``memory_dir`` > ``global_memory_dir`` > default chain on ``None``.
* The Engine cache is partitioned by resolved per-task root
  (``_engine_cache_scope``) — two tasks with equal standard key dimensions but
  different tenant roots must never share a cached Engine (the MemoryStore is
  baked into its tool closures).
* ``build_consolidation_digest`` / ``run_consolidation`` take ``include_task``
  so a host runs one curation pass per tenant; ``run_consolidation``'s
  ``on_seeded`` hands the curation task id to the host BEFORE it is claimable,
  so the host can bind it in its resolver mapping.

Defaults unchanged: no resolver ⇒ today's precedence chain; no filter ⇒ the
whole-ledger digest (the existing suites cover byte-identity).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from noeta.client.consolidation import (
    CONSOLIDATION_AGENT_NAME,
    build_consolidation_digest,
    read_consolidation_marker,
    run_consolidation,
)
from noeta.core.fold import fold
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.sdk import AgentDefinition, Client, HostConfig, Options
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode
from noeta.tools.memory import MEMORY_WRITE_TOOL_NAME

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _write_call(call_id: str, name: str, text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name=MEMORY_WRITE_TOOL_NAME,
                arguments={"name": name, "text": text},
            )
        ],
        usage=Usage(uncached=1, output=1),
    )


def _seed_memory(root: Path, name: str, text: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(text, encoding="utf-8")


def _memory_host(tmp_path: Path, responses, **knobs):
    """A memory-enabled SdkHost + driver over a fresh in-memory triple."""
    host = make_host(
        make_registry(runner_main_spec("main", memory=True)),
        workspace_dir=tmp_path / "ws",
        provider=FakeLLMProvider(responses=list(responses)),
        model="stub-model",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        **knobs,
    )
    (tmp_path / "ws").mkdir(exist_ok=True)
    return host, make_driver(host)


def _memory_origins(host, task_id: str) -> list[str]:
    folded = fold(host.event_log, host.content_store, task_id)
    return [
        "".join(b.text for b in m.content if hasattr(b, "text"))
        for m in folded.runtime.messages
        if m.origin == "memory"
    ]


# ---------------------------------------------------------------------------
# 1. Resolution chain — resolver first, fallback preserved
# ---------------------------------------------------------------------------


def test_memory_root_resolver_first_then_host_chain(tmp_path: Path) -> None:
    tenant = tmp_path / "tenant-a"
    explicit = tmp_path / "explicit"
    global_dir = tmp_path / "global"

    def resolver(task_id: str) -> Optional[Path]:
        return tenant if task_id == "task-a" else None

    host, _ = _memory_host(
        tmp_path,
        [],
        memory_dir=explicit,
        global_memory_dir=global_dir,
        memory_root_resolver=resolver,
    )
    # Resolver hit wins over the whole host chain.
    assert host.memory_root("task-a") == tenant
    # Resolver fallback (None) / no task id ⇒ the existing chain, unchanged.
    assert host.memory_root("task-unknown") == explicit
    assert host.memory_root() == explicit


def test_memory_root_host_chain_without_resolver(tmp_path: Path) -> None:
    from noeta.execution import memory as execution_memory

    explicit = tmp_path / "explicit"
    global_dir = tmp_path / "global"
    host, _ = _memory_host(tmp_path, [], global_memory_dir=global_dir)
    assert host.memory_root() == global_dir
    assert host.memory_root("any-task") == global_dir  # task id is inert
    host2, _ = _memory_host(
        tmp_path, [], memory_dir=explicit, global_memory_dir=global_dir
    )
    assert host2.memory_root() == explicit
    host3, _ = _memory_host(tmp_path, [])
    assert host3.memory_root() == execution_memory.DEFAULT_GLOBAL_MEMORY_DIR


def test_host_config_forwards_memory_fields(tmp_path: Path) -> None:
    tenant = tmp_path / "tenant"
    explicit = tmp_path / "explicit"

    client = Client(
        Options(
            system_prompt="finish",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(responses=[]),
        workspace_dir=tmp_path,
        host_config=HostConfig(
            memory_dir=explicit,
            global_memory_dir=tmp_path / "global",
            memory_root_resolver=lambda tid: tenant if tid == "t-1" else None,
        ),
    )
    try:
        assert client.memory_root() == explicit
        assert client.memory_root("t-1") == tenant
        assert client.memory_root("t-2") == explicit
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# 2. Recall reads the per-task store (resolver deriving the tenant from the
#    durable workspace binding — the first-turn-capable product strategy)
# ---------------------------------------------------------------------------


def test_recall_and_tools_follow_the_per_task_root(tmp_path: Path) -> None:
    """Two sessions on different tenant roots: each recalls only its own
    store, and a ``memory_write`` lands only in its own root. The resolver
    derives the tenant from the session's durable ``TaskHostBound`` workspace
    binding — available before the first turn's recall runs, so even the
    opening goal is tenant-scoped."""
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    mem_a = tmp_path / "mem-a"
    mem_b = tmp_path / "mem-b"
    shared_pool = tmp_path / "shared-pool"
    _seed_memory(mem_a, "deploy-runbook", "# Deploy\nAlpha tenant secret.\n")
    _seed_memory(mem_b, "billing-policy", "# Billing\nBeta tenant secret.\n")

    holder: dict = {}

    def resolver(task_id: str) -> Optional[Path]:
        for env in holder["host"].event_log.read(task_id):
            if env.type == "TaskHostBound":
                ws = getattr(env.payload, "workspace_dir", None)
                if ws == str(ws_a):
                    return mem_a
                if ws == str(ws_b):
                    return mem_b
        return None

    host, driver = _memory_host(
        tmp_path,
        [
            _end("a-reply"),
            _write_call("mw-b", "from-b", "# B\nWritten by tenant B.\n"),
            _end("b-reply"),
        ],
        global_memory_dir=shared_pool,
        memory_root_resolver=resolver,
    )
    holder["host"] = host

    out_a = driver.start(
        goal="what does the deploy-runbook say?", agent="main",
        workspace_dir=str(ws_a),
    )
    assert out_a.status == "terminal"
    recalls_a = _memory_origins(host, out_a.task_id)
    assert recalls_a and "Alpha tenant secret." in recalls_a[0]
    assert all("Beta tenant secret." not in r for r in recalls_a)

    out_b = driver.start(
        goal="what does the deploy-runbook say?", agent="main",
        workspace_dir=str(ws_b),
    )
    assert out_b.status == "terminal"
    # Tenant B's store has no deploy-runbook: the SAME goal recalls nothing —
    # tenant A's facts never cross into B's stream.
    assert _memory_origins(host, out_b.task_id) == []
    # B's memory_write landed in B's root only; the shared pool stays empty.
    assert (mem_b / "from-b.md").is_file()
    assert not (mem_a / "from-b.md").exists()
    assert not list(shared_pool.glob("*.md")) if shared_pool.exists() else True


# ---------------------------------------------------------------------------
# 3. Engine cache — the resolved root partitions the cache
# ---------------------------------------------------------------------------


def test_engine_cache_not_shared_across_tenant_roots(tmp_path: Path) -> None:
    """Two sessions equal on EVERY standard cache dimension (agent, model,
    workspace, provider, …) but mapped to different tenant roots must resolve
    distinct Engines — and each session's ``memory_write`` must land in its
    own root. Without the ``_engine_cache_scope`` partition the second session
    would reuse the first tenant's cached Engine (its baked-in MemoryStore)."""
    mem_a = tmp_path / "mem-a"
    mem_b = tmp_path / "mem-b"
    mapping: dict[str, Path] = {}

    host, driver = _memory_host(
        tmp_path,
        [
            _write_call("mw-a", "fact", "# F\nTenant A's fact.\n"),
            _end("a-done"),
            _write_call("mw-b", "fact", "# F\nTenant B's fact.\n"),
            _end("b-done"),
        ],
        global_memory_dir=tmp_path / "shared-pool",
        memory_root_resolver=mapping.get,
    )

    # The product pattern: seed (task id minted, lease held), register the
    # tenant mapping, then drive — the driving Engine resolves the tenant root.
    seeded_a = driver.seed_start(goal="remember my fact", agent="main")
    mapping[seeded_a.task_id] = mem_a
    assert driver.drive_seeded(seeded_a).status == "terminal"

    seeded_b = driver.seed_start(goal="remember my fact", agent="main")
    mapping[seeded_b.task_id] = mem_b
    assert driver.drive_seeded(seeded_b).status == "terminal"

    # Same memory name, different stores — no cross-tenant clobbering.
    assert "Tenant A's fact." in (mem_a / "fact.md").read_text(encoding="utf-8")
    assert "Tenant B's fact." in (mem_b / "fact.md").read_text(encoding="utf-8")

    # The cached Engines are distinct per tenant root (and stable per task).
    task_a = fold(host.event_log, host.content_store, seeded_a.task_id)
    task_b = fold(host.event_log, host.content_store, seeded_b.task_id)
    assert host.resolve_engine(task_a) is not host.resolve_engine(task_b)
    assert host.resolve_engine(task_a) is host.resolve_engine(task_a)


def test_engine_cache_scope_is_none_for_memory_off_or_fallback(
    tmp_path: Path,
) -> None:
    """The scope partitions ONLY when it must: a memory-off agent or a
    resolver fallback keeps the shared ``None`` slot (no cache fragmentation,
    byte-equal key semantics)."""
    tenant = tmp_path / "tenant"
    host, _ = _memory_host(
        tmp_path,
        [],
        memory_root_resolver=lambda tid: tenant if tid == "t-a" else None,
    )
    spec_on = host.registry.resolve("main")
    assert host._engine_cache_scope(spec_on, "t-a") == str(tenant)
    assert host._engine_cache_scope(spec_on, "t-unknown") is None
    assert host._engine_cache_scope(spec_on, None) is None
    import dataclasses

    spec_off = dataclasses.replace(
        spec_on,
        capabilities=dataclasses.replace(spec_on.capabilities, memory=False),
    )
    assert host._engine_cache_scope(spec_off, "t-a") is None


# ---------------------------------------------------------------------------
# 4. Consolidation digest scoping
# ---------------------------------------------------------------------------


def _client(tmp_path: Path, responses, *, agents=None) -> Client:
    options = Options(
        system_prompt="you finish immediately",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
        agents=dict(agents or {}),
    )
    return Client(
        options,
        provider=FakeLLMProvider(responses=list(responses)),
        workspace_dir=tmp_path,
        multi_turn=True,
    )


def test_digest_include_task_scopes_sessions(tmp_path: Path) -> None:
    client = _client(tmp_path, [_end("r-a"), _end("r-b")])
    try:
        out_a = client.start(goal="tenant-a goal")
        client.start(goal="tenant-b goal")
        digest = build_consolidation_digest(
            client, include_task=lambda tid: tid == out_a.task_id
        )
        assert digest is not None
        assert "tenant-a goal" in digest
        assert "tenant-b goal" not in digest
        assert "1 shown" in digest
        assert "restricted to a host-selected subset of sessions" in digest
        # An out-of-scope session is out of the digest's universe entirely:
        # it neither consumes the cap nor counts as omitted.
        assert "omitted" not in digest
        # No filter ⇒ the whole ledger, header unchanged.
        full = build_consolidation_digest(client)
        assert full is not None
        assert "tenant-b goal" in full
        assert "restricted to a host-selected subset" not in full
        # A filter matching nothing ⇒ no digest at all.
        assert build_consolidation_digest(client, include_task=lambda _: False) is None
    finally:
        client.shutdown()


def test_run_consolidation_scoped_per_tenant_with_on_seeded(
    tmp_path: Path,
) -> None:
    """One curation pass per tenant: the digest carries only that tenant's
    sessions, the marker debounces per tenant root, and ``on_seeded`` hands
    the curation task id over BEFORE it is claimable so the host can bind it
    in its resolver mapping."""
    root_a = tmp_path / "mem-a"
    root_b = tmp_path / "mem-b"
    internal = AgentDefinition(
        description="internal curation stand-in",
        prompt="curate the store",
        tools=(),
    )
    client = _client(
        tmp_path,
        [_end("r-a"), _end("r-b")],
        agents={CONSOLIDATION_AGENT_NAME: internal},
    )
    try:
        out_a = client.start(goal="tenant-a activity")
        out_b = client.start(goal="tenant-b activity")
        seeded_ids: list[str] = []
        assert (
            run_consolidation(
                client,
                memory_root=root_a,
                now=NOW,
                include_task=lambda tid: tid == out_a.task_id,
                on_seeded=seeded_ids.append,
            )
            is True
        )
        # Tenant A's pass: scoped digest, tenant-A marker only, id handed over.
        assert len(seeded_ids) == 1
        goal = client.events(seeded_ids[0])[0].payload.goal
        assert "tenant-a activity" in goal
        assert "tenant-b activity" not in goal
        assert read_consolidation_marker(root_a) == NOW
        assert read_consolidation_marker(root_b) is None
        # Tenant B debounces independently (per-root marker): its pass at the
        # same instant still runs.
        assert (
            run_consolidation(
                client,
                memory_root=root_b,
                now=NOW,
                include_task=lambda tid: tid == out_b.task_id,
                on_seeded=seeded_ids.append,
            )
            is True
        )
        assert len(seeded_ids) == 2
        goal_b = client.events(seeded_ids[1])[0].payload.goal
        assert "tenant-b activity" in goal_b
        assert "tenant-a activity" not in goal_b
        assert read_consolidation_marker(root_b) == NOW
    finally:
        client.shutdown()


def test_run_consolidation_empty_scope_writes_no_marker(tmp_path: Path) -> None:
    root = tmp_path / "mem"
    client = _client(tmp_path, [_end("r")])
    try:
        client.start(goal="someone else's activity")
        assert (
            run_consolidation(
                client, memory_root=root, now=NOW, include_task=lambda _: False
            )
            is False
        )
    finally:
        client.shutdown()
    # Nothing in scope ⇒ nothing enqueued ⇒ the debounce is not armed.
    assert read_consolidation_marker(root) is None
