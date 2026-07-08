"""T6 — durable ``exec_env_ref`` weld/fold + multi-machine sandbox reconnect.

T5 made a host route its sessions into a container; T6 makes "which container"
durable so a resumed / **reclaimed** session — possibly on another host whose
config default differs — reconnects to the SAME container. The mechanism mirrors
``workspace_dir`` exactly: welded onto ``TaskHostBound`` at session open, folded
into ``governance.exec_env_ref``, read by the resolver, threaded into the Engine
cache key + build. Covered here:

* the payload / fold round-trip (byte-equal when ``None``);
* ``seed_start`` welds the host's container address into ``TaskHostBound``;
* the acceptance criterion — a task folded on ANOTHER host (different config
  base_url) resolves an Engine whose fs backend targets the RECORDED address,
  not the folding host's config;
* the cache-key dimension — two sessions on different containers never share an
  Engine (their fs tools target different backends);
* a non-sandbox session records no ref (byte-equal) and stays local.

The real container is gated (``NOETA_TEST_AIO_SANDBOX_URL``); here a fake factory
records the base_url each backend is built against and opens no socket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import pytest

import noeta.client.sandbox as sandbox_mod
from noeta.client.host import SdkHost
from noeta.client.host_config import SandboxExecEnvConfig
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs._subprocess import _RunOutcome

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

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None) -> _RunOutcome:
        return _RunOutcome(0, 1, b"", b"", False, False, False)


def _recording_factory(seen: list[str]):
    """A backend factory that records each built base_url (opens no socket)."""

    def factory(config: SandboxExecEnvConfig) -> _FakeEnv:
        seen.append(config.base_url)
        return _FakeEnv(config.base_url)

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


def _host(triple, workspace: Path, *, exec_env=None, responses=None) -> SdkHost:
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
        policy_wrapper=multi_turn_policy_wrapper,
        exec_env=exec_env,
    )


def _task_host_bound_ref(event_log, task_id: str) -> Optional[str]:
    for env in event_log.read(task_id):
        if env.type == "TaskHostBound":
            return getattr(env.payload, "exec_env_ref", None)
    return None


# --------------------------------------------------------------------------- #
# weld + fold
# --------------------------------------------------------------------------- #


def test_seed_start_welds_and_folds_exec_env_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_factory", _recording_factory([]))
    ws = tmp_path / "ws"
    ws.mkdir()
    triple = _triple()
    host = _host(
        triple, ws, exec_env=SandboxExecEnvConfig(base_url="http://A:1111")
    )
    out = InteractionDriver(host, default_model=None).start(goal="hi", agent="main")
    # welded on the durable TaskHostBound...
    assert _task_host_bound_ref(triple[0], out.task_id) == "http://A:1111"
    # ...and folded into governance.
    task = fold(triple[0], triple[1], out.task_id)
    assert task.governance.exec_env_ref == "http://A:1111"


def test_non_sandbox_session_records_no_ref(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    triple = _triple()
    host = _host(triple, ws)  # no exec_env config
    out = InteractionDriver(host, default_model=None).start(goal="hi", agent="main")
    assert _task_host_bound_ref(triple[0], out.task_id) is None
    task = fold(triple[0], triple[1], out.task_id)
    assert task.governance.exec_env_ref is None


# --------------------------------------------------------------------------- #
# multi-machine reconnect (the acceptance criterion)
# --------------------------------------------------------------------------- #


def test_reclaim_on_another_host_reconnects_to_recorded_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    triple = _triple()

    # Host A binds the session to container A and drives one turn.
    seen_a: list[str] = []
    monkeypatch.setattr(sandbox_mod, "_default_factory", _recording_factory(seen_a))
    host_a = _host(
        triple, ws, exec_env=SandboxExecEnvConfig(base_url="http://A:1111")
    )
    out = InteractionDriver(host_a, default_model=None).start(goal="hi", agent="main")
    assert seen_a == ["http://A:1111"]  # A only ever built its own container

    # Another process/host (SAME event log, DIFFERENT config default) folds the
    # task and re-resolves its Engine — it must reconnect to the RECORDED
    # container A, not host B's own config default B.
    seen_b: list[str] = []
    monkeypatch.setattr(sandbox_mod, "_default_factory", _recording_factory(seen_b))
    host_b = _host(
        triple, ws, exec_env=SandboxExecEnvConfig(base_url="http://B:2222")
    )
    task = fold(triple[0], triple[1], out.task_id)
    engine = host_b.resolve_engine(task)
    # B built a backend for the recorded address A — never for its own default.
    assert seen_b == ["http://A:1111"]
    assert engine._tools["read"].exec_env.base_url == "http://A:1111"


# --------------------------------------------------------------------------- #
# cache-key dimension
# --------------------------------------------------------------------------- #


def test_exec_env_ref_keys_the_engine_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_default_factory", _recording_factory([]))
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _host(
        _triple(), ws, exec_env=SandboxExecEnvConfig(base_url="http://A:1111")
    )
    e_x1 = host.resolve_engine_for_agent("main", exec_env_ref="http://x:1")
    e_x2 = host.resolve_engine_for_agent("main", exec_env_ref="http://x:1")
    e_y = host.resolve_engine_for_agent("main", exec_env_ref="http://y:2")
    assert e_x1 is e_x2  # same ref → cached Engine reused
    assert e_x1 is not e_y  # different container → distinct Engine
    assert e_x1._tools["read"].exec_env.base_url == "http://x:1"
    assert e_y._tools["read"].exec_env.base_url == "http://y:2"
