"""A foreground ``Bash`` command in a sandbox container stops like a local one.

The container runs each command in its own shell session (``id`` + a
``hard_timeout`` on the exec), and ``AioSandboxExecEnv`` hands a terminator for
that session to ``Bash`` through ``run_argv(on_start=...)``. ``Bash`` registers
it in the session's foreground kill table, so interrupt / cancel / close reach
a command the host has no process for: the kill POSTs ``/v1/shell/kill`` with
the same id, the blocked call returns at once, and the result reads
*interrupted*, never a timeout.

Everything here runs against a fake transport; the live half is in
``test_sandbox_live_interrupt_and_read.py``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from noeta.builtins.fs.impl.shell import ShellRunTool
from noeta.builtins.sandbox.impl.exec_env import AioSandboxExecEnv
from noeta.execution.driver import InteractionDriver
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.background_shell import ProcessRegistry
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from tests.test_background_shell_lifetime import _end_turn, _make_host


BASE = "http://sandbox.local:8080"


def _envelope(data: Optional[dict[str, Any]], *, success: bool = True) -> bytes:
    return json.dumps({"success": success, "message": "m", "data": data}).encode()


class BlockingAio:
    """A fake container: ``/v1/shell/exec`` blocks until the session it names
    is killed (then answers the way the real image does — only at its hard
    timeout, which here means "released by the test") or until ``finish`` is
    called. Records every POST."""

    def __init__(self, *, exec_answer: Optional[dict[str, Any]] = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self._lock = threading.Lock()
        self.exec_started = threading.Event()
        self.killed_ids: list[str] = []
        self._release = threading.Event()
        self._exec_answer = exec_answer

    def finish(self) -> None:
        self._release.set()

    def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: float
    ) -> bytes:
        path = url[len(BASE):]
        parsed = json.loads(body)
        with self._lock:
            self.calls.append((path, parsed, timeout_s))
        if path == "/v1/shell/sessions/create":
            return _envelope({"session_id": parsed["id"], "working_dir": "/w"})
        if path == "/v1/shell/kill":
            with self._lock:
                self.killed_ids.append(parsed["id"])
            return _envelope({"status": "terminated", "exit_code": None})
        if path == "/v1/shell/exec":
            self.exec_started.set()
            if self._exec_answer is not None:
                return _envelope(self._exec_answer)
            # The real image keeps the exec open after a kill until
            # ``hard_timeout``; the adapter must not wait for it.
            self._release.wait(30)
            return _envelope(
                {"session_id": parsed["id"], "status": "hard_timeout",
                 "output": "", "exit_code": -1}
            )
        raise AssertionError(f"unexpected call to {path!r}")

    def bodies(self, path: str) -> list[dict[str, Any]]:
        with self._lock:
            return [b for p, b, _ in self.calls if p == path]


def _env(fake: BlockingAio) -> AioSandboxExecEnv:
    return AioSandboxExecEnv(base_url=BASE, post=fake)


def _tool_ctx(
    fake: BlockingAio, root: Path, task_id: str = "t-sbx"
) -> tuple[ShellRunTool, ToolContext, ProcessRegistry]:
    store = InMemoryContentStore()
    registry = ProcessRegistry(event_log=InMemoryEventLog(), content_store=store)
    tool = ShellRunTool(
        workspace=WorkspaceRoot.from_path(root),
        mode=ShellMode.ARBITRARY,
        exec_env=_env(fake),
    )
    ctx = ToolContext(
        artifact_store=store,
        metadata={"task_id": task_id, "trace_id": "tr"},
        background_runner=registry,
    )
    return tool, ctx, registry


def _foreground(registry: ProcessRegistry) -> dict[str, Any]:
    with registry._lock:  # noqa: SLF001 — observe the live kill table
        return dict(registry._foreground)  # noqa: SLF001


def _await(pred: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


def _invoke_in_thread(
    tool: ShellRunTool, ctx: ToolContext, command: str
) -> tuple[threading.Thread, list[ToolResult], list[float]]:
    out: list[ToolResult] = []
    ended: list[float] = []

    def _run() -> None:
        out.append(tool.invoke({"command": command, "timeout": 60_000}, ctx))
        ended.append(time.monotonic())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, out, ended


# -- wire: session id + hard timeout ------------------------------------------ #


def test_exec_runs_in_its_own_session_with_hard_timeout() -> None:
    fake = BlockingAio(
        exec_answer={"status": "completed", "output": "hi", "exit_code": 0}
    )
    outcome = _env(fake).run_argv(
        ["echo", "hi"], cwd=Path("/w"), timeout_s=42, output_cap=1000
    )
    assert outcome.returncode == 0 and outcome.stdout == b"hi"
    paths = [p for p, _, _ in fake.calls]
    assert paths == ["/v1/shell/sessions/create", "/v1/shell/exec"]
    (create,) = fake.bodies("/v1/shell/sessions/create")
    (body,) = fake.bodies("/v1/shell/exec")
    assert body["id"] == create["id"] and body["id"]
    assert body["hard_timeout"] == 42
    # A quiet command must not be cut short before its real deadline.
    assert body["no_change_timeout"] > 42
    # The transport waits a little longer than the container's own deadline.
    exec_timeout = [t for p, _, t in fake.calls if p == "/v1/shell/exec"][0]
    assert exec_timeout > 42
    # A normal finish kills nothing (a server started with ``&`` survives,
    # as it does locally).
    assert fake.killed_ids == []


def test_each_run_gets_a_fresh_session() -> None:
    fake = BlockingAio(
        exec_answer={"status": "completed", "output": "", "exit_code": 0}
    )
    env = _env(fake)
    for _ in range(2):
        env.run_argv(["true"], cwd=Path("/w"), timeout_s=5, output_cap=10)
    ids = [b["id"] for b in fake.bodies("/v1/shell/exec")]
    assert len(set(ids)) == 2


@pytest.mark.parametrize("status", ["hard_timeout", "no_change_timeout"])
def test_cut_short_exec_is_a_timeout_and_kills_the_session(status: str) -> None:
    fake = BlockingAio(
        exec_answer={"status": status, "output": "partial", "exit_code": -1}
    )
    outcome = _env(fake).run_argv(
        ["sleep", "99"], cwd=Path("/w"), timeout_s=3, output_cap=1000
    )
    assert outcome.timed_out is True
    assert outcome.stdout == b"partial"
    (body,) = fake.bodies("/v1/shell/exec")
    # ``hard_timeout`` alone does not reliably end the process: kill it.
    assert fake.killed_ids == [body["id"]]


def test_transport_timeout_is_a_timeout_and_kills_the_session() -> None:
    calls: list[str] = []

    def post(url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: float) -> bytes:
        path = url[len(BASE):]
        calls.append(path)
        if path == "/v1/shell/exec":
            raise TimeoutError("read timed out")
        return _envelope({})

    outcome = AioSandboxExecEnv(base_url=BASE, post=post).run_argv(
        ["sleep", "99"], cwd=Path("/w"), timeout_s=3, output_cap=1000
    )
    assert outcome.timed_out is True
    assert calls == [
        "/v1/shell/sessions/create", "/v1/shell/exec", "/v1/shell/kill"
    ]


def test_session_create_fault_is_a_failed_run() -> None:
    def post(url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: float) -> bytes:
        return _envelope(None, success=False)

    outcome = AioSandboxExecEnv(base_url=BASE, post=post).run_argv(
        ["true"], cwd=Path("/w"), timeout_s=3, output_cap=1000
    )
    assert outcome.returncode == -1 and not outcome.timed_out


def test_on_start_kill_returns_run_argv_promptly() -> None:
    fake = BlockingAio()
    kills: list[Any] = []
    result: list[Any] = []

    def _run() -> None:
        result.append(
            _env(fake).run_argv(
                ["sleep", "99"], cwd=Path("/w"), timeout_s=60, output_cap=10,
                on_start=kills.append,
            )
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert fake.exec_started.wait(5)
    kills[0]()
    thread.join(2)
    fake.finish()
    assert not thread.is_alive(), "run_argv kept waiting on the exec after a kill"
    (outcome,) = result
    assert outcome.returncode == -1 and outcome.timed_out is False
    (body,) = fake.bodies("/v1/shell/exec")
    assert fake.killed_ids == [body["id"]]


def test_kill_before_exec_skips_the_exec() -> None:
    fake = BlockingAio()
    outcome = _env(fake).run_argv(
        ["sleep", "99"], cwd=Path("/w"), timeout_s=60, output_cap=10,
        on_start=lambda kill: kill(),
    )
    assert outcome.returncode == -1 and not outcome.timed_out
    assert fake.bodies("/v1/shell/exec") == []
    (create,) = fake.bodies("/v1/shell/sessions/create")
    assert fake.killed_ids == [create["id"]]


def test_kill_after_finish_never_raises() -> None:
    def post(url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: float) -> bytes:
        path = url[len(BASE):]
        if path == "/v1/shell/kill":
            raise ConnectionError("container gone")
        if path == "/v1/shell/exec":
            return _envelope({"status": "completed", "output": "", "exit_code": 0})
        return _envelope({})

    kills: list[Any] = []
    AioSandboxExecEnv(base_url=BASE, post=post).run_argv(
        ["true"], cwd=Path("/w"), timeout_s=5, output_cap=10, on_start=kills.append
    )
    kills[0]()  # late, and the transport faults: still silent


def test_backend_advertises_foreground_kill() -> None:
    assert _env(BlockingAio()).supports_foreground_kill is True


# -- Bash + the session kill table -------------------------------------------- #


def test_session_kill_interrupts_a_sandbox_foreground_run(tmp_path: Path) -> None:
    fake = BlockingAio()
    tool, ctx, registry = _tool_ctx(fake, tmp_path)
    thread, out, ended = _invoke_in_thread(tool, ctx, "sleep 30")
    assert fake.exec_started.wait(5)
    _await(lambda: bool(_foreground(registry).get("t-sbx")))
    stopped_at = time.monotonic()
    registry.kill_root_task("t-sbx")
    thread.join(5)
    fake.finish()
    assert not thread.is_alive()
    assert ended[0] - stopped_at < 2.0
    (result,) = out
    assert result.success is False
    assert result.summary == "Command interrupted (stop requested)"
    (body,) = fake.bodies("/v1/shell/exec")
    _await(lambda: fake.killed_ids == [body["id"]])
    assert _foreground(registry) == {}


def test_sandbox_run_that_finishes_unregisters_and_reads_ok(tmp_path: Path) -> None:
    fake = BlockingAio(
        exec_answer={"status": "completed", "output": "done", "exit_code": 0}
    )
    tool, ctx, registry = _tool_ctx(fake, tmp_path)
    result = tool.invoke({"command": "echo done"}, ctx)
    assert result.success and result.output == "done"
    assert _foreground(registry) == {}
    assert fake.killed_ids == []


def test_sandbox_timeout_reads_timed_out_not_interrupted(tmp_path: Path) -> None:
    fake = BlockingAio(
        exec_answer={"status": "hard_timeout", "output": "", "exit_code": -1}
    )
    tool, ctx, _ = _tool_ctx(fake, tmp_path)
    result = tool.invoke({"command": "sleep 99", "timeout": 2000}, ctx)
    assert result.success is False
    assert result.summary == "Command timed out after 2s"


def test_without_a_registry_the_sandbox_run_still_works(tmp_path: Path) -> None:
    fake = BlockingAio(
        exec_answer={"status": "completed", "output": "x", "exit_code": 0}
    )
    tool = ShellRunTool(
        workspace=WorkspaceRoot.from_path(tmp_path),
        mode=ShellMode.ARBITRARY,
        exec_env=_env(fake),
    )
    result = tool.invoke({"command": "echo x"}, ToolContext(artifact_store=InMemoryContentStore()))
    assert result.success and result.output == "x"


# -- the registry with a kill callable ---------------------------------------- #


def _registry() -> ProcessRegistry:
    return ProcessRegistry(
        event_log=InMemoryEventLog(), content_store=InMemoryContentStore()
    )


def test_registry_kill_callable_marks_killed_and_runs_off_thread() -> None:
    reg = _registry()
    called = threading.Event()
    caller: list[str] = []

    def kill() -> None:
        caller.append(threading.current_thread().name)
        called.set()

    handle = reg.register_foreground(kill=kill, spawned_by_task_id="root")
    reg.kill_root_task("root")
    assert called.wait(5)
    assert caller[0] != threading.current_thread().name
    assert reg.unregister_foreground(handle) is True
    # Idempotent: a second unregister answers from the handle's own mark.
    assert reg.unregister_foreground(handle) is True
    assert _foreground(reg) == {}


def test_registry_kill_callable_that_raises_is_swallowed() -> None:
    reg = _registry()
    ran = threading.Event()

    def kill() -> None:
        ran.set()
        raise RuntimeError("boom")

    handle = reg.register_foreground(kill=kill, spawned_by_task_id="root")
    reg.kill_root_task("root")
    assert ran.wait(5)
    assert reg.unregister_foreground(handle) is True


def test_registry_unkilled_callable_handle_reads_not_killed() -> None:
    reg = _registry()
    handle = reg.register_foreground(kill=lambda: None, spawned_by_task_id="root")
    reg.kill_root_task("other")
    assert reg.unregister_foreground(handle) is False


def test_registry_purge_then_unregister_answers_from_mark() -> None:
    reg = _registry()
    handle = reg.register_foreground(kill=lambda: None, spawned_by_task_id="root")
    reg.kill_root_task("root")
    reg.purge_root_task("root")
    assert reg.unregister_foreground(handle) is True


def test_registry_requires_exactly_one_of_popen_or_kill() -> None:
    reg = _registry()
    with pytest.raises(ValueError):
        reg.register_foreground(spawned_by_task_id="root")


@pytest.mark.parametrize("verb", ["interrupt", "cancel", "close"])
def test_session_stop_verbs_reach_a_kill_callable(tmp_path: Path, verb: str) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _make_host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    task_id = driver.start(goal="kick off", agent="main").task_id
    reg = host._process_registry  # noqa: SLF001 — reach the wired registry
    assert reg is not None
    called = threading.Event()
    handle = reg.register_foreground(kill=called.set, spawned_by_task_id=task_id)
    try:
        getattr(driver, verb)(task_id)
        assert called.wait(5), f"{verb} did not reach the sandbox kill"
    finally:
        killed = reg.unregister_foreground(handle)
    assert killed is True
