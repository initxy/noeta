"""Durable ``exec_env_ref`` — per-session container provision, reconnect, reap.

A host provisions a per-session container at ``seed_start`` and records which
one (``"{base_url}#{sandbox_id}"``) on ``TaskHostBound``, so a task reclaimed by
a different host — whose own sandbox config points somewhere else — reconnects
to the SAME container through ``provider.attach`` instead of provisioning a
fresh one and stranding the session's files. The ref rides the same route as
``workspace_dir``: welded at session open, folded into
``governance.exec_env_ref``, read by the resolver, and keyed into the Engine
cache so two sessions on different containers can never share an Engine.

No socket is opened: a fake provider mints handles and a fake backend factory
records the base_url each backend targets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import pytest

import noeta.client.sandbox as sandbox_mod
from noeta.client.host import SdkHost
from noeta.client.sandbox_provider import (
    SandboxHandle,
    SandboxSpec,
    StaticApiKeyAuth,
    decode_exec_env_ref,
)
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.values import ContentRef
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.subproc import RunOutcome

from tests._sdk_session import official_registry


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _FakeEnv:
    """A minimal :class:`ExecEnv` tagged with the base_url it was built for."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def read_bytes(self, path: Path) -> bytes:
        raise FileNotFoundError(str(path))

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        raise FileNotFoundError(str(path))

    def write_bytes(self, path: Path, body: bytes) -> None: ...
    def create_exclusive(self, path: Path, body: bytes) -> None: ...
    def unlink(self, path: Path) -> None: ...
    def exists(self, path: Path) -> bool:
        return False

    def is_file(self, path: Path) -> bool:
        return False

    def is_dir(self, path: Path) -> bool:
        return True

    def is_symlink(self, path: Path) -> bool:
        return False

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None) -> RunOutcome:
        return RunOutcome(0, 1, b"", b"", False, False, False)


class FakeProvider:
    """Mints a deterministic handle per allocate; attach decodes the ref."""

    def __init__(self, tag: str) -> None:
        self._tag = tag
        self._counter = 0
        self.allocated: list[str] = []
        self.released: list[str] = []

    def allocate(self, root_task_id: str, spec: SandboxSpec) -> SandboxHandle:
        self._counter += 1
        self.allocated.append(root_task_id)
        return SandboxHandle(
            base_url=f"http://{self._tag}-{self._counter}:8080",
            sandbox_id=f"sid-{self._tag}-{self._counter}",
            auth=StaticApiKeyAuth("SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def release(self, root_task_id: str) -> None:
        self.released.append(root_task_id)

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=sandbox_id,
            auth=StaticApiKeyAuth("SANDBOX_API_KEY"),
            workdir="/workspace",
        )


def _recording_factory(seen: list[str]) -> Any:
    def factory(handle: SandboxHandle, preamble: Any = None) -> _FakeEnv:
        seen.append(handle.base_url)
        return _FakeEnv(handle.base_url)

    return factory


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _triple():
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)
    return event_log, content_store, dispatcher


def _host(
    triple, workspace: Path, *, sandbox_provider=None, responses=None, multi_turn=True
) -> SdkHost:
    event_log, content_store, dispatcher = triple
    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses or [_end_turn()]),
        model="gpt-test",
        workspace_dir=workspace,
        registry=official_registry(),
        aliases={"default": "main"},
        policy_wrapper=multi_turn_policy_wrapper if multi_turn else None,
        sandbox_provider=sandbox_provider,
        sandbox_spec=SandboxSpec(image="img") if sandbox_provider else None,
    )


def _task_host_bound_ref(event_log, task_id: str) -> Optional[str]:
    for env in event_log.read(task_id):
        if env.type == "TaskHostBound":
            return getattr(env.payload, "exec_env_ref", None)
    return None


def _recorded_environment(event_log, content_store, task_id: str) -> Optional[str]:
    """The rendered ``<workspace-environment>`` block as the model saw it."""
    for env in event_log.read(task_id):
        if env.type != "ContextContentRecorded":
            continue
        if getattr(env.payload, "kind", "") != "environment":
            continue
        digest = getattr(env.payload, "content_hash", "")
        return content_store.get(
            ContentRef(hash=digest, size=0, media_type="text/markdown")
        ).decode("utf-8")
    return None


# --------------------------------------------------------------------------- #
# weld + fold
# --------------------------------------------------------------------------- #


def test_seed_start_allocates_welds_and_folds_exec_env_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", _recording_factory([]))
    ws = tmp_path / "ws"
    ws.mkdir()
    triple = _triple()
    provider = FakeProvider("A")
    host = _host(triple, ws, sandbox_provider=provider)
    out = InteractionDriver(host, default_model=None).start(goal="hi", agent="main")
    # a container was provisioned, keyed by the (pre-minted) root task id
    assert provider.allocated == [out.task_id]
    ref = _task_host_bound_ref(triple[0], out.task_id)
    assert ref == "http://A-1:8080#sid-A-1"  # encoded base_url#sandbox_id
    task = fold(triple[0], triple[1], out.task_id)
    assert task.governance.exec_env_ref == ref


