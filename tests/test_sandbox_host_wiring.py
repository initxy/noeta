"""Host-layer sandbox manager: provision + route into build_session_inputs.

The sandbox backend is *reachable* (``build_session_inputs(exec_env=...)``) and
*runs*: the host builds a live backend from a ``SandboxProvider`` (v2) or the v1
``HostConfig.exec_env`` attach config, and threads it into every session's Engine
so the fs / shell tools' IO lands in the container instead of the host. Covered:

* ``SandboxExecEnvManager`` — per-session ``allocate`` (fresh container + durable
  ref), cached ``resolve`` (build once), ``attach`` reconnect, ``release`` /
  idempotent ``teardown``;
* per-session isolation — two ``allocate`` calls mint DISTINCT containers;
* ``SdkHost`` default (no config) is byte-identical (LocalExecEnv + host root);
* ``SdkHost`` with the v1 attach config routes fs tools onto the container
  backend + a lexical container ``WorkspaceRoot`` rooted at ``workdir``;
* ``SdkHost`` with a v2 provider provisions a per-session container and routes
  the fs tools onto it;
* the SEED build shares the Engine cache with the first driving turn → the same
  backend;
* the Client threads the config in and reaps it on shutdown.

No socket is ever opened: the real ``AioSandboxExecEnv`` builds inert, and the
end-to-end tests inject a fake backend factory / fake provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Iterable, Optional

import pytest

import noeta.client.sandbox as sandbox_mod
from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    Capabilities,
    ComponentRef,
    ToolRef,
)
from noeta.client.host import SdkHost
from noeta.client.host_config import SandboxExecEnvConfig
from noeta.client.sandbox import SandboxExecEnvManager, provider_for_config
from noeta.client.sandbox_provider import (
    SandboxHandle,
    SandboxSpec,
    StaticApiKeyAuth,
    decode_exec_env_ref,
    encode_exec_env_ref,
)
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.tool import ToolContext
from noeta.sdk import Client, HostConfig, Options
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs._subprocess import _RunOutcome
from noeta.tools.fs.exec_env import AioSandboxExecEnv


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class RecordingExecEnv:
    """A minimal :class:`ExecEnv` tagged with the base_url it was built for."""

    def __init__(
        self, files: Optional[dict[str, bytes]] = None, *, base_url: str = ""
    ) -> None:
        self.files = dict(files or {})
        self.base_url = base_url
        self.reads: list[str] = []

    def read_bytes(self, path: Path) -> bytes:
        self.reads.append(str(path))
        return self.files[str(path)]

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def write_bytes(self, path: Path, body: bytes) -> None:
        self.files[str(path)] = body

    def create_exclusive(self, path: Path, body: bytes) -> None:
        self.files[str(path)] = body

    def unlink(self, path: Path) -> None:
        self.files.pop(str(path), None)

    def exists(self, path: Path) -> bool:
        return str(path) in self.files

    def is_file(self, path: Path) -> bool:
        return str(path) in self.files

    def is_dir(self, path: Path) -> bool:
        return True

    def is_symlink(self, path: Path) -> bool:
        return False

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None) -> _RunOutcome:
        return _RunOutcome(0, 1, b"ran", b"", False, False, False)


class FakeProvider:
    """A ``SandboxProvider`` that mints a fresh handle per ``allocate`` (no socket)."""

    def __init__(self, *, workdir: str = "/workspace") -> None:
        self._workdir = workdir
        self._counter = 0
        self.allocated: list[tuple[str, SandboxSpec]] = []
        self.released: list[str] = []

    def allocate(self, session_root_id: str, spec: SandboxSpec) -> SandboxHandle:
        self._counter += 1
        self.allocated.append((session_root_id, spec))
        return SandboxHandle(
            base_url=f"http://sbx-{self._counter}:8080",
            sandbox_id=f"sid-{self._counter}",
            auth=StaticApiKeyAuth("SANDBOX_API_KEY"),
            workdir=self._workdir,
        )

    def release(self, session_root_id: str) -> None:
        self.released.append(session_root_id)

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=sandbox_id,
            auth=StaticApiKeyAuth("SANDBOX_API_KEY"),
            workdir=self._workdir,
        )


def _recording_factory() -> Any:
    return lambda handle, preamble=None: RecordingExecEnv(base_url=handle.base_url)


# --------------------------------------------------------------------------- #
# SandboxExecEnvManager — per-session lifecycle
# --------------------------------------------------------------------------- #


def _manager(provider: Any, **kw: Any) -> SandboxExecEnvManager:
    return SandboxExecEnvManager(
        provider,
        spec_template=SandboxSpec(image="img"),
        backend_factory=_recording_factory(),
        **kw,
    )


def _capturing_factory(captured: list[Any]) -> Any:
    """A factory that records the ``preamble`` the manager binds onto a backend."""
    def factory(handle: SandboxHandle, preamble: Any = None) -> RecordingExecEnv:
        captured.append(preamble)
        return RecordingExecEnv(base_url=handle.base_url)

    return factory


def test_exec_preamble_binds_the_session_ref_onto_the_backend() -> None:
    # A HostConfig ``sandbox_exec_preamble`` reaches the backend as a per-session
    # BoundPreamble: the manager curries the durable exec_env_ref, so the host
    # sees (ref, argv) while the backend gets a plain (argv) -> str minted fresh.
    seen: list[tuple[str, list[str]]] = []

    def exec_preamble(ref: str, argv: Sequence[str]) -> str:
        seen.append((ref, list(argv)))
        return "SETUP && "

    captured: list[Any] = []
    mgr = SandboxExecEnvManager(
        FakeProvider(),
        spec_template=SandboxSpec(image="img"),
        backend_factory=_capturing_factory(captured),
        exec_preamble=exec_preamble,
    )
    ref = mgr.allocate("task-root")
    mgr.resolve(ref)
    bound = captured[0]
    assert bound is not None
    assert bound(["lark-cli", "whoami"]) == "SETUP && "
    assert seen == [(ref, ["lark-cli", "whoami"])]


def test_no_exec_preamble_binds_none() -> None:
    captured: list[Any] = []
    mgr = SandboxExecEnvManager(
        FakeProvider(),
        spec_template=SandboxSpec(image="img"),
        backend_factory=_capturing_factory(captured),
    )
    mgr.resolve(mgr.allocate("task-root"))
    assert captured == [None]


def test_allocate_provisions_and_returns_encoded_ref() -> None:
    provider = FakeProvider()
    mgr = _manager(provider)
    ref = mgr.allocate("task-root", host_workspace="/host/ws")
    assert ref == encode_exec_env_ref("http://sbx-1:8080", "sid-1")
    # the per-session workspace mount was assembled into the spec
    _, spec = provider.allocated[0]
    assert any(m.target == "/workspace" and m.source == "/host/ws" for m in spec.mounts)


def test_resolve_builds_once_and_caches() -> None:
    provider = FakeProvider()
    mgr = _manager(provider)
    ref = mgr.allocate("task-root")
    backend, workdir = mgr.resolve(ref)
    assert backend is mgr.resolve(ref)[0]  # cached — one backend per ref
    assert backend.base_url == "http://sbx-1:8080"
    assert workdir == "/workspace"


def test_resolve_unknown_ref_reconnects_via_attach() -> None:
    # A ref this manager never allocated (resume / reclaim, another host) is
    # reconnected via provider.attach against the RECORDED address.
    provider = FakeProvider()
    mgr = _manager(provider)
    ref = encode_exec_env_ref("http://recorded:9999", "sid-remote")
    backend, _ = mgr.resolve(ref)
    assert backend.base_url == "http://recorded:9999"


def test_two_allocations_are_isolated_containers() -> None:
    provider = FakeProvider()
    mgr = _manager(provider)
    ref_a = mgr.allocate("task-a")
    ref_b = mgr.allocate("task-b")
    assert ref_a != ref_b  # per-session isolation: distinct sandbox ids
    assert mgr.resolve(ref_a)[0] is not mgr.resolve(ref_b)[0]


def test_release_reaps_the_session_container() -> None:
    provider = FakeProvider()
    mgr = _manager(provider)
    mgr.allocate("task-root")
    mgr.release("task-root")
    assert provider.released == ["task-root"]
    # idempotent — releasing again (or an unknown id) is a clean no-op
    mgr.release("task-root")
    mgr.release("never-allocated")
    assert provider.released == ["task-root", "task-root", "never-allocated"]


def test_release_evicts_per_session_backend_cache() -> None:
    # Per-session path: releasing a root drops its uniquely-owned cached backend,
    # so a later resolve of the same ref reconnects (a fresh backend).
    provider = FakeProvider()
    mgr = _manager(provider)
    ref = mgr.allocate("task-root")
    first = mgr.resolve(ref)[0]
    mgr.release("task-root")
    assert mgr.resolve(ref)[0] is not first


# --------------------------------------------------------------------------- #
# resolve_browser — per-session browser backend (browser subsystem, B5)
# --------------------------------------------------------------------------- #


class _RecordingBrowser:
    """A stand-in browser backend that records the handle base_url it was built
    from — no socket, so tests assert vend/cache/reconnect without a container."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


