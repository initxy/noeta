"""What an editor sees of a file change: Edit, Write and NotebookEdit go out
as ACP diffs (old and new text), Edit's already in the call it approves."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")

from acp import schema

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.events import ToolFinished
from noeta.core.extension import Ask, Extension, TaskView
from noeta.core.types import ToolResult, ToolUse
from noeta.host._acp_updates import finish_update
from noeta.host.acp import NoetaAcpAgent
from noeta.host.local import LocalApp
from noeta.plugins.fs import FileTools
from noeta.testing import ScriptedProvider, reply


class AskChanges(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt=f"run {call.name}?") if call.name != "Read" else None


class Editor:
    def __init__(self) -> None:
        self.updates: list[Any] = []
        self.cards: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, session_id: str, tool_call: Any, options: Any,
                                 **kw: Any) -> Any:
        self.cards.append(tool_call)
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id="allow"))


def diffs(content: Any) -> list[tuple[str, str | None, str]]:
    return [(c.path, c.old_text, c.new_text) for c in content or () if c.type == "diff"]


async def run(tmp_path: Path, *replies: Any) -> Editor:
    provider = ScriptedProvider([*replies, reply("done")])
    agent = Agent(name="main", model="m", extensions=[FileTools(), AskChanges()])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: LocalExecEnv(str(tmp_path)))
    acp_agent = NoetaAcpAgent(app)
    editor = Editor()
    acp_agent.on_connect(editor)
    await acp_agent.initialize(protocol_version=1)
    session = await acp_agent.new_session(cwd=str(tmp_path))
    await acp_agent.prompt(session.session_id, [schema.TextContentBlock(type="text", text="go")])
    await app.stop()
    return editor


def finished(editor: Editor) -> list[Any]:
    return [u for u in editor.updates
            if u.session_update == "tool_call_update" and u.status == "completed"]


async def test_an_edit_is_a_diff_in_its_call_its_card_and_its_result(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_text("one\ntwo\nthree\n")
    edit = {"file_path": str(a), "old_string": "two", "new_string": "TWO"}
    editor = await run(tmp_path, reply("", ("Read", {"file_path": str(a)})),
                       reply("", ("Edit", edit)))
    [call] = [u for u in editor.updates
              if u.session_update == "tool_call" and u.title.startswith("Edit")]
    assert call.kind == "edit"
    assert diffs(call.content) == [(str(a), "two", "TWO")]
    assert [loc.path for loc in call.locations] == [str(a)]
    [card] = editor.cards
    assert diffs(card.content) == [(str(a), "two", "TWO")]
    assert diffs(finished(editor)[-1].content) == [(str(a), "two", "TWO")]


async def test_a_write_shows_the_whole_file_before_and_after(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    old = "".join(f"line {i}\n" for i in range(20))
    a.write_text(old)
    new = old.replace("line 3\n", "line three\n").replace("line 17\n", "")
    b = tmp_path / "b.txt"
    editor = await run(tmp_path, reply("", ("Read", {"file_path": str(a)})),
                       reply("", ("Write", {"file_path": str(a), "content": new})),
                       reply("", ("Write", {"file_path": str(b), "content": "fresh\n"})))
    [over, created] = [diffs(u.content) for u in finished(editor)[-2:]]
    assert over == [(str(a), old, new)]
    assert created == [(str(b), None, "fresh\n")]


def _finished(**meta: Any) -> ToolFinished:
    return ToolFinished(result=ToolResult(call_id="c1", content=[]), meta=meta)


def test_a_write_whose_diff_does_not_rebuild_the_old_text_falls_back_to_text() -> None:
    call = ToolUse(id="c1", name="Write", input={"file_path": "/w/a.txt", "content": "x\n"})
    ev = _finished(diff="--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-q\n+y\n",
                   created=False, path="a.txt")
    update = finish_update(ev, call)
    assert diffs(update.content) == []
    assert update.content[0].content.text.startswith("--- a/a.txt")


@pytest.mark.parametrize(("mode", "old", "new"), [
    ("replace", "x = 1\n", "y"), ("insert", None, "y"), ("delete", "x = 1\n", "")])
def test_a_notebook_edit_is_a_diff_of_the_cell(mode: str, old: str | None, new: str) -> None:
    call = ToolUse(id="c1", name="NotebookEdit", input={
        "notebook_path": "/w/n.ipynb", "cell_id": "c1", "new_source": "y", "edit_mode": mode})
    update = finish_update(_finished(path="n.ipynb", mode=mode, old_source=old), call)
    assert diffs(update.content) == [("/w/n.ipynb", old, new)]
