"""An agent file's ``initialPrompt``: the first message of a new top-level
conversation with that agent, ahead of what the person sent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.types import Image, Message, Text
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider


async def test_the_initial_prompt_opens_a_new_conversation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    async def create(self: Client, agent: str, input: Any = None, **kw: Any) -> str:
        sent.append(input)
        return str(kw.get("task_id"))

    monkeypatch.setattr(Client, "create", create)
    agents = [Agent(name="rev", model="m", extra={"initialPrompt": "Review the diff."}),
              Agent(name="plain", model="m")]
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), agents, ScriptedProvider([]))
    c = app.client
    await c.create("rev", "focus on tests")
    await c.create("rev")
    await c.create("plain", "hi")
    await c.create("rev", "again", history_from="other")
    image = Image(media_type="image/png", data=b"hi")
    await c.create("rev", Message(role="user", content=[image, Text(text="look")],
                                  origin="human"))
    assert sent[:4] == ["Review the diff.\n\nfocus on tests", "Review the diff.", "hi", "again"]
    last = sent[4]
    assert isinstance(last, Message)
    assert [type(b).__name__ for b in last.content] == ["Text", "Image", "Text"]
    assert last.text.startswith("Review the diff.")
    await app.store.close()
