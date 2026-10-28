"""Plan mode's plan file as the editor's ACP plan: each Write or Edit of
the file sends its list as plan entries, and a loaded session gets the plan
back."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")

from acp import schema

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.host._acp_updates import plan_entries
from noeta.host.acp import NoetaAcpAgent
from noeta.host.local import LocalApp
from noeta.plugins.fs import FileTools
from noeta.plugins.permissions import Permissions
from noeta.plugins.permissions import plan as pl
from noeta.testing import ScriptedProvider, reply


class Editor:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, **kw: Any) -> Any:
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id="allow"))

    def plans(self) -> list[list[tuple[str, str]]]:
        return [[(e.content, e.status) for e in u.entries]
                for u in self.updates if u.session_update == "plan"]


async def test_the_plan_file_is_the_editors_plan(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    path = str(tmp_path / "plans" / "the-plan.md")
    monkeypatch.setattr(pl, "new_plan_path", lambda _d, rng=None: path)
    other = str(tmp_path / "notes.md")
    provider = ScriptedProvider([
        reply("", ("Write", {"file_path": path, "content": "# Plan\n\n- [ ] one\n- [ ] two\n"})),
        reply("", ("Write", {"file_path": other, "content": "- not the plan\n"})),
        reply("", ("Edit", {"file_path": path, "old_string": "- [ ] one",
                            "new_string": "- [x] one"})),
        reply("planned")])
    agent = Agent(name="main", model="m", extensions=[
        FileTools(), Permissions(mode="bypassPermissions", plans_dir=str(tmp_path / "plans"))])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: LocalExecEnv(str(tmp_path)))
    acp_agent = NoetaAcpAgent(app)
    editor = Editor()
    acp_agent.on_connect(editor)
    await acp_agent.initialize(protocol_version=1)
    session = await acp_agent.new_session(cwd=str(tmp_path))
    await acp_agent.set_session_mode(session_id=session.session_id, mode_id="plan")
    await acp_agent.prompt(session.session_id, [schema.TextContentBlock(type="text",
                                                                        text="plan it")])
    assert editor.plans() == [[("one", "pending"), ("two", "pending")],
                              [("one", "completed"), ("two", "pending")]]

    again = NoetaAcpAgent(app)
    loaded = Editor()
    again.on_connect(loaded)
    await again.load_session(cwd=str(tmp_path), session_id=session.session_id)
    assert loaded.plans() == [[("one", "completed"), ("two", "pending")]]
    await app.stop()


@pytest.mark.parametrize(("text", "entries"), [
    ("# P\n\n1. first\n2) second\n   * [X] nested done\n```\n- in code\n```\n",
     [("first", "pending"), ("second", "pending"), ("nested done", "completed")]),
    ("# Plan\n\nIntro.\n\n## Context\n\nwords\n\n## Steps ##\n", [
        ("Plan", "pending"), ("Context", "pending"), ("Steps", "pending")]),
    ("\n  Just do it.\nThen more.\n", [("Just do it.", "pending")]),
    ("", []),
])
def test_plan_entries(text: str, entries: list[tuple[str, str]]) -> None:
    got = plan_entries(text)
    assert [(e.content, e.status) for e in got] == entries
    assert all(e.priority == "medium" for e in got)
