"""The background-shell tools driven directly: launch texts, the output
file, TaskStop, Monitor events and expiry, the job cap, and where notices
go."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.errors import HeldElsewhere
from noeta.core.events import External, Say
from noeta.core.holds import Release, TreeHold
from noeta.core.tool import Tool, ToolContext, ToolOutput
from noeta.core.types import Text
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.shell.jobs import JOB_ENDED, Job
from noeta.plugins.shell.notices import format_duration, monitor_expired_note
from noeta.plugins.shell.tools import HELD_ELSEWHERE
from noeta.toolkit.notices import NOTICE_GUARD


@dataclass
class Harness:
    tmp: Path
    registry: Registry
    ext: Shell
    posts: list[tuple[str, str]] = field(default_factory=list)
    records: list[tuple[str, str, Any]] = field(default_factory=list)
    calls: int = 0

    def tool(self, name: str) -> Tool:
        return next(t for t in self.ext._tools if t.name == name)

    async def post(self, body: Any, task_id: str | None = None,
                   dedupe_key: str | None = None, forward_if_ended: bool = False) -> None:
        assert forward_if_ended  # a spawner may end before its notice lands
        if isinstance(body, External):  # a job's end, posted once
            assert body.name == JOB_ENDED
            assert dedupe_key == f"{JOB_ENDED}:{body.data['job_id']}"
            message = body.data["notice"]
            if message is None:
                return  # stopped quietly
        else:
            assert isinstance(body, Say) and body.when == "now"
            assert body.message.origin == "system"
            message = body.message.text
        guard, _, notice = message.partition("\n\n<task-notification>")
        assert guard == NOTICE_GUARD  # every notice opens with the not-the-user guard
        self.posts.append((task_id or "task", "<task-notification>" + notice))

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
        return next(jobs[job_id] for jobs in self.registry.jobs.values() if job_id in jobs)

    async def settle(self, job_id: str) -> None:
        job = self.job(job_id)
        await asyncio.wait_for(job.done.wait(), 10)
        assert job.sender is not None
        await asyncio.wait_for(job.sender, 10)


async def stop(h: Harness, job_id: str) -> ToolOutput:
    return await h.tool("TaskStop").run({"task_id": job_id}, h.ctx())


def text(out: ToolOutput) -> str:
    return "".join(b.text for b in out.content if isinstance(b, Text))


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry(log_dir=str(tmp_path / "logs"))
    return Harness(work, registry, Shell(registry=registry))


async def test_background_launch_and_end_notice(h: Harness) -> None:
    out = await h.call("Bash", command="echo hi; exit 3", run_in_background=True)
    assert not out.is_error
    job_id = out.meta["job_id"]
    assert re.fullmatch(r"[a-z0-9]{9}", job_id)
    path = str(h.tmp.parent / "logs" / f"w{os.getpid()}" / f"{job_id}.output")
    assert text(out).startswith(
        f"Started in the background with ID {job_id}; its output is written to {path}. "
        "You will be notified when it finishes")
    assert h.records[0][:2] == ("shell", "started")
    await h.settle(job_id)
    assert h.posts == [("task", (
        f"<task-notification>\n<task-id>{job_id}</task-id>\n<tool-use-id>call_1</tool-use-id>"
        f"\n<output-file>{path}</output-file>"
        "\n<status>failed</status>\n<summary>Background command \"echo hi; exit 3\" failed "
        "with exit code 3</summary>\n</task-notification>"))]
    assert Path(path).read_text() == "hi\n\n[exited with code 3]\n"


async def test_ends_with_turn_wording_and_cd_note(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"))
    h = Harness(tmp_path, registry,
                Shell(registry=registry, background_ends_with_turn=True))
    out = await h.call("Bash", command="cd /; true", run_in_background=True,
                       description="go root")
    assert "it is stopped when you give your final response" in text(out)
    assert text(out).endswith(
        "\n" + f"The shell's directory stays {tmp_path}: a cd inside a background command does "
        "not carry over to later commands.")
    await h.settle(out.meta["job_id"])
    assert 'Background command "go root" completed (exit code 0)' in h.posts[0][1]


async def test_a_leading_sleep_is_fine_in_the_background_only(h: Harness) -> None:
    fg = await h.call("Bash", command="sleep 30")
    assert fg.is_error and "Blocked: standalone sleep 30." in text(fg)
    bg = await h.call("Bash", command="sleep 30", run_in_background=True)
    assert not bg.is_error
    res = await stop(h, bg.meta["job_id"])
    assert not res.is_error, text(res)
    await h.settle(bg.meta["job_id"])


async def test_task_stop_stops_a_job_and_sends_no_notice(h: Harness) -> None:
    # Claude Code sends no completion notification after a TaskStop.
    out = await h.call("Bash", command="echo keep; sleep 5", run_in_background=True)
    job_id = out.meta["job_id"]
    assert text(await stop(h, "nope12345")) == "TaskStop: No task found with ID: nope12345"
    assert text(await h.call("TaskStop")) == "TaskStop: requires a non-empty 'task_id'"
    stopped = await stop(h, job_id)
    assert not stopped.is_error
    assert json.loads(text(stopped)) == {
        "message": f"Successfully stopped task: {job_id} (echo keep; sleep 5)",
        "task_id": job_id, "task_type": "local_bash", "command": "echo keep; sleep 5"}
    posted = len(h.posts)
    await h.settle(job_id)
    assert len(h.posts) == posted
    again = await h.call("TaskStop", shell_id=job_id)
    assert again.is_error and text(again) == (
        f"TaskStop: Task {job_id} is not running (status: killed)")
    done = await h.call("Bash", command="true", run_in_background=True)
    await h.settle(done.meta["job_id"])
    finished = await stop(h, done.meta["job_id"])
    assert text(finished) == (
        f"TaskStop: Task {done.meta['job_id']} is not running (status: completed)")


def test_monitor_and_task_stop_are_deferred(h: Harness) -> None:
    assert getattr(h.tool("Monitor"), "deferred", False)
    assert getattr(h.tool("TaskStop"), "deferred", False)
    assert not getattr(h.tool("Bash"), "deferred", False)
    assert [t.name for t in h.ext._tools] == ["Bash", "Monitor", "TaskStop", "EnterWorktree",
                                             "ExitWorktree"]


async def test_the_job_cap_refuses_a_ninth_job(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"), max_jobs=1)
    h = Harness(tmp_path, registry, Shell(registry=registry))
    first = await h.call("Bash", command="sleep 5", run_in_background=True)
    second = await h.call("Bash", command="true", run_in_background=True)
    assert text(second) == ("Bash: too many background jobs (1/1); stop one with "
                            "TaskStop first")
    # Another root has its own allowance.
    other = await h.call("Bash", task_id="r2", root_id="r2", command="true",
                         run_in_background=True)
    assert not other.is_error
    await stop(h, first.meta["job_id"])
    for out in (first, other):
        await h.settle(out.meta["job_id"])


async def test_foreground_commands_do_not_count_toward_the_job_cap(tmp_path: Path) -> None:
    # A foreground command is waited on, not stoppable with TaskStop: it must
    # not take a background job's place.
    registry = Registry(log_dir=str(tmp_path / "logs"), max_jobs=1)
    h = Harness(tmp_path, registry, Shell(registry=registry))
    fg = asyncio.create_task(h.call("Bash", command="true; sleep 1"))
    for _ in range(100):
        if registry.jobs.get("task"):
            break
        await asyncio.sleep(0.01)
    assert registry.jobs.get("task")
    bg = await h.call("Bash", command="true", run_in_background=True)
    assert not bg.is_error, text(bg)
    await h.settle(bg.meta["job_id"])
    assert not (await fg).is_error


async def test_a_timeout_moves_the_command_to_the_background_past_the_job_cap(
        tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"), max_jobs=1)
    h = Harness(tmp_path, registry, Shell(registry=registry))
    first = await h.call("Bash", command="sleep 5", run_in_background=True)
    out = await h.call("Bash", command="echo started; sleep 2; echo done", timeout=1000)
    assert not out.is_error and "moved to the background" in text(out)
    await stop(h, first.meta["job_id"])
    for job in (first, out):
        await h.settle(job.meta["job_id"])


async def test_monitor_streams_lines_then_reports_the_end(h: Harness) -> None:
    out = await h.call("Monitor", command="echo a; echo b; sleep 0.6; echo oops >&2",
                       description="watch", timeout_ms=60_000)
    job_id = out.meta["job_id"]
    assert text(out).startswith(f"Monitor started with ID {job_id}; it expires in 1m unless")
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
        "Monitor: requires non-empty 'command' (or 'ws')")
    assert text(await h.call("Monitor", command="true")) == (
        "Monitor: requires non-empty 'description'")


async def test_a_finished_spawners_notices_go_to_the_root(h: Harness) -> None:
    out = await h.call("Bash", task_id="child", root_id="root", command="sleep 0.2",
                       run_in_background=True)
    h.registry.ended["child"] = None
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
    ext = Shell(registry=registry)
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


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_a_background_reading_git_is_hardened_too(tmp_path: Path) -> None:
    from tests.plugins.shell.test_bash import _planted_clone

    ws, markers = _planted_clone(tmp_path)
    registry = Registry(log_dir=str(tmp_path / "logs"))
    bg = Harness(ws, registry, Shell(registry=registry))
    for command in ("git -C sub diff HEAD~ HEAD", "cd sub && git log -p"):
        out = await bg.call("Bash", command=command, run_in_background=True)
        await bg.settle(out.meta["job_id"])
    assert [k for k, m in markers.items() if m.exists()] == []


# -- a foreground command past its timeout -----------------------------------


async def test_a_foreground_command_ends_quietly_and_leaves_nothing(h: Harness) -> None:
    (h.tmp / "sub").mkdir()
    out = await h.call("Bash", command="cd sub && echo hi && echo err >&2")
    assert not out.is_error and text(out) == "hi\nerr"
    assert out.meta["cwd"] == str(h.tmp / "sub")
    assert [r[:2] for r in h.records] == [("shell", "cwd")]
    failed = await h.call("Bash", command="echo no; exit 4")
    assert failed.is_error and text(failed) == "Exit code 4\nno"
    await asyncio.sleep(0.05)
    assert h.posts == [] and h.registry.jobs == {}
    assert list((h.tmp.parent / "logs" / f"w{os.getpid()}").iterdir()) == []


async def test_a_foreground_command_returns_when_its_shell_exits(h: Harness) -> None:
    # As Claude Code: a child left running with `&` (holding the output) does
    # not keep the call waiting.
    loop = asyncio.get_running_loop()
    start = loop.time()
    out = await h.call("Bash", command="echo before; sleep 5 & echo hi", timeout=20000)
    assert loop.time() - start < 3
    assert not out.is_error and text(out) == "before\nhi"
    assert h.registry.jobs == {} or all(not jobs for jobs in h.registry.jobs.values())


async def test_a_command_using_fd_3_keeps_its_file_and_the_directory(h: Harness) -> None:
    (h.tmp / "sub").mkdir()
    out = await h.call("Bash", command="exec 3>notes.txt; echo mine >&3; cd sub")
    assert not out.is_error
    assert (h.tmp / "notes.txt").read_text() == "mine\n"
    assert out.meta["cwd"] == str(h.tmp / "sub")


async def test_a_timeout_moves_the_command_to_the_background(h: Harness) -> None:
    out = await h.call("Bash", command="echo started; sleep 2; echo done", timeout=1000)
    assert not out.is_error
    job_id = out.meta["job_id"]
    path = str(h.tmp.parent / "logs" / f"w{os.getpid()}" / f"{job_id}.output")
    first, _, rest = text(out).partition("\n")
    assert first == "started"
    assert rest.startswith(
        "The command was still running when its 1s timeout ran out, so it was moved to the "
        f"background with ID {job_id}; its output goes on into {path}.")
    assert "printed nothing" not in rest
    assert [r[:2] for r in h.records] == [("shell", "started")]
    await h.settle(job_id)
    assert len(h.posts) == 1 and "<status>completed</status>" in h.posts[0][1]
    assert Path(path).read_text() == "started\ndone\n\n[exited with code 0]\n"


async def test_a_silent_moved_command_says_it_printed_nothing(h: Harness) -> None:
    out = await h.call("Bash", command="true; sleep 2", timeout=1000)
    assert text(out).startswith("The command was still running")
    assert "It had printed nothing yet" in text(out)
    await h.settle(out.meta["job_id"])


async def test_a_command_starting_with_sleep_is_killed_at_its_timeout(h: Harness) -> None:
    out = await h.call("Bash", command="sleep 3; echo late", timeout=1000)
    assert out.is_error and text(out).startswith("Command timed out after 1s")
    assert h.records == [] and h.registry.jobs == {}


# -- a task tree another worker holds ------------------------------------------


async def _held_elsewhere() -> Release:
    raise HeldElsewhere("held by another worker")


def _ctx_holding(h: Harness, hold: Any) -> ToolContext:
    ctx = h.ctx()
    ctx.hold = hold
    return ctx


async def test_no_background_job_starts_while_another_worker_holds_the_tree(h: Harness) -> None:
    ctx = _ctx_holding(h, _held_elsewhere)
    out = await h.tool("Bash").run({"command": "sleep 5", "run_in_background": True}, ctx)
    assert out.is_error and HELD_ELSEWHERE in text(out)
    assert h.records == [] and h.registry.jobs == {}


def test_the_refusal_names_both_reasons_a_tree_is_held_elsewhere() -> None:
    # A hold is refused for another worker's background job and, as well, for
    # another task of the tree running on another worker (the root, say).
    assert "background command or another task" in HELD_ELSEWHERE
    assert "in the foreground" in HELD_ELSEWHERE and "later" in HELD_ELSEWHERE


async def test_a_timeout_kills_the_command_while_another_worker_holds_the_tree(
        h: Harness) -> None:
    ctx = _ctx_holding(h, _held_elsewhere)
    out = await h.tool("Bash").run({"command": "echo started; sleep 3; echo late",
                                    "timeout": 1000}, ctx)
    assert out.is_error and text(out).startswith("Command timed out after 1s")
    assert "late" not in text(out)
    await asyncio.sleep(0.05)
    assert h.records == [] and h.registry.jobs == {}


async def test_a_timeout_kills_the_command_when_taking_the_hold_fails(h: Harness) -> None:
    async def broken_store() -> Release:
        raise OSError("the store is unreachable")

    ctx = _ctx_holding(h, broken_store)
    out = await h.tool("Bash").run({"command": "echo started; sleep 3; echo late",
                                    "timeout": 1000}, ctx)
    assert out.is_error and text(out).startswith("Command timed out after 1s")
    assert "late" not in text(out)
    await asyncio.sleep(0.05)
    assert h.records == [] and h.registry.jobs == {}


async def test_a_hold_taken_for_a_command_that_ended_meanwhile_is_given_back(
        h: Harness) -> None:
    released: list[str] = []

    async def slow_hold() -> Release:
        await asyncio.sleep(0.6)  # the command ends while the hold is taken

        async def give_back() -> None:
            released.append("hold")

        return Release(TreeHold(root_id="task", worker="w", epoch=1, seconds=60), give_back)

    ctx = _ctx_holding(h, slow_hold)
    out = await h.tool("Bash").run({"command": "echo hi; sleep 1.2", "timeout": 1000}, ctx)
    assert not out.is_error and text(out) == "hi"
    assert released == ["hold"] and h.records == []
