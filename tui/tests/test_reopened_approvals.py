"""An approval card takes answers only while its request is open: a reopened
task shows the approvals it was asked as history (the typed line goes to the
prompt, not to an old card), one still open is answered as ever, and one
answered elsewhere leaves its choices."""

from __future__ import annotations

from pathlib import Path

from noeta.testing import ScriptedProvider, reply
from noeta_tui.widgets import ApprovalCard, AssistantMessage, Choices, PromptInput

from support import AskBash, Lines, make_app, settle, shown, type_line


async def _asked(tmp_path: Path, *, answer: bool) -> str:
    """A task whose Bash call was asked about: answered yes (``answer``), or
    left open with the app closed."""
    provider = ScriptedProvider([reply("", ("Bash", {"n": 2})), reply("ok")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()])
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "run it")
        await settle(pilot, lambda: len(app.query(Choices)) == 1)
        if answer:
            await pilot.press("y")
            await settle(pilot, lambda: not app.busy and len(app.query(AssistantMessage)) == 1)
        return app.task_id


async def test_a_reopened_tasks_answered_approval_is_history(tmp_path: Path) -> None:
    tid = await _asked(tmp_path, answer=True)
    provider = ScriptedProvider([reply("on it")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()], resume=tid)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: len(app.query(AssistantMessage)) == 1
                     and app.focused is app.query_one(PromptInput))
        [card] = app.query(ApprovalCard)
        assert card.done and not app.query(Choices) and "⎿  Yes" in shown(app)
        assert not app.waiting_for
        await pilot.press(*"no", "enter")  # what the person types is a prompt
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        assert provider.requests[0].messages[-1].text == "no"


async def test_a_reopened_tasks_open_approval_is_answered(tmp_path: Path) -> None:
    tid = await _asked(tmp_path, answer=False)
    provider = ScriptedProvider([reply("done")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()], resume=tid)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: isinstance(app.focused, Choices))
        await pilot.press("y")
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        assert "line 2" in repr(provider.requests[0].messages[-1].content)


async def test_an_approval_answered_elsewhere_leaves_its_choices(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Bash", {"n": 1})), reply("ok")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()])
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "run it")
        await settle(pilot, lambda: len(app.query(Choices)) == 1)
        [card] = app.query(ApprovalCard)
        await app.local.client.answer(app.task_id, card.call_id, "deny")  # another window
        await settle(pilot, lambda: "⎿  No" in shown(app)
                     and app.focused is app.query_one(PromptInput))
        assert card.done and not app.query(Choices)
