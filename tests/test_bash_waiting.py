"""Waiting on background work the way Claude Code does.

A long leading ``sleep`` is refused with Claude Code's ``Blocked:`` message
(threshold 25 s), ``BashOutput`` can block until a background command ends
(``block`` + ``timeout``, interruptible by a human stop), and the launch result
tells the model how it will learn of the end — a notice, which on a host whose
turn end hands the work back cannot follow its final response.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from noeta.builtins.fs.impl import ShellPollTool, ShellRunTool
from noeta.builtins.fs.impl.shell import long_leading_sleep
from noeta.protocols.tool import ToolContext
from noeta.runtime.background_shell import ProcessRegistry
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import WorkspaceRoot
from noeta.sdk import Client, HostConfig, Options
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider


# -- the leading-sleep refusal ---------------------------------------------


@pytest.mark.parametrize(
    ("command", "described"),
    [
        ("sleep 110", "standalone sleep 110"),
        ("sleep 25", "standalone sleep 25"),
        ("sleep 30.5", "standalone sleep 30.5"),
        ("sleep 600 && tail -5 out.log", "sleep 600 followed by: tail -5 out.log"),
        ("  sleep 120; cat a | grep b", "sleep 120 followed by: cat a grep b"),
        ("sleep 24", None),
        ("sleep 5 && make", None),
        ("echo hi && sleep 300", None),
        ("sleep 1m", None),
        ("until test -f done; do sleep 2; done", None),
        ("echo 'sleep 300; x'", None),
    ],
)
def test_long_leading_sleep_matches_claude_code(command: str, described: Any) -> None:
    assert long_leading_sleep(command) == described


def _bash(tmp_path: Path, **kwargs: Any) -> ShellRunTool:
    return ShellRunTool(
        workspace=WorkspaceRoot.from_path(tmp_path), mode=ShellMode.ARBITRARY, **kwargs
    )


def _ctx(**kwargs: Any) -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore(), **kwargs)


def test_a_long_leading_sleep_is_blocked_before_it_runs(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    result = _bash(tmp_path).invoke(
        {"command": f"sleep 110 && touch {marker}"}, _ctx()
    )
    assert not result.success
    assert result.summary == (
        f"Bash: Blocked: sleep 110 followed by: touch {marker}. To wait for a "
        "condition, use Monitor with an until-loop (e.g. `until <check>; do "
        "sleep 2; done`). To wait for a command you started, use "
        "run_in_background: true. Do not chain shorter sleeps to work around "
        "this block."
    )
    assert not marker.exists()


def test_a_short_sleep_and_a_backgrounded_one_still_run(tmp_path: Path) -> None:
    assert _bash(tmp_path).invoke({"command": "sleep 0.1 && echo ok"}, _ctx()).success
    store = InMemoryContentStore()
    registry = ProcessRegistry(event_log=InMemoryEventLog(), content_store=store)
    started = _bash(tmp_path).invoke(
        {"command": "sleep 30", "run_in_background": True},
        ToolContext(artifact_store=store, background_runner=registry, metadata={"task_id": "t"}),
    )
    assert started.success
    registry.kill_root_task("t")


# -- the launch result -------------------------------------------------------


def _launch(tmp_path: Path, **kwargs: Any) -> tuple[str, ProcessRegistry]:
    store = InMemoryContentStore()
    registry = ProcessRegistry(event_log=InMemoryEventLog(), content_store=store)
    started = _bash(tmp_path, **kwargs).invoke(
        {"command": "echo hi", "run_in_background": True},
        ToolContext(artifact_store=store, background_runner=registry, metadata={"task_id": "t"}),
    )
    assert started.success
    return str(started.output), registry


def test_launch_promises_a_notice_by_default(tmp_path: Path) -> None:
    output, _ = _launch(tmp_path)
    assert re.match(
        r"Command running in background with ID: bg-[0-9a-f]+\. You will be "
        r"notified when it completes\. To check interim output, use BashOutput "
        r"on that ID\.$",
        output,
    )


def test_launch_on_a_host_that_ends_jobs_with_the_turn(tmp_path: Path) -> None:
    output, _ = _launch(tmp_path, background_ends_with_turn=True)
    assert "If it exits while you are still working you will be notified" in output
    assert "terminated when you give your final response" in output
    assert "do not end your turn to wait for it" in output


def test_host_config_background_ends_with_turn_reaches_the_host(tmp_path: Path) -> None:
    provider = FakeLLMProvider(responses=[])
    with Client(
        Options(system_prompt="s"),
        provider=provider,
        workspace_dir=tmp_path,
        host_config=HostConfig(background_ends_with_turn=True),
    ) as client:
        assert client._host.background_ends_with_turn is True  # noqa: SLF001


# -- BashOutput block --------------------------------------------------------


def _job(tmp_path: Path, code: str) -> tuple[ProcessRegistry, ToolContext, str]:
    store = InMemoryContentStore()
    registry = ProcessRegistry(event_log=InMemoryEventLog(), content_store=store)
    ctx = ToolContext(
        artifact_store=store, background_runner=registry, metadata={"task_id": "t"}
    )
    started = _bash(tmp_path).invoke(
        {"command": f'{sys.executable} -c "{code}"', "run_in_background": True}, ctx
    )
    job = re.search(r"ID: ([\w-]+)", str(started.output))
    assert job is not None
    return registry, ctx, job.group(1)


def test_block_waits_for_the_command_to_finish(tmp_path: Path) -> None:
    registry, ctx, job = _job(tmp_path, "import time; time.sleep(0.4); print('done')")
    result = ShellPollTool().invoke({"bash_id": job, "block": True}, ctx)
    assert result.success
    assert str(result.output).startswith("status: exited (exit code 0)")
    assert "done" in str(result.output)


def test_block_gives_up_at_its_timeout(tmp_path: Path) -> None:
    registry, ctx, job = _job(tmp_path, "import time; time.sleep(30)")
    began = time.monotonic()
    result = ShellPollTool().invoke({"bash_id": job, "block": True, "timeout": 200}, ctx)
    assert time.monotonic() - began < 5
    assert str(result.output).startswith("status: running (still running after waiting 0.2s)")
    registry.kill_root_task("t")


def test_a_human_stop_ends_the_wait(tmp_path: Path) -> None:
    registry, ctx, job = _job(tmp_path, "import time; time.sleep(30)")
    threading.Timer(0.3, lambda: registry.kill_root_task("t")).start()
    began = time.monotonic()
    result = ShellPollTool().invoke(
        {"bash_id": job, "block": True, "timeout": 20000}, ctx
    )
    assert time.monotonic() - began < 10
    assert "(wait interrupted: stop requested)" in str(result.output) or (
        str(result.output).startswith("status: killed")
    )


def test_without_block_the_poll_answers_at_once(tmp_path: Path) -> None:
    registry, ctx, job = _job(tmp_path, "import time; time.sleep(30)")
    result = ShellPollTool().invoke({"bash_id": job}, ctx)
    assert str(result.output).startswith("status: running\n")
    registry.kill_root_task("t")


def test_registry_wait_answers_unknown_and_done(tmp_path: Path) -> None:
    registry, _, job = _job(tmp_path, "print(1)")
    assert registry.wait("bg-nope", timeout_s=0.1) == "unknown"
    assert registry.wait(job, timeout_s=5) == "done"


def test_a_runner_without_wait_serves_block_as_a_poll() -> None:
    class _Plain:
        def poll(self, job_id: str) -> dict[str, Any]:
            return {"status": "running", "new_output": ""}

    result = ShellPollTool().invoke(
        {"bash_id": "j", "block": True},
        ToolContext(artifact_store=InMemoryContentStore(), background_runner=_Plain()),  # type: ignore[arg-type]
    )
    assert str(result.output) == "status: running\n(no new output)"
