"""The background-shell tools driven directly: launch texts, BashOutput,
KillShell, Monitor events and expiry, the job cap, and where notices go."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from noeta.events import Say
from noeta.execenv.local import LocalExecEnv
from noeta.ext.background_shell import BackgroundShells, Registry
from noeta.ext.background_shell.jobs import Job
from noeta.ext.background_shell.notices import format_duration, monitor_expired_note
from noeta.tools.base import Tool, ToolContext, ToolOutput
from noeta.types import Text


@dataclass
class Harness:
    tmp: Path
    registry: Registry
    ext: BackgroundShells
    posts: list[tuple[str, str]] = field(default_factory=list)
    records: list[tuple[str, str, Any]] = field(default_factory=list)
    calls: int = 0

    def tool(self, name: str) -> Tool:
        return next(t for t in self.ext._tools if t.name == name)

    async def post(self, body: Any, task_id: str | None = None) -> None:
        assert isinstance(body, Say) and body.when == "now"
        assert body.message.origin == "system"
        self.posts.append((task_id or "task", body.message.text))

    def ctx(self, task_id: str = "task", root_id: str = "task") -> ToolContext:
        self.calls += 1
        return ToolContext(task_id=task_id, call_id=f"call_{self.calls}",
                           env=LocalExecEnv(str(self.tmp)), state={},
                           record=lambda e, k, d: self.records.append((e, k, d)),
                           post=self.post, root_id=root_id)

    async def call(self, name: str, task_id: str = "task", root_id: str = "task",
                   **input: Any) -> ToolOutput:
        return await self.tool(name).run(input, self.ctx(task_id, root_id))

    def job(self, job_id: str) -> Job:
        job = self.registry.get(job_id)
        assert job is not None
        return job

    async def settle(self, job_id: str) -> None:
        job = self.job(job_id)
        await asyncio.wait_for(job.done.wait(), 10)
        assert job.sender is not None
        await asyncio.wait_for(job.sender, 10)


def text(out: ToolOutput) -> str:
    return "".join(b.text for b in out.content if isinstance(b, Text))


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry(log_dir=str(tmp_path / "logs"))
    return Harness(work, registry, BackgroundShells(registry=registry))


async def test_background_launch_and_end_notice(h: Harness) -> None:
    out = await h.call("Bash", command="echo hi; exit 3", run_in_background=True)
    assert not out.is_error
    job_id = out.meta["job_id"]
    assert text(out) == (
        f"Command running in background with ID: {job_id}. You will be notified when it "
        "completes. To check interim output, use BashOutput on that ID.")
    assert h.records[0][:2] == ("background_shell", "started")
    await h.settle(job_id)
    assert h.posts == [("task", (
        f"<task-notification>\n<task-id>{job_id}</task-id>\n<tool-use-id>call_1</tool-use-id>"
        "\n<status>failed</status>\n<summary>Background command \"echo hi; exit 3\" failed "
        "with exit code 3</summary>\n</task-notification>\nLast output:\nhi"))]


async def test_ends_with_turn_wording_and_cd_note(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"))
    h = Harness(tmp_path, registry,
                BackgroundShells(registry=registry, background_ends_with_turn=True))
    out = await h.call("Bash", command="cd /; true", run_in_background=True,
                       description="go root")
    assert "it is terminated when you give your final response" in text(out)
    assert text(out).endswith(
        "\n" + f"Session cwd remains {tmp_path}; directory changes made by the backgrounded "
        "command do not apply to subsequent commands.")
    await h.settle(out.meta["job_id"])
    assert 'Background command "go root" completed (exit code 0)' in h.posts[0][1]


async def test_a_leading_sleep_is_fine_in_the_background_only(h: Harness) -> None:
    fg = await h.call("Bash", command="sleep 30")
    assert fg.is_error and "Blocked: standalone sleep 30." in text(fg)
    bg = await h.call("Bash", command="sleep 30", run_in_background=True)
    assert not bg.is_error
    await h.call("KillShell", shell_id=bg.meta["job_id"])
    await h.settle(bg.meta["job_id"])


async def test_bash_output_returns_only_new_output(h: Harness) -> None:
    out = await h.call("Bash", command="echo one; echo two", run_in_background=True)
    job_id = out.meta["job_id"]
    first = await h.call("BashOutput", bash_id=job_id, block=True, timeout=5000)
    assert text(first) == "status: exited (exit code 0)\none\ntwo\n"
    again = await h.call("BashOutput", bash_id=job_id)
    assert text(again) == "status: exited (exit code 0)\n(no new output)"
    await h.settle(job_id)


async def test_bash_output_filter_wait_timeout_and_errors(h: Harness) -> None:
    out = await h.call("Bash", command="echo keep; echo drop; sleep 5", run_in_background=True)
    job_id = out.meta["job_id"]
    polled = await h.call("BashOutput", bash_id=job_id, block=True, timeout=300,
                          filter="^keep")
    assert text(polled) == "status: running (still running after waiting 0.3s)\nkeep"
    assert text(await h.call("BashOutput", bash_id="bg-nope")) == (
        "BashOutput: unknown background job 'bg-nope'")
    assert text(await h.call("BashOutput", bash_id=job_id, filter="(")).startswith(
        "BashOutput: invalid 'filter' regex:")
    assert text(await h.call("BashOutput")) == "BashOutput: requires a non-empty 'bash_id'"
    killed = await h.call("KillShell", shell_id=job_id)
    assert text(killed) == f"Shell {job_id} is being terminated (killing)"
    await h.settle(job_id)
    assert "<status>killed</status>" in h.posts[-1][1]
    assert 'Background command "echo keep; echo drop; sleep 5" was stopped' in h.posts[-1][1]
    again = await h.call("KillShell", shell_id=job_id)
    assert text(again) == f"Shell {job_id} is being terminated (killed)"


async def test_poll_reports_dropped_output(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"), output_cap=10)
    h = Harness(tmp_path, registry, BackgroundShells(registry=registry))
    out = await h.call("Bash", command="printf 0123456789abcdef", run_in_background=True)
    polled = await h.call("BashOutput", bash_id=out.meta["job_id"], block=True)
    assert text(polled) == (
        "status: exited (exit code 0)\n[6 bytes of older output dropped (buffer overflow)]"
        "\n6789abcdef")
    await h.settle(out.meta["job_id"])


async def test_the_job_cap_refuses_a_ninth_job(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"), max_jobs=1)
    h = Harness(tmp_path, registry, BackgroundShells(registry=registry))
    first = await h.call("Bash", command="sleep 5", run_in_background=True)
    second = await h.call("Bash", command="true", run_in_background=True)
    assert text(second) == ("Bash: too many background jobs (1/1); kill one with "
                            "KillShell first")
    # Another root has its own allowance.
    other = await h.call("Bash", task_id="r2", root_id="r2", command="true",
                         run_in_background=True)
    assert not other.is_error
    await h.call("KillShell", shell_id=first.meta["job_id"])
    for out in (first, other):
        await h.settle(out.meta["job_id"])


async def test_monitor_streams_lines_then_reports_the_end(h: Harness) -> None:
    out = await h.call("Monitor", command="echo a; echo b; sleep 0.6; echo oops >&2",
                       description="watch", timeout_ms=60_000)
    job_id = out.meta["job_id"]
    assert text(out).startswith(f"Monitor started (task {job_id}, expires in 1m unless")
    await h.settle(job_id)
    texts = [t for _, t in h.posts]
    assert texts[0] == (f"<task-notification>\n<task-id>{job_id}</task-id>\n"
                        "<summary>Monitor event: \"watch\"</summary>\n<event>a\nb</event>\n"
                        "</task-notification>")
    assert texts[-1] == (f"<task-notification>\n<task-id>{job_id}</task-id>\n"
                         "<tool-use-id>call_1</tool-use-id>\n<status>completed</status>\n"
                         "<summary>Monitor \"watch\" stream ended</summary>\n"
                         "<event>stderr:\noops</event>\n</task-notification>")


async def test_monitor_expiry_sends_one_note_and_no_end_notice(h: Harness) -> None:
    out = await h.call("Monitor", command="sleep 30", description="idle", timeout_ms=1000)
    job_id = out.meta["job_id"]
    await h.settle(job_id)
    assert [t for _, t in h.posts] == [
        f"<task-notification>\n<task-id>{job_id}</task-id>\n"
        "<summary>Monitor event: \"idle\"</summary>\n"
        f"<event>{monitor_expired_note(1000, 0)}</event>\n</task-notification>"]


async def test_monitor_needs_command_and_description(h: Harness) -> None:
    assert text(await h.call("Monitor", description="x")) == (
        "Monitor: requires non-empty 'command'")
    assert text(await h.call("Monitor", command="true")) == (
        "Monitor: requires non-empty 'description'")


async def test_a_finished_spawners_notices_go_to_the_root(h: Harness) -> None:
    out = await h.call("Bash", task_id="child", root_id="root", command="sleep 0.2",
                       run_in_background=True)
    h.registry.ended.add("child")
    await h.settle(out.meta["job_id"])
    assert [t for t, _ in h.posts] == ["root"]


async def test_kill_where_is_quiet(h: Harness) -> None:
    out = await h.call("Bash", command="sleep 30", run_in_background=True)
    h.registry.kill_where("task")
    await h.settle(out.meta["job_id"])
    assert h.posts == []
    assert h.job(out.meta["job_id"]).status == "killed"


async def test_no_post_means_no_background(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"))
    ext = BackgroundShells(registry=registry)
    ctx = ToolContext(task_id="t", call_id="c", env=LocalExecEnv(str(tmp_path)), state={},
                      record=lambda *_: None)
    out = await ext.bash.run({"command": "true", "run_in_background": True}, ctx)
    assert out.is_error
    assert "background execution is not available on this host" in text(out)


def test_format_duration() -> None:
    assert format_duration(300_000) == "5m"
    assert format_duration(90_000) == "1m 30s"
    assert format_duration(1000) == "1s"
    assert format_duration(3_600_000) == "1h"
