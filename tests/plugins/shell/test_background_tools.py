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
from noeta.core.events import Cancel, ExtensionRecorded, External, Say
from noeta.core.tool import KeptClaim, Tool, ToolContext, ToolOutput
from noeta.core.types import Text
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.shell.jobs import JOB_ENDED, Job
from noeta.plugins.shell.notices import format_duration, looks_like_prompt, monitor_expired_note
from noeta.plugins.shell.tools import STOP_JOB
from noeta.toolkit.background_tasks import STOP_RUN
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
                   dedupe_key: str | None = None) -> None:
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
    assert text(await stop(h, "nope12345")) == "<tool_use_error>No task found with ID: nope12345</tool_use_error>"
    assert text(await h.call("TaskStop")) == "<tool_use_error>TaskStop requires a non-empty 'task_id'</tool_use_error>"
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
        f"<tool_use_error>Task {job_id} is not running (status: killed)</tool_use_error>")
    done = await h.call("Bash", command="true", run_in_background=True)
    await h.settle(done.meta["job_id"])
    finished = await stop(h, done.meta["job_id"])
    assert text(finished) == (
        f"<tool_use_error>Task {done.meta['job_id']} is not running (status: completed)</tool_use_error>")


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
    # Another task has its own allowance.
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
    # Claude Code's: stderr makes no event and no part of the end notice; it
    # is in the output file the notice names, with stdout.
    out_file = h.job(job_id).out_path
    assert texts[-1] == (f"<task-notification>\n<task-id>{job_id}</task-id>\n"
                         "<tool-use-id>call_1</tool-use-id>\n"
                         f"<output-file>{out_file}</output-file>\n<status>completed</status>\n"
                         "<summary>Monitor \"watch\" stream ended</summary>\n"
                         "</task-notification>")
    assert Path(out_file).read_text() == "a\nb\noops\n"


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
        "<tool_use_error>Monitor requires non-empty 'command' (or 'ws')</tool_use_error>")
    assert text(await h.call("Monitor", command="true")) == (
        "<tool_use_error>Monitor requires non-empty 'description'</tool_use_error>")


async def test_a_jobs_notices_go_to_the_task_that_started_it(h: Harness) -> None:
    out = await h.call("Bash", task_id="child", root_id="root", command="sleep 0.2",
                       run_in_background=True)
    await h.settle(out.meta["job_id"])
    assert [t for t, _ in h.posts] == ["child"]


async def test_kill_all_is_quiet(h: Harness) -> None:
    out = await h.call("Bash", command="sleep 30", run_in_background=True)
    h.registry.kill_all("task")
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


async def test_a_running_foreground_command_shows_its_latest_output(h: Harness) -> None:
    bash = h.tool("Bash")
    bash.progress_after, bash.progress_every = 0.05, 0.05  # type: ignore[attr-defined]
    shown: list[str] = []
    ctx = h.ctx()
    ctx.progress = shown.append
    out = await bash.run({"command": "echo one; sleep 0.3; echo two; sleep 0.3"}, ctx)
    assert text(out) == "one\ntwo"
    assert "one\n" in shown and shown[-1] == "one\ntwo\n"
    bash.progress_after = 30.0  # type: ignore[attr-defined]
    quick: list[str] = []
    ctx = h.ctx()
    ctx.progress = quick.append
    await bash.run({"command": "echo fast"}, ctx)
    assert quick == []  # done before the first showing


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


# -- the task's claim, kept while a job runs ------------------------------------


def _ctx_keeping(h: Harness, kept: list[KeptClaim]) -> ToolContext:
    def keep() -> KeptClaim:
        claim = KeptClaim(_release=lambda k: kept.remove(k))
        kept.append(claim)
        return claim

    ctx = h.ctx()
    ctx.keep = keep
    return ctx


async def test_a_background_job_keeps_the_claim_until_its_end_is_posted(h: Harness) -> None:
    kept: list[KeptClaim] = []
    out = await h.tool("Bash").run({"command": "sleep 0.2", "run_in_background": True},
                                   _ctx_keeping(h, kept))
    assert len(kept) == 1
    await h.settle(out.meta["job_id"])
    assert kept == [] and len(h.posts) == 1


async def test_a_job_dies_quietly_when_the_kept_claim_is_lost(h: Harness) -> None:
    kept: list[KeptClaim] = []
    out = await h.tool("Bash").run({"command": "sleep 30", "run_in_background": True},
                                   _ctx_keeping(h, kept))
    [claim] = kept
    claim.mark_lost()
    job = h.job(out.meta["job_id"])
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.status == "killed" and job.fenced and h.posts == []