def _browser_manager(provider: Any, built: list[Any], **kw: Any) -> SandboxExecEnvManager:
    def browser_factory(handle: SandboxHandle) -> _RecordingBrowser:
        obj = _RecordingBrowser(base_url=handle.base_url)
        built.append(obj)
        return obj

    return SandboxExecEnvManager(
        provider,
        spec_template=SandboxSpec(image="img"),
        backend_factory=_recording_factory(),
        browser_factory=browser_factory,
        **kw,
    )


def test_resolve_browser_builds_once_and_caches() -> None:
    built: list[Any] = []
    mgr = _browser_manager(FakeProvider(), built)
    ref = mgr.allocate("task-root")
    browser = mgr.resolve_browser(ref)
    assert mgr.resolve_browser(ref) is browser  # cached — one browser per ref
    assert len(built) == 1
    assert browser.base_url == "http://sbx-1:8080"


def test_resolve_browser_unknown_ref_reconnects_via_attach() -> None:
    # A ref never allocated on this host (resume / reclaim) is reconnected via
    # provider.attach — the browser backend addresses the RECORDED base_url, not
    # this host's default (mirrors resolve's reconnect path).
    built: list[Any] = []
    mgr = _browser_manager(FakeProvider(), built)
    ref = encode_exec_env_ref("http://sbx-remote:8080", "sid-remote")
    browser = mgr.resolve_browser(ref)
    assert browser.base_url == "http://sbx-remote:8080"


