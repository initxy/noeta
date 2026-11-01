"""A file a ``Bash`` command showed counts as read, as in Claude Code:
``Edit`` / ``Write`` take it as read, the changed-file note watches a
whole-file view, and the record is in the task's log for any worker."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, ToolFinished
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ModelRequest, ToolResult
from noeta.plugins.fs import FileTools
from noeta.plugins.fs.read import FILE_UNCHANGED
from noeta.plugins.shell import Shell
from noeta.testing import ScriptedProvider, reply

Step = tuple[str, dict[str, Any]]


async def _no_sleep(_: float) -> None:
    return None


def _touch(path: Path, text: str) -> None:
    path.write_text(text)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


class Formatter:
    """Rewrites a file behind the model's back."""

    name = "Format"
    description = "format"
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self, path: Path, text: str) -> None:
        self.path, self.text = path, text

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        _touch(self.path, self.text)
        return text_output("formatted")


class Scenario:
    def __init__(self, tmp: Path, *steps: Step, tools: list[Any] | None = None) -> None:
        self.work = tmp / "w"
        self.work.mkdir(exist_ok=True)
        self.db = str(tmp / "t.db")
        self.steps = steps
        self.tools = tools or []

    def runtime(self, store: SqliteStore, provider: ScriptedProvider) -> Runtime:
        agent = Agent(name="a", model="m", tools=self.tools, extensions=[FileTools(), Shell()])
        return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                       env_for=lambda _s: LocalExecEnv(str(self.work)), sleep=_no_sleep)

    async def go(self) -> tuple[list[ToolResult], ScriptedProvider, list[Any]]:
        store = SqliteStore(self.db)
        provider = ScriptedProvider([*(reply("", step) for step in self.steps), reply("done")])
        client = Client(store)
        tid = await client.create("a", "go")
        await self.runtime(store, provider).run_until_idle()
        events = [r.event for r in await client.events(tid)]
        await store.close()
        return [e.result for e in events if isinstance(e, ToolFinished)], provider, events


def _bash(command: str) -> Step:
    return ("Bash", {"command": command})


def _edit(path: Path, old: str, new: str) -> Step:
    return ("Edit", {"file_path": str(path), "old_string": old, "new_string": new})


def _reads(events: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in events if isinstance(e, ExtensionRecorded)
            and e.extension == "fs" and e.kind == "read"]


def _notes(request: ModelRequest) -> list[str]:
    return [m.text for m in request.messages
            if m.origin == "system" and m.text.startswith("Note: ")]