async def test_a_command_moved_to_the_background_keeps_the_claim(h: Harness) -> None:
    kept: list[KeptClaim] = []
    out = await h.tool("Bash").run({"command": "echo hi; sleep 2.5", "timeout": 1000},
                                   _ctx_keeping(h, kept))
    assert not out.is_error and "moved to the background" in text(out)
    assert len(kept) == 1
    await h.settle(out.meta["job_id"])
    assert kept == []


async def test_a_command_that_ends_in_time_keeps_nothing(h: Harness) -> None:
    kept: list[KeptClaim] = []
    out = await h.tool("Bash").run({"command": "echo hi", "timeout": 1000},
                                   _ctx_keeping(h, kept))
    assert text(out) == "hi" and kept == [] and h.records == []


async def test_task_stop_asks_a_running_child_to_stop_its_job(h: Harness) -> None:
    posted: list[tuple[Any, str | None, str | None]] = []

    async def post(body: Any, task_id: str | None = None,
                   dedupe_key: str | None = None) -> None:
        posted.append((body, task_id, dedupe_key))

    async def tasks(**_kw: Any) -> list[Any]:
        row = type("Row", (), {"task_id": "kid", "status": "running"})
        return [row()]

    async def read_log(task_id: str, after: int = 0) -> list[Any]:
        started = ExtensionRecorded(extension="shell", kind="started", data={
            "job_id": "kidjob", "command": "make", "description": "build it"})
        return [type("Rec", (), {"event": started})()] if task_id == "kid" else []

    ctx = ToolContext(task_id="task", call_id="c", env=LocalExecEnv(str(h.tmp)), state={},
                      record=lambda *_: None, post=post, tasks=tasks, read_log=read_log)
    out = await h.tool("TaskStop").run({"task_id": "kidjob"}, ctx)
    assert json.loads(text(out))["message"] == "Successfully stopped task: kidjob (build it)"
    assert posted == [(External(name=STOP_JOB, data={"job_id": "kidjob"}), "kid",
                       f"{STOP_JOB}:kidjob")]
    assert (await h.tool("TaskStop").run({"task_id": "other"}, ctx)).is_error


async def test_progress_that_fails_never_fails_the_command(h: Harness) -> None:
    bash = h.tool("Bash")
    bash.progress_after, bash.progress_every = 0.05, 0.05  # type: ignore[attr-defined]
    ctx = h.ctx()

    def broken(_text: str) -> None:
        raise RuntimeError("a front end went away")

    ctx.progress = broken
    out = await bash.run({"command": "echo one; sleep 0.3; echo two"}, ctx)
    assert not out.is_error and text(out) == "one\ntwo"


@pytest.mark.parametrize(("command", "status", "summary"), [
    ("grep -q needle /dev/null", "completed",
     'Background command "x" completed (exit code 1: No matches found)'),
    ("git -C . diff --quiet --no-index /dev/null t.txt", "completed",
     'Background command "x" completed (exit code 1: Files differ)'),
    ("test -e /nonexistent", "completed",
     'Background command "x" completed (exit code 1: Condition is false)'),
    # After ``&&`` status 1 may be the earlier command's: Claude Code calls it failed.
    ("true && grep -q needle /dev/null", "failed",
     'Background command "x" failed with exit code 1'),
    ("grep -q needle /nonexistent", "failed", 'Background command "x" failed with exit code 2'),
    ("false", "failed", 'Background command "x" failed with exit code 1'),
])
async def test_a_background_search_that_finds_nothing_completed(
        h: Harness, command: str, status: str, summary: str) -> None:
    (h.tmp / "t.txt").write_text("t\n")
    out = await h.call("Bash", command=command, description="x", run_in_background=True)
    await h.settle(out.meta["job_id"])
    notice = h.posts[-1][1]
    assert f"<status>{status}</status>" in notice
    assert f"<summary>{summary}</summary>" in notice


