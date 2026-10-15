"""``HostConfig.shell_env``: variables a host hands one task's shell commands.

The scrubbed environment is process-wide; a host that runs many sessions in one
process (one per job) still needs to tell each session's commands which job
they belong to. The resolver is keyed by the ROOT task, asked per command,
reaches foreground and background runs alike, and fails soft. Also here: a
closed conversation is not woken by a background completion notice.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from noeta.builtins.fs.impl.shell import ShellRunTool
from noeta.client import SdkHost
from noeta.core.fold import fold
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext
from noeta.runtime.background_shell import ProcessRegistry
from noeta.runtime.exec_env import LocalExecEnv
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.subproc import RunOutcome
from noeta.runtime.workspace import FsWriteMode, WorkspaceRoot
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from tests._sdk_session import official_registry as official_agent_registry

ECHO = f"{sys.executable} -c \"import os; print(os.environ.get('JOB', '<unset>'))\""


def _tool(tmp_path: Path, resolver: Any, **kwargs: Any) -> ShellRunTool:
    return ShellRunTool(
        workspace=WorkspaceRoot.from_path(tmp_path),
        mode=ShellMode.ARBITRARY,
        shell_env=resolver,
        **kwargs,
    )


def _ctx(metadata: dict[str, Any], **kwargs: Any) -> ToolContext:
    return ToolContext(
        artifact_store=InMemoryContentStore(), metadata=metadata, **kwargs
    )


def test_foreground_command_sees_the_root_tasks_variables(tmp_path: Path) -> None:
    asked: list[str] = []

    def resolver(root: str) -> Mapping[str, str]:
        asked.append(root)
        return {"JOB": f"job-of-{root}"}

    result = _tool(tmp_path, resolver).invoke(
        {"command": ECHO}, _ctx({"task_id": "child", "root_task_id": "root"})
    )
    assert result.success
    assert result.output.strip() == "job-of-root"
    assert asked == ["root"]


def test_without_a_root_the_task_itself_is_asked(tmp_path: Path) -> None:
    result = _tool(tmp_path, lambda root: {"JOB": root}).invoke(
        {"command": ECHO}, _ctx({"task_id": "only"})
    )
    assert result.output.strip() == "only"


def test_a_broken_resolver_or_bad_entries_never_fail_the_command(
    tmp_path: Path,
) -> None:
    def broken(root: str) -> Mapping[str, str]:
        raise RuntimeError("boom")

    result = _tool(tmp_path, broken).invoke(
        {"command": ECHO}, _ctx({"task_id": "t"})
    )
    assert result.success and result.output.strip() == "<unset>"

    bad: Any = {"JOB=x; rm -rf": "a", "OK": 1, 3: "b", "NUL": "a\x00b"}
    result = _tool(tmp_path, lambda root: bad).invoke(
        {"command": f"{sys.executable} -c \"import os; print(sorted(k for k in os.environ if k in ('OK', 'NUL')))\""},
        _ctx({"task_id": "t"}),
    )
    assert result.output.strip() == "[]"


def test_no_resolver_adds_nothing(tmp_path: Path) -> None:
    result = _tool(tmp_path, None).invoke({"command": ECHO}, _ctx({"task_id": "t"}))
    assert result.output.strip() == "<unset>"


class _RecordingEnv(LocalExecEnv):
    """A backend that may or may not declare the capability."""

    def __init__(self, supports: bool) -> None:
        self._supports = supports
        self.seen: Optional[dict[str, Any]] = None

    @property
    def supports_extra_env(self) -> bool:
        return self._supports

    def run_argv(self, argv: list[str], **kwargs: Any) -> RunOutcome:  # type: ignore[override]
        self.seen = kwargs
        return super().run_argv(argv, **kwargs)


def test_a_backend_without_the_capability_is_never_handed_the_variables(
    tmp_path: Path,
) -> None:
    without = _RecordingEnv(supports=False)
    _tool(tmp_path, lambda root: {"JOB": "x"}, exec_env=without).invoke(
        {"command": "true"}, _ctx({"task_id": "t"})
    )
    assert without.seen is not None and "extra_env" not in without.seen

    with_it = _RecordingEnv(supports=True)
    _tool(tmp_path, lambda root: {"JOB": "x"}, exec_env=with_it).invoke(
        {"command": "true"}, _ctx({"task_id": "t"})
    )
    assert with_it.seen is not None and with_it.seen["extra_env"] == {"JOB": "x"}


def test_background_command_sees_the_variables(tmp_path: Path) -> None:
    store = InMemoryContentStore()
    registry = ProcessRegistry(event_log=InMemoryEventLog(), content_store=store)
    result = _tool(tmp_path, lambda root: {"JOB": f"bg-{root}"}).invoke(
        {"command": ECHO, "run_in_background": True},
        ToolContext(
            artifact_store=store,
            metadata={"task_id": "t", "root_task_id": "t"},
            background_runner=registry,
        ),
    )
    assert result.success
    job_id = result.output.split("ID: ")[1].split()[0]
    deadline = time.monotonic() + 5.0
    seen = ""
    while time.monotonic() < deadline:
        polled = registry.poll(job_id)
        seen += str(polled.get("new_output", ""))
        if polled.get("status") != "running":
            break
        time.sleep(0.02)
    assert "bg-t" in seen


# -- end to end through the host -----------------------------------------------


def _bash(call_id: str, command: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name="Bash", arguments={"command": command})],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _host(
    workspace: Path, responses: list[LLMResponse], **kwargs: Any
) -> tuple[SdkHost, InMemoryEventLog, InMemoryContentStore]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        workspace_dir=workspace,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ARBITRARY,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
        **kwargs,
    )
    return host, event_log, content_store


def test_host_shell_env_reaches_a_sessions_command(tmp_path: Path) -> None:
    host, event_log, content_store = _host(
        tmp_path,
        [_bash("c1", ECHO), _end_turn()],
        shell_env=lambda root: {"JOB": f"job-of-{root}"},
    )
    driver = InteractionDriver(host)
    outcome = driver.start(goal="go", agent="main")
    # ``Bash`` is gated by default; the approved call is the one that runs
    driver.approve(outcome.task_id, call_id="c1")
    task = fold(event_log, content_store, outcome.task_id)
    outputs = [
        block.output
        for message in task.runtime.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert outputs and str(outputs[0]).strip() == f"job-of-{outcome.task_id}"


def test_a_closed_conversation_is_not_woken_by_a_background_exit(
    tmp_path: Path,
) -> None:
    host, event_log, content_store = _host(
        tmp_path, [_end_turn("hi"), _end_turn("woken")]
    )
    driver = InteractionDriver(host)
    host.set_background_notifier(driver)
    task_id = driver.start(goal="kick off", agent="main").task_id
    driver.close(task_id)
    users = len([m for m in fold(event_log, content_store, task_id).runtime.messages if m.role == "user"])

    registry = host._process_registry  # noqa: SLF001 — the wired registry
    assert registry is not None
    job = registry.spawn(
        argv=[sys.executable, "-c", "print('late')"],
        cwd=tmp_path,
        env={},
        command="python late",
        spawned_by_task_id=task_id,
        trace_id="tr",
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and registry.poll(job["job_id"]).get("status") == "running":
        time.sleep(0.02)
    time.sleep(0.5)  # the delivery thread's window

    task = fold(event_log, content_store, task_id)
    assert len([m for m in task.runtime.messages if m.role == "user"]) == users
    assert task.status == "suspended"
    assert any(env.type == "BackgroundShellExited" for env in event_log.read(task_id))


# -- HostConfig.shell_env_inherit: host variables past the fixed allow-list --

PROXY_ECHO = (
    f"{sys.executable} -c \"import os; "
    "print(os.environ.get('HTTPS_PROXY', '<unset>'), os.environ.get('JOB', '<unset>'))\""
)


def test_inherited_names_reach_the_command_from_this_process(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:1")
    plain = _tool(tmp_path, None).invoke({"command": PROXY_ECHO}, _ctx({}))
    assert plain.output.split() == ["<unset>", "<unset>"]
    widened = _tool(tmp_path, None, inherit_env=("HTTPS_PROXY", "NOT_SET_HERE")).invoke(
        {"command": PROXY_ECHO}, _ctx({})
    )
    assert widened.output.split() == ["http://proxy:1", "<unset>"]


def test_a_task_variable_wins_over_an_inherited_one(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("JOB", "from-host")
    result = _tool(
        tmp_path, lambda root: {"JOB": "from-task"}, inherit_env=("JOB",)
    ).invoke({"command": ECHO}, _ctx({"task_id": "t"}))
    assert result.output.strip() == "from-task"


def test_host_config_shell_env_inherit_reaches_a_sessions_command(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:2")
    host, event_log, content_store = _host(
        tmp_path,
        [_bash("c1", PROXY_ECHO), _end_turn()],
        shell_env_inherit=("HTTPS_PROXY",),
    )
    driver = InteractionDriver(host)
    outcome = driver.start(goal="go", agent="main")
    driver.approve(outcome.task_id, call_id="c1")
    task = fold(event_log, content_store, outcome.task_id)
    outputs = [
        block.output
        for message in task.runtime.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert outputs and str(outputs[0]).split()[0] == "http://proxy:2"