async def test_a_file_shown_by_cat_can_be_edited_without_a_read(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "a.txt"
    src.write_text("one\ntwo\n")
    run.steps = (_bash("cat a.txt"), _edit(src, "two", "2"))
    results, _, events = await run.go()
    assert [r.is_error for r in results] == [False, False], results[-1].text
    assert src.read_text() == "one\n2\n"
    # The whole file was in view: the edit's result says the file is current.
    assert "no need to Read it back" in results[1].text
    [shown, _written] = _reads(events)
    assert shown["path"] == str(src.resolve()) and "range" not in shown
    assert "whole" not in shown


async def test_a_piped_command_records_nothing(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "a.txt"
    src.write_text("one\ntwo\n")
    run.steps = (_bash("cat a.txt | head -n 5"), _edit(src, "two", "2"))
    results, _, events = await run.go()
    assert results[1].is_error and "Read the file first" in results[1].text
    assert _reads(events) == []


async def test_lines_shown_by_head_let_edit_through_as_a_view_of_part(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "long.txt"
    src.write_text("".join(f"line {n}\n" for n in range(1, 21)))
    run.steps = (_bash("head -n 3 long.txt"),
                 ("Read", {"file_path": str(src), "limit": 3}),
                 _edit(src, "line 15\n", "line fifteen\n"),
                 ("Write", {"file_path": str(run.work / "short.txt"), "content": "x\n"}))
    (run.work / "short.txt").write_text("a\nb\n")
    results, _, events = await run.go()
    assert [r.is_error for r in results] == [False, False, False, True]
    # The same lines read again are answered from the shell's output.
    assert results[1].text == FILE_UNCHANGED
    # Claude Code lets the edit through, without saying the file is in view.
    assert "no need to Read it back" not in results[2].text
    assert "line fifteen" in src.read_text()
    assert _reads(events)[0]["range"] == [1, 3]
    assert _reads(events)[0]["whole"] is False


async def test_head_of_a_shorter_file_is_a_whole_view(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "two.txt"
    src.write_text("a\nb")
    run.steps = (_bash("head -n 5 two.txt"), _edit(src, "b", "B"))
    results, _, events = await run.go()
    assert [r.is_error for r in results] == [False, False]
    assert "whole" not in _reads(events)[0]


async def test_a_grep_counts_only_when_it_matched(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "g.txt"
    src.write_text("alpha\nbeta\n")
    run.steps = (_bash("grep zeta g.txt"), _edit(src, "beta", "b"),
                 _bash("grep alpha g.txt"), _edit(src, "beta", "b"))
    results, _, events = await run.go()
    assert [r.is_error for r in results] == [False, True, False, False]
    [grep, _written] = _reads(events)
    assert grep["whole"] is False and "range" not in grep  # watched, not in view


async def test_a_command_that_failed_to_show_the_file_records_nothing(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "a.txt"
    src.write_text("a\n")
    run.steps = (_bash("sed -n 9p a.txt; cat missing.txt"),)
    _results, _, events = await run.go()
    assert _reads(events) == []


async def test_a_file_already_read_keeps_its_read(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "a.txt"
    src.write_text("a\nb\nc\n")
    run.steps = (("Read", {"file_path": str(src)}), _bash("head -n 1 a.txt"))
    _results, _, events = await run.go()
    assert [r.get("range") for r in _reads(events)] == [[1, 0]]


async def test_a_relative_path_is_taken_from_the_shells_directory(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    (run.work / "sub").mkdir()
    src = run.work / "sub" / "a.txt"
    src.write_text("a\n")
    run.steps = (_bash("cd sub"), _bash("cat -n a.txt"), _edit(src, "a", "A"))
    results, _, _ = await run.go()
    assert [r.is_error for r in results] == [False, False, False], results[-1].text


async def test_a_file_shown_by_cat_gets_the_changed_file_note(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "a.txt"
    lines = [f"x{n} = {n}" for n in range(1, 31)]
    src.write_text("\n".join(lines) + "\n")
    changed = list(lines)
    changed[9] = "x10 = 100"
    run.tools = [Formatter(src, "\n".join(changed) + "\n")]
    run.steps = (_bash("cat a.txt"), ("Format", {}), _edit(src, "x10 = 100", "x10 = 10"))
    results, provider, _ = await run.go()
    [note] = _notes(provider.requests[2])
    shown = [f"{n}\tx{n} = {100 if n == 10 else n}" for n in range(2, 19)]
    assert note.endswith("numbered as in the file now:\n" + "\n".join(shown))
    # With the changes shown the edit needs no new read, as after a Read.
    assert [r.is_error for r in results] == [False, False, False], results[-1].text


async def test_lines_shown_by_sed_get_no_changed_file_note(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "a.txt"
    src.write_text("a\nb\nc\n")
    run.tools = [Formatter(src, "a\nB\nc\n")]
    run.steps = (_bash("sed -n 1,2p a.txt"), ("Format", {}))
    _results, provider, _ = await run.go()
    assert _notes(provider.requests[2]) == []


async def test_the_record_serves_a_later_worker(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "a.txt"
    src.write_text("one\n")
    store = SqliteStore(run.db)
    client = Client(store)
    first = ScriptedProvider([reply("", _bash("cat a.txt")), reply("shown")])
    tid = await client.create("a", "show it")
    await run.runtime(store, first).run_until_idle()
    later = ScriptedProvider([reply("", _edit(src, "one", "1")), reply("done")])
    await client.send(tid, "edit it")
    await run.runtime(store, later).run_until_idle()
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    await store.close()
    assert [r.is_error for r in results] == [False, False], results[-1].text
    assert src.read_text() == "1\n"


async def test_a_file_whose_output_was_cut_counts_as_read_out_of_view(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    src = run.work / "big.txt"
    text = "".join(f"row {n:05d} " + "y" * 60 + "\n" for n in range(1000))
    src.write_text(text)
    (run.work / "big2.txt").write_text(text)
    run.steps = (_bash("cat big.txt"), _edit(src, "row 00500", "row five hundred"),
                 _bash("tail -n 900 big2.txt"))
    results, _, events = await run.go()
    assert [r.is_error for r in results] == [False, False, False], results[1].text
    assert "no need to Read it back" not in results[1].text
    whole, _written, lines = _reads(events)
    assert whole["whole"] is False and "range" not in whole
    # Some lines, not in view: not watched, and never answered as unchanged.
    assert lines["window"] is True and "range" not in lines
