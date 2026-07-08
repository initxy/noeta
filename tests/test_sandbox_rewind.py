"""T7 — rewind restore routes through the sandbox container's ExecEnv.

The capture half already flows through the container: an ``edit`` / ``write``
reads its pre-edit bytes via its own (sandbox) ExecEnv and surfaces them on
``file_changes`` — the ToolRuntime just persists those bytes, unchanged. The
RESTORE half — ``InteractionDriver._restore_files`` writing baselines back to
"disk" — used raw host pathlib; under a sandbox that disk is the CONTAINER, so
T7 routes the write-back through the session's ExecEnv (the recorded
``exec_env_ref``, T6) rooted at the container workdir. A local session keeps the
byte-identical host path. Covered:

* ``SdkHost.exec_env_for_ref`` resolves a sandbox session → (backend, root),
  and returns ``None`` for a local session / ``None`` ref / no host sandbox;
* ``_restore_files`` writes a restored baseline into the container backend,
  re-creating its parent dir, and deletes an AI-created file there;
* a local (no-ref) rewind still writes the host filesystem, untouched.

No socket opens: the backend is a recording fake injected via the factory.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
from noeta.execution.driver import InteractionDriver
from noeta.protocols.events import FileBaseline
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs._subprocess import _RunOutcome


# --------------------------------------------------------------------------- #
# a recording ExecEnv that behaves like an in-memory container filesystem
# --------------------------------------------------------------------------- #


class FakeContainerFs:
    def __init__(self, base_url: str, files: Optional[dict[str, bytes]] = None) -> None:
        self.base_url = base_url
        self.files: dict[str, bytes] = dict(files or {})
        self.writes: list[str] = []
        self.unlinks: list[str] = []
        self.mkdirs: list[str] = []

    def write_bytes(self, path: Path, body: bytes) -> None:
        self.writes.append(str(path))
        self.files[str(path)] = body

    def mkdir(self, path: Path) -> None:
        self.mkdirs.append(str(path))

    def unlink(self, path: Path) -> None:
        self.unlinks.append(str(path))
        self.files.pop(str(path), None)

    def exists(self, path: Path) -> bool:
        return str(path) in self.files

    # unused-by-restore ExecEnv surface (kept so it is a full backend)
    def read_bytes(self, path: Path) -> bytes:
        return self.files[str(path)]

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def create_exclusive(self, path: Path, body: bytes) -> None:
        self.files[str(path)] = body

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
        return _RunOutcome(0, 1, b"", b"", False, False, False)


def _spec() -> AgentSpec:
    return AgentSpec(
        name="main",
        instructions="main",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(ToolRef(name="read", risk_level="low", version="1"),),
        capabilities=Capabilities(),
        default_budget=BudgetSpec(max_iterations=20),
        metadata={},
    )


def _host(tmp_path: Path, **knobs: Any) -> SdkHost:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    ws = tmp_path / "ws"
    ws.mkdir()
    registry = AgentRegistry()
    registry.add(_spec())
    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="ok")],
                    usage=Usage(uncached=1, output=1),
                )
            ]
        ),
        model="stub-model",
        workspace_dir=ws,
        registry=registry,
        **knobs,
    )


def _tool_result_env(seq: int, baselines: list[FileBaseline]) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq,
        type="ToolResultRecorded",
        payload=SimpleNamespace(file_baselines=baselines),
    )


def _baseline_task(*, workspace: Optional[str], exec_env_ref: Optional[str]):
    return SimpleNamespace(
        governance=SimpleNamespace(workspace=workspace, exec_env_ref=exec_env_ref)
    )


# --------------------------------------------------------------------------- #
# exec_env_for_ref
# --------------------------------------------------------------------------- #


def test_exec_env_for_ref_resolves_sandbox_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeContainerFs("http://A:1111")
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", lambda handle: fake)
    host = _host(
        tmp_path,
        exec_env=SandboxExecEnvConfig(base_url="http://A:1111", workdir="/c/ws"),
    )
    got = host.exec_env_for_ref("http://A:1111")
    assert got is not None
    exec_env, root = got
    assert exec_env is fake
    assert root == Path("/c/ws")


def test_exec_env_for_ref_none_for_local_session(tmp_path: Path) -> None:
    # host WITHOUT a sandbox → always None (host FS restore).
    host = _host(tmp_path)
    assert host.exec_env_for_ref("http://A:1111") is None
    assert host.exec_env_for_ref(None) is None


def test_exec_env_for_ref_none_when_ref_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # sandbox host but a local session (no recorded ref) → host FS restore.
    monkeypatch.setattr(
        sandbox_mod, "_default_backend_factory", lambda handle: FakeContainerFs("http://A:1111")
    )
    host = _host(tmp_path, exec_env=SandboxExecEnvConfig(base_url="http://A:1111"))
    assert host.exec_env_for_ref(None) is None


# --------------------------------------------------------------------------- #
# _restore_files routes through the container
# --------------------------------------------------------------------------- #


def test_restore_writes_baseline_into_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeContainerFs("http://A:1111")
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", lambda handle: fake)
    host = _host(
        tmp_path,
        exec_env=SandboxExecEnvConfig(base_url="http://A:1111", workdir="/c/ws"),
    )
    ref = host.content_store.put(b"pre-edit bytes", media_type="text/plain")
    events = [_tool_result_env(5, [FileBaseline(path="pkg/mod.py", content_ref=ref)])]
    task = _baseline_task(workspace=None, exec_env_ref="http://A:1111")

    InteractionDriver._restore_files(host, events, keep_through=1, baseline_task=task)

    # the restore wrote the baseline back INTO the container, re-creating the dir,
    # and never touched the host filesystem.
    assert fake.writes == ["/c/ws/pkg/mod.py"]
    assert fake.mkdirs == ["/c/ws/pkg"]
    assert fake.files["/c/ws/pkg/mod.py"] == b"pre-edit bytes"


def test_restore_deletes_ai_created_file_in_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # content_ref=None → the AI created the file this turn → rewind DELETES it,
    # in the container.
    fake = FakeContainerFs("http://A:1111", files={"/c/ws/new.txt": b"created"})
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", lambda handle: fake)
    host = _host(
        tmp_path,
        exec_env=SandboxExecEnvConfig(base_url="http://A:1111", workdir="/c/ws"),
    )
    events = [_tool_result_env(5, [FileBaseline(path="new.txt")])]
    task = _baseline_task(workspace=None, exec_env_ref="http://A:1111")

    InteractionDriver._restore_files(host, events, keep_through=1, baseline_task=task)

    assert fake.unlinks == ["/c/ws/new.txt"]
    assert "/c/ws/new.txt" not in fake.files


def test_local_rewind_still_writes_host_fs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sandbox-configured host, but a session with NO recorded ref (local):
    # exec_env_for_ref → None → the byte-identical host-FS path runs, and the
    # container backend is never touched.
    fake = FakeContainerFs("http://A:1111")
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", lambda handle: fake)
    host = _host(tmp_path, exec_env=SandboxExecEnvConfig(base_url="http://A:1111"))
    ws = host.workspace_dir  # the host default root
    ref = host.content_store.put(b"host bytes", media_type="text/plain")
    events = [_tool_result_env(5, [FileBaseline(path="f.txt", content_ref=ref)])]
    task = _baseline_task(workspace=str(ws), exec_env_ref=None)

    InteractionDriver._restore_files(host, events, keep_through=1, baseline_task=task)

    assert (ws / "f.txt").read_bytes() == b"host bytes"
    assert fake.writes == []  # container backend untouched