def test_resolve_browser_reuses_the_handle_a_prior_exec_resolve_cached() -> None:
    # resolve() caches the handle; a later resolve_browser reuses it (no second
    # attach) — the ExecEnv and browser backends address the SAME container.
    built: list[Any] = []
    provider = FakeProvider()
    mgr = _browser_manager(provider, built)
    ref = mgr.allocate("task-root")
    exec_backend, _ = mgr.resolve(ref)
    browser = mgr.resolve_browser(ref)
    assert browser.base_url == exec_backend.base_url == "http://sbx-1:8080"


def test_release_evicts_per_session_browser_cache() -> None:
    built: list[Any] = []
    mgr = _browser_manager(FakeProvider(), built)
    ref = mgr.allocate("task-root")
    first = mgr.resolve_browser(ref)
    mgr.release("task-root")
    assert mgr.resolve_browser(ref) is not first  # rebuilt after eviction
    assert len(built) == 2


def test_release_keeps_shared_attach_backend_for_peers() -> None:
    # Attach deployment (P3): every session shares ONE container (ref =
    # base_url). Releasing one session must NOT evict the shared cached backend a
    # peer is still using — only a per-session ref is evicted on release.
    cfg = SandboxExecEnvConfig(base_url="http://box:8080")
    mgr = SandboxExecEnvManager(
        provider_for_config(cfg),
        spec_template=SandboxSpec(image=""),
        default_ref="http://box:8080",
        backend_factory=_recording_factory(),
    )
    ref_a = mgr.allocate("task-a")
    ref_b = mgr.allocate("task-b")
    assert ref_a == ref_b == "http://box:8080"  # one shared container
    backend = mgr.resolve(ref_a)[0]
    mgr.release("task-a")
    # the peer still resolves the SAME cached backend — not rebuilt
    assert mgr.resolve(ref_b)[0] is backend


