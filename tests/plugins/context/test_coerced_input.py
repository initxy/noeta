"""Claude Code's coercions reach the tool: a call that passes the schema check
only as a string runs with the converted value (``"false"`` is ``False``,
``"5"`` is ``5``), never the raw string."""

from __future__ import annotations

from noeta.core.agent import Agent
from noeta.core.events import ToolStarted
from noeta.plugins.fs import FileTools
from noeta.plugins.shell import Shell
from noeta.testing import reply
from tests.plugins.context.conftest import Harness


def agent() -> Agent:
    return Agent(name="a", model="m", extensions=[FileTools(), Shell()])


async def test_replace_all_false_as_a_string_replaces_nothing_ambiguous(h: Harness) -> None:
    (h.work / "a.txt").write_text("x\nx\n")
    h.provider.add(reply("", ("Read", {"file_path": "a.txt"})),
                   reply("", ("Edit", {"file_path": "a.txt", "old_string": "x",
                                       "new_string": "y", "replace_all": "false"})),
                   reply("done"))
    tid = await h.start(agent())
    # Two matches and replace_all false: refused, nothing changed.
    assert (h.work / "a.txt").read_text() == "x\nx\n"
    edit = (await h.results(tid))[-1].result
    assert edit.is_error and "occurs 2 times" in edit.text
    started = [r.event.call for r in await h.events(tid) if isinstance(r.event, ToolStarted)]
    assert started[-1].input["replace_all"] is False  # the logged call as it ran


async def test_run_in_background_false_as_a_string_runs_in_the_foreground(h: Harness) -> None:
    h.provider.add(reply("", ("Bash", {"command": "echo here",
                                       "run_in_background": "false"})), reply("done"))
    tid = await h.start(agent())
    [result] = await h.results(tid)
    assert result.result.text.strip() == "here" and not result.result.is_error


async def test_an_offset_as_a_string_is_honoured(h: Harness) -> None:
    (h.work / "b.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    h.provider.add(reply("", ("Read", {"file_path": "b.txt", "offset": "5", "limit": "2"})),
                   reply("done"))
    tid = await h.start(agent())
    [result] = await h.results(tid)
    assert "line5" in result.result.text and "line6" in result.result.text
    assert "line4" not in result.result.text and "line7" not in result.result.text