async def test_a_background_command_stuck_at_a_prompt_is_reported_once(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"), stall_check_s=0.05, stall_after_s=0.2)
    h = Harness(tmp_path, registry, Shell(registry=registry))
    out = await h.call("Bash", command="echo working; printf 'Overwrite? (y/n) '; sleep 30",
                       description="copy", run_in_background=True)
    job_id = out.meta["job_id"]
    for _ in range(200):
        if h.posts:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)  # no second notice
    assert len(h.posts) == 1
    notice = h.posts[0][1]
    assert notice.startswith(
        f"<task-notification>\n<task-id>{job_id}</task-id>\n<tool-use-id>call_1</tool-use-id>"
        f"\n<output-file>{h.job(job_id).out_path}</output-file>\n<summary>Background command "
        '"copy" appears to be waiting for interactive input</summary>\n</task-notification>'
        "\nLast output:\nworking\nOverwrite? (y/n)\n")
    await stop(h, job_id)
    await h.settle(job_id)


async def test_a_quiet_command_or_monitor_without_a_prompt_is_not_reported(
        tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"), stall_check_s=0.05, stall_after_s=0.1)
    h = Harness(tmp_path, registry, Shell(registry=registry))
    bash = await h.call("Bash", command="echo building; sleep 30", run_in_background=True)
    monitor = await h.call("Monitor", command="printf 'Continue? '; sleep 30",
                           description="m", timeout_ms=60_000)
    await asyncio.sleep(0.5)
    assert h.posts == []
    for out in (bash, monitor):
        await stop(h, out.meta["job_id"])
        await h.settle(out.meta["job_id"])


@pytest.mark.parametrize(("output", "prompt"), [
    ("Proceed (y/n)", True), ("Do you want to continue? ", True), ("Press Enter to go", True),
    ("Overwrite?", True), ("compiling 3/9", False), ("Done?\nnext line", False)])
def test_what_counts_as_a_prompt(output: str, prompt: bool) -> None:
    assert looks_like_prompt(output) is prompt


async def test_task_stop_stops_this_tasks_background_agents_and_workflow_runs(
        h: Harness) -> None:
    # Claude Code's TaskStop stops any background task: an agent by its agent
    # ID (a resumed one runs in a newer task), a Workflow run by its ID.
    posted: list[tuple[Any, str | None, str | None]] = []
    rows = {"t2": "running", "t3": "ended"}

    async def post(body: Any, task_id: str | None = None,
                   dedupe_key: str | None = None) -> None:
        posted.append((body, task_id, dedupe_key))

    async def task(task_id: str) -> Any:
        status = rows.get(task_id)
        return None if status is None else type("Row", (), {"status": status})()

    state = {
        "subtasks": {"agents": {
            "a1": {"task_id": "t2", "description": "review the diff", "type": "Explore"},
            "a3": {"task_id": "t3", "description": "done already", "type": "Explore"}}},
        "workflow": {"runs": {
            "wf_live01": {"status": "running", "name": "audit", "description": "Audit it"},
            "wf_over01": {"status": "completed", "name": "old", "description": "Old"}}},
    }
    ctx = ToolContext(task_id="task", call_id="c", env=LocalExecEnv(str(h.tmp)), state=state,
                      record=lambda *_: None, post=post, task=task, root_id="task")
    stop_tool = h.tool("TaskStop")

    async def stop_any(task_id: str) -> ToolOutput:
        return await stop_tool.run({"task_id": task_id}, ctx)

    out = await stop_any("a1")
    assert json.loads(text(out)) == {
        "message": "Successfully stopped task: a1 (review the diff)", "task_id": "a1",
        "task_type": "local_agent", "command": "review the diff"}
    body, target, _ = posted.pop()
    assert isinstance(body, Cancel) and target == "t2"
    assert json.loads(text(await stop_any("t2")))["task_type"] == "local_agent"
    posted.clear()
    assert text(await stop_any("a3")) == "<tool_use_error>Task a3 is not running (status: ended)</tool_use_error>"
    out = await stop_any("wf_live01")
    assert json.loads(text(out)) == {
        "message": "Successfully stopped task: wf_live01 (Audit it)", "task_id": "wf_live01",
        "task_type": "local_workflow", "command": "Audit it"}
    [(body, target, key)] = posted
    assert body == External(name=STOP_RUN, data={"run_id": "wf_live01"})
    assert target is None and key == f"{STOP_RUN}:wf_live01"
    assert text(await stop_any("wf_over01")) == (
        "<tool_use_error>Task wf_over01 is not running (status: completed)</tool_use_error>")
    # An unknown ID names the agents still running, as Claude Code does.
    assert text(await stop_any("zzz")) == (
        "<tool_use_error>No task found with ID: zzz. Running background agents: a1 (review the diff)</tool_use_error>")