def test_teardown_releases_every_open_session() -> None:
    provider = FakeProvider()
    mgr = _manager(provider)
    mgr.allocate("task-a")
    mgr.allocate("task-b")
    mgr.teardown()
    assert sorted(provider.released) == ["task-a", "task-b"]
    mgr.teardown()  # idempotent


def test_default_ref_fallback_for_attach_config() -> None:
    # The attach path has ONE shared container; resolve(default_ref) targets it.
    cfg = SandboxExecEnvConfig(base_url="http://box:8080", workdir="/c/ws")
    mgr = SandboxExecEnvManager(
        provider_for_config(cfg),
        spec_template=SandboxSpec(image=""),
        default_workdir="/c/ws",
        default_ref="http://box:8080",
        backend_factory=_recording_factory(),
    )
    assert mgr.default_ref == "http://box:8080"
    backend, workdir = mgr.resolve(mgr.default_ref)
    assert backend.base_url == "http://box:8080"
    assert workdir == "/c/ws"


def test_config_attach_allocate_returns_bare_base_url() -> None:
    # The attach provider mints no sandbox_id → the ref is a bare base_url,
    # byte-identical to a v1 recording.
    cfg = SandboxExecEnvConfig(base_url="http://box:8080")
    mgr = SandboxExecEnvManager(
        provider_for_config(cfg),
        spec_template=SandboxSpec(image=""),
        backend_factory=_recording_factory(),
    )
    assert mgr.allocate("task-root") == "http://box:8080"


def test_default_backend_factory_builds_aio_adapter_without_socket() -> None:
    handle = SandboxHandle(
        base_url="http://box:8080",
        sandbox_id="sid",
        auth=StaticApiKeyAuth("SANDBOX_API_KEY"),
    )
    env = sandbox_mod._default_backend_factory(handle)
    assert isinstance(env, AioSandboxExecEnv)


# --------------------------------------------------------------------------- #
# SdkHost wiring
# --------------------------------------------------------------------------- #


def _stub_provider() -> FakeLLMProvider:
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


def _fs_spec() -> AgentSpec:
    """A main spec whitelisting the fs tools (by their pack names)."""
    return AgentSpec(
        name="main",
        instructions="You are the main agent.",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(
            ToolRef(name="read", risk_level="low", version="1"),
            ToolRef(name="write", risk_level="high", version="1"),
            ToolRef(name="shell_run", risk_level="high", version="1"),
        ),
        capabilities=Capabilities(),
        default_budget=BudgetSpec(max_iterations=20),
        metadata={},
    )


def _make_host(tmp_path: Path, **host_kwargs: Any) -> SdkHost:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    ws = tmp_path / "ws"
    ws.mkdir()
    registry = AgentRegistry()
    registry.add(_fs_spec())
    kwargs: dict[str, Any] = dict(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=_stub_provider(),
        model="stub-model",
        workspace_dir=ws,
        registry=registry,
        permission_mode="bypassPermissions",
    )
    kwargs.update(host_kwargs)
    return SdkHost(**kwargs)


def _build(host: SdkHost, **kw: Any) -> Any:
    return host._build_engine(
        _fs_spec(),
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
        **kw,
    )


def test_host_without_config_uses_local_backend(tmp_path: Path) -> None:
    from noeta.tools.fs.exec_env import LocalExecEnv

    host = _make_host(tmp_path)
    assert host._sandbox is None
    engine = _build(host, task_id="t1")
    read = engine._tools["read"]
    assert isinstance(read.exec_env, LocalExecEnv)
    assert read.workspace.lexical is False


