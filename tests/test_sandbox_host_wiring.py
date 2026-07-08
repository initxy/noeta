"""T5 — host-layer sandbox manager: provision + route into build_session_inputs.

T4 made the sandbox backend *reachable* (``build_session_inputs(exec_env=...)``);
T5 makes it *run*: the host reads ``HostConfig.exec_env``, builds a live backend,
and threads it into every session's Engine so the fs / shell tools' IO lands in
the container instead of the host. Covered here:

* ``SandboxExecEnvManager`` — lazy single-build, shared instance, idempotent
  teardown, eager-vs-attach close ownership;
* ``SdkHost`` default (no config) is byte-identical (LocalExecEnv + host root);
* ``SdkHost`` with a config routes fs tools onto the container backend + a
  lexical container ``WorkspaceRoot`` rooted at the config's ``workdir``;
* an end-to-end ``read`` flowing through an injected backend;
* the SEED build (``task_id=None``, which shares the Engine cache with the first
  driving turn) is routed to the SAME backend — else the local backend would be
  silently pinned into the cached Engine and the sandbox bypassed;
* the Client threads ``HostConfig.exec_env`` in and reaps it on shutdown.

No socket is ever opened: the real ``AioSandboxExecEnv`` builds inert (it only
calls out when a method runs), and the end-to-end tests inject a fake factory.
"""

from __future__ import annotations

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
from noeta.client.sandbox import SandboxExecEnvManager
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
# a recording ExecEnv fake (mirrors tests/test_exec_env_wiring.py)
# --------------------------------------------------------------------------- #


class RecordingExecEnv:
    """A minimal :class:`ExecEnv` that records reads, canned-replies, and closes."""

    def __init__(self, files: Optional[dict[str, bytes]] = None) -> None:
        self.files = dict(files or {})
        self.reads: list[str] = []
        self.closed = 0

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

    def close(self) -> None:
        self.closed += 1


# --------------------------------------------------------------------------- #
# SandboxExecEnvManager
# --------------------------------------------------------------------------- #


def test_manager_builds_lazily_and_shares_one_instance() -> None:
    cfg = SandboxExecEnvConfig(base_url="http://box:8080")
    built: list[SandboxExecEnvConfig] = []

    def factory(c: SandboxExecEnvConfig) -> RecordingExecEnv:
        built.append(c)
        return RecordingExecEnv()

    mgr = SandboxExecEnvManager(cfg, factory=factory)
    assert built == []  # nothing built until first use
    env = mgr.exec_env()
    assert mgr.exec_env() is env  # shared across calls
    assert built == [cfg]  # built exactly once


def test_manager_workdir_is_config_workdir() -> None:
    cfg = SandboxExecEnvConfig(base_url="http://box:8080", workdir="/home/agent/ws")
    mgr = SandboxExecEnvManager(cfg, factory=lambda c: RecordingExecEnv())
    assert mgr.workdir == "/home/agent/ws"


def test_manager_teardown_is_idempotent_and_rebuilds() -> None:
    cfg = SandboxExecEnvConfig(base_url="http://box:8080")
    mgr = SandboxExecEnvManager(cfg, factory=lambda c: RecordingExecEnv())
    first = mgr.exec_env()
    mgr.teardown()
    mgr.teardown()  # idempotent — no raise on an already-empty manager
    assert mgr.exec_env() is not first  # a fresh backend after teardown


def test_manager_eager_closes_on_teardown() -> None:
    cfg = SandboxExecEnvConfig(base_url="http://box:8080", provision="eager")
    fake = RecordingExecEnv()
    mgr = SandboxExecEnvManager(cfg, factory=lambda c: fake)
    mgr.exec_env()
    mgr.teardown()
    assert fake.closed == 1


def test_manager_attach_never_closes() -> None:
    # "attach" reconnects to a container someone else owns — teardown must drop
    # the local handle WITHOUT stopping the shared container.
    cfg = SandboxExecEnvConfig(base_url="http://box:8080", provision="attach")
    fake = RecordingExecEnv()
    mgr = SandboxExecEnvManager(cfg, factory=lambda c: fake)
    mgr.exec_env()
    mgr.teardown()
    assert fake.closed == 0


def test_default_factory_builds_aio_adapter_without_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The production factory reads the key from the env at connect time (D5) and
    # builds the AIO adapter — inert until a method runs, so no socket opens.
    monkeypatch.setenv("SANDBOX_API_KEY", "s3cr3t")
    cfg = SandboxExecEnvConfig(base_url="http://box:8080/")
    env = sandbox_mod._default_factory(cfg)
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


def test_host_with_config_routes_fs_tools_to_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = RecordingExecEnv()
    monkeypatch.setattr(sandbox_mod, "_default_factory", lambda c: fake)
    host = _make_host(
        tmp_path,
        exec_env=SandboxExecEnvConfig(base_url="http://box:8080", workdir="/c/ws"),
    )
    assert host._sandbox is not None
    engine = _build(host, task_id="t1")
    for name in ("read", "write", "shell_run"):
        assert engine._tools[name].exec_env is fake
    # the fs root is the lexical CONTAINER workdir, not the host workspace_dir
    read = engine._tools["read"]
    assert read.workspace.lexical is True
    assert read.workspace.root == Path("/c/ws")


def test_host_with_config_uses_real_aio_adapter(tmp_path: Path) -> None:
    # No fake factory — the real one builds the AIO adapter (inert, no socket).
    host = _make_host(
        tmp_path,
        exec_env=SandboxExecEnvConfig(base_url="http://box:8080"),
    )
    engine = _build(host, task_id="t1")
    assert isinstance(engine._tools["read"].exec_env, AioSandboxExecEnv)


def test_read_flows_through_injected_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = RecordingExecEnv({"/c/ws/note.md": b"in-container"})
    monkeypatch.setattr(sandbox_mod, "_default_factory", lambda c: fake)
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
    # The seed Engine (task_id=None, built by resolve_engine_for_agent before a
    # task exists) shares the Engine cache with the first driving turn. If the
    # seed built the LOCAL backend, the cached Engine would pin it and the drive
    # would bypass the sandbox — so the seed must route to the SAME backend.
    fake = RecordingExecEnv()
    monkeypatch.setattr(sandbox_mod, "_default_factory", lambda c: fake)
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


def test_client_threads_config_and_reaps_on_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = RecordingExecEnv()
    monkeypatch.setattr(sandbox_mod, "_default_factory", lambda c: fake)
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
        host_config=HostConfig(
            exec_env=SandboxExecEnvConfig(base_url="http://box:8080", provision="eager")
        ),
    )
    try:
        assert client._host.exec_env is not None
        assert client._host._sandbox is not None
        # force the backend to exist so shutdown has something to reap
        client._host._sandbox.exec_env()
    finally:
        client.shutdown()
    assert fake.closed == 1