def test_seed_records_the_environment_against_the_container_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seeded environment resident must describe the CONTAINER, not the host.

    ``seed_start`` resolves the Engine that drives the pre-loop content init off
    the Task ``create_task`` just returned. That Task carries the ``TaskHostBound``
    the same call emitted, so the resolve must see the session's workspace +
    container. Returned unfolded, it silently falls back to the host-fixed
    default dir with no ExecEnv, and the block names a host path the model
    cannot ``cd`` into — permanently, since the resident is activate-once.
    """
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", _recording_factory([]))
    ws = tmp_path / "session-ws"
    ws.mkdir()
    triple = _triple()
    host = _host(triple, tmp_path, sandbox_provider=FakeProvider("E"))
    out = InteractionDriver(host, default_model=None).start(
        goal="hi", agent="main", workspace_dir=str(ws)
    )
    block = _recorded_environment(triple[0], triple[1], out.task_id)
    assert block is not None, "no environment resident was recorded"
    assert "Working directory: /workspace\n" in block, block
    assert str(tmp_path) not in block, block


def test_non_sandbox_session_records_no_ref(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    triple = _triple()
    host = _host(triple, ws)  # no sandbox provider
    out = InteractionDriver(host, default_model=None).start(goal="hi", agent="main")
    assert _task_host_bound_ref(triple[0], out.task_id) is None
    task = fold(triple[0], triple[1], out.task_id)
    assert task.governance.exec_env_ref is None


def test_two_sessions_get_distinct_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", _recording_factory([]))
    ws = tmp_path / "ws"
    ws.mkdir()
    triple = _triple()
    host = _host(triple, ws, sandbox_provider=FakeProvider("A"))
    driver = InteractionDriver(host, default_model=None)
    a = driver.start(goal="hi", agent="main")
    b = driver.start(goal="hi", agent="main")
    ref_a = _task_host_bound_ref(triple[0], a.task_id)
    ref_b = _task_host_bound_ref(triple[0], b.task_id)
    assert ref_a != ref_b  # per-session isolation


# --------------------------------------------------------------------------- #
# multi-machine reconnect
# --------------------------------------------------------------------------- #


def test_reclaim_on_another_host_reconnects_to_recorded_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    triple = _triple()

    # Host A provisions container A and drives one turn.
    seen_a: list[str] = []
    monkeypatch.setattr(
        sandbox_mod, "_default_backend_factory", _recording_factory(seen_a)
    )
    host_a = _host(triple, ws, sandbox_provider=FakeProvider("A"))
    out = InteractionDriver(host_a, default_model=None).start(goal="hi", agent="main")
    recorded = _task_host_bound_ref(triple[0], out.task_id)
    assert seen_a == ["http://A-1:8080"]

    # Another process/host (SAME event log, DIFFERENT provider) folds the task
    # and re-resolves. It must attach to the RECORDED container A: provisioning
    # its own would hand the resumed task an empty workspace.
    seen_b: list[str] = []
    monkeypatch.setattr(
        sandbox_mod, "_default_backend_factory", _recording_factory(seen_b)
    )
    provider_b = FakeProvider("B")
    host_b = _host(triple, ws, sandbox_provider=provider_b)
    task = fold(triple[0], triple[1], out.task_id)
    engine = host_b.resolve_engine(task)
    assert provider_b.allocated == []  # B never provisioned — it attached
    assert seen_b == ["http://A-1:8080"]
    assert engine._tools["Read"].exec_env.base_url == "http://A-1:8080"
    assert recorded.startswith("http://A-1:8080")


# --------------------------------------------------------------------------- #
# cache-key dimension
# --------------------------------------------------------------------------- #


def test_exec_env_ref_keys_the_engine_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", _recording_factory([]))
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _host(_triple(), ws, sandbox_provider=FakeProvider("A"))
    e_x1 = host.resolve_engine_for_agent("main", exec_env_ref="http://x:1#s")
    e_x2 = host.resolve_engine_for_agent("main", exec_env_ref="http://x:1#s")
    e_y = host.resolve_engine_for_agent("main", exec_env_ref="http://y:2#s")
    assert e_x1 is e_x2  # same ref → cached Engine reused
    assert e_x1 is not e_y  # different container → distinct Engine
    assert e_x1._tools["Read"].exec_env.base_url == "http://x:1"
    assert e_y._tools["Read"].exec_env.base_url == "http://y:2"


# --------------------------------------------------------------------------- #
# lifecycle — release at root terminal
# --------------------------------------------------------------------------- #


def test_root_terminal_releases_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_backend_factory", _recording_factory([]))
    ws = tmp_path / "ws"
    ws.mkdir()
    triple = _triple()
    provider = FakeProvider("A")
    # A single-turn policy (no multi-turn wrapper) ends at a genuine terminal.
    host = _host(triple, ws, sandbox_provider=provider, multi_turn=False)
    out = InteractionDriver(host, default_model=None).start(goal="hi", agent="main")
    task = fold(triple[0], triple[1], out.task_id)
    assert task.status == "terminal"
    assert provider.allocated == [out.task_id]
    assert provider.released == [out.task_id]  # released at root terminal