def test_host_with_attach_config_routes_fs_tools_to_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", _recording_factory())
    host = _make_host(
        tmp_path,
        exec_env=SandboxExecEnvConfig(base_url="http://box:8080", workdir="/c/ws"),
    )
    assert host._sandbox is not None
    # No explicit ref → the attach path's default container (v1 behaviour).
    engine = _build(host, task_id="t1")
    backend = engine._tools["read"].exec_env
    for name in ("read", "write", "shell_run"):
        assert engine._tools[name].exec_env is backend
    assert backend.base_url == "http://box:8080"
    # the fs root is the lexical CONTAINER workdir, not the host workspace_dir
    read = engine._tools["read"]
    assert read.workspace.lexical is True
    assert read.workspace.root == Path("/c/ws")


def test_host_with_provider_routes_to_per_session_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", _recording_factory())
    provider = FakeProvider(workdir="/workspace")
    host = _make_host(tmp_path, sandbox_provider=provider)
    assert host._sandbox is not None
    ref = host.allocate_exec_env("task-root", str(tmp_path / "ws"))
    assert provider.allocated  # a container was provisioned
    engine = _build(host, task_id="task-root", exec_env_ref=ref)
    read = engine._tools["read"]
    assert read.exec_env.base_url == "http://sbx-1:8080"
    assert read.workspace.lexical is True
    assert read.workspace.root == Path("/workspace")


def test_host_with_config_uses_real_aio_adapter(tmp_path: Path) -> None:
    # No fake factory — the real one builds the AIO adapter. Building the backend
    # is inert (no socket); a full Engine build would now read environment /
    # skills THROUGH the container (Tier 2), so assert on the backend the manager
    # resolves rather than driving a build that expects a live container.
    host = _make_host(
        tmp_path,
        exec_env=SandboxExecEnvConfig(base_url="http://box:8080"),
    )
    backend, _ = host._sandbox.resolve("http://box:8080")
    assert isinstance(backend, AioSandboxExecEnv)


def test_read_flows_through_injected_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = RecordingExecEnv({"/c/ws/note.md": b"in-container"})
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", lambda handle, preamble=None: fake)
    host = _make_host(
        tmp_path,
        exec_env=SandboxExecEnvConfig(base_url="http://box:8080", workdir="/c/ws"),
    )
    engine = _build(host, task_id="t1")
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = engine._tools["read"].invoke({"path": "note.md"}, ctx=ctx)
    assert result.success
    assert fake.reads == ["/c/ws/note.md"]


def test_seed_build_shares_the_sandbox_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The seed Engine (built by resolve_engine_for_agent before a task exists)
    # shares the Engine cache with the first driving turn. If the seed built the
    # LOCAL backend, the cached Engine would pin it and the drive would bypass the
    # sandbox — so the seed must route to the SAME backend.
    fake = RecordingExecEnv(base_url="http://box:8080")
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", lambda handle, preamble=None: fake)
    host = _make_host(
        tmp_path,
        aliases={"default": "main"},
        exec_env=SandboxExecEnvConfig(base_url="http://box:8080", workdir="/c/ws"),
    )
    seed = host.resolve_engine_for_agent("main")
    assert seed._tools["read"].exec_env is fake
    # The driving resolve reuses the cached Engine → same backend, not Local.
    again = host.resolve_engine_for_agent("main")
    assert again is seed


# --------------------------------------------------------------------------- #
# Client wiring + shutdown teardown
# --------------------------------------------------------------------------- #


def test_client_threads_provider_and_reaps_on_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", _recording_factory())
    provider = FakeProvider()
    ws = tmp_path / "ws"
    ws.mkdir()
    options = Options(
        system_prompt="finish",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )
    client = Client(
        options,
        provider=_stub_provider(),
        workspace_dir=ws,
        host_config=HostConfig(sandbox_provider=provider),
    )
    try:
        assert client._host.sandbox_provider is provider
        assert client._host._sandbox is not None
        # provision a session container so shutdown has something to reap
        client._host.allocate_exec_env("task-root", str(ws))
        assert provider.allocated
    finally:
        client.shutdown()
    assert provider.released == ["task-root"]
