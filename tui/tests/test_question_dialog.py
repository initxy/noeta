"""The agent's questions in the terminal, as Claude Code's dialog: picked
with the keys (↑/↓ and Enter, an option's number), answered in your own
words in its "Type something." box, several questions on tabs with a review
before they are sent, multi-select boxes, options beside their preview,
"Chat about this" to talk them over instead (the turn goes on) and Esc to
decline (the turn stops); the call's line then says how they were answered."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.events import Answer, ToolFinished
from noeta.core.types import ToolResult
from noeta.host.local import LocalApp
from noeta.plugins.ask_user import AskUser
from noeta.testing import ScriptedProvider, reply
from noeta_tui.app import NoetaApp
from noeta_tui.widgets import QuestionCard, ToolCall

from support import settle, shown

MODEL = "claude-sonnet-4-5"
DB = {"question": "Which database?", "header": "DB", "multiSelect": False,
      "options": [{"label": "SQLite", "description": "a file"},
                  {"label": "Postgres", "description": "a server"}]}
EXTRAS = {"question": "Which extras?", "header": "Extras", "multiSelect": True,
          "options": [{"label": "Docs", "description": "a guide"},
                      {"label": "Tests", "description": "a suite"}]}


async def _no_sleep(_: float) -> None:
    return None


def asking(tmp_path: Path, *questions: dict[str, Any], then: str = "ok"
           ) -> tuple[NoetaApp, ScriptedProvider]:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", {"questions": list(questions)})),
                                 reply(then)])
    agent = Agent(name="main", model=MODEL, extensions=(AskUser(),))
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None, sleep=_no_sleep)
    return NoetaApp(local, agent="main", model=MODEL, cwd=str(tmp_path)), provider


async def open_dialog(pilot: Any, app: NoetaApp) -> None:
    await pilot.press(*"go", "enter")
    await settle(pilot, lambda: "Enter to select" in shown(app))


async def answered(pilot: Any, app: NoetaApp, provider: ScriptedProvider) -> None:
    await settle(pilot, lambda: provider.steps == [] and not app.busy)


async def answers(app: NoetaApp) -> list[Answer]:
    return [i.body for i in await app.local.store.inbox(app.task_id)
            if isinstance(i.body, Answer)]


async def result(app: NoetaApp) -> ToolResult:
    [done] = [r.event for r in await app.local.client.events(app.task_id)
              if isinstance(r.event, ToolFinished)]
    return done.result


async def test_arrows_and_enter_pick_and_the_line_says_what_was_answered(
        tmp_path: Path) -> None:
    app, provider = asking(tmp_path, DB)
    async with app.run_test(size=(100, 32)) as pilot:
        await open_dialog(pilot, app)
        text = shown(app)
        for part in ("Which database?", "❯ 1. SQLite", "a file", "2. Postgres", "3. ",
                     "4. Chat about this", "↑/↓ to navigate"):
            assert part in text
        assert app.query_one(".question-other-line").placeholder == "Type something."
        assert "Submit" not in text  # one question, one answer: no tabs, no review
        [call] = [c for c in app.query(ToolCall) if c.call.name == "AskUserQuestion"]
        assert not call.head.display  # the dialog stands for the call's line
        await pilot.press("down", "enter")
        await answered(pilot, app, provider)
        [answer] = await answers(app)
        assert answer.data == {"answers": {"0": {"selected": ["Postgres"]}}}
        assert not app.query(QuestionCard)
        text = shown(app)
        assert "User answered noeta's questions:" in text
        assert "· Which database? → Postgres" in text


async def test_typing_answers_in_your_own_words(tmp_path: Path) -> None:
    app, provider = asking(tmp_path, DB)
    async with app.run_test(size=(100, 32)) as pilot:
        await open_dialog(pilot, app)
        await pilot.press(*"MySQL", "enter")  # typed on an option: into the box
        await answered(pilot, app, provider)
        [answer] = await answers(app)
        assert answer.data == {"answers": {"0": {"other": "MySQL"}}}
        assert (await result(app)).text.startswith('The user replied: "Which database?"="MySQL"')


async def test_several_questions_go_on_tabs_and_are_reviewed(tmp_path: Path) -> None:
    app, provider = asking(tmp_path, DB, EXTRAS)
    async with app.run_test(size=(100, 32)) as pilot:
        await open_dialog(pilot, app)
        text = shown(app)
        for part in ("☐ DB", "☐ Extras", "✔ Submit", "Tab/Arrow keys to navigate"):
            assert part in text
        await pilot.press("1")  # SQLite; the next tab opens
        await settle(pilot, lambda: "Which extras?" in shown(app))
        assert "☒ DB" in shown(app)
        await pilot.press("enter")  # checks Docs
        await settle(pilot, lambda: "[✔] Docs" in shown(app))
        await pilot.press("down", "down", *"lint", "enter")  # the box, then Submit
        await pilot.press("enter")
        await settle(pilot, lambda: "Review your answers" in shown(app))
        text = shown(app)
        assert "You have not answered all questions" not in text
        for part in ("· Which database?", "→ SQLite", "· Which extras?", "→ Docs, lint",
                     "Ready to submit your answers?", "1. Submit answers", "2. Cancel"):
            assert part in text
        await pilot.press("left")  # back to a question: its answer kept
        await settle(pilot, lambda: "[✔] Docs" in shown(app) and "lint" in str(
            app.query_one(".question-other-line").value))
        await pilot.press("tab", "enter")
        await answered(pilot, app, provider)
        [answer] = await answers(app)
        assert answer.data == {"answers": {"0": {"selected": ["SQLite"]},
                                           "1": {"selected": ["Docs"], "other": "lint"}}}
        assert '"Which extras?"="Docs, lint"' in (await result(app)).text
        assert "· Which extras? → Docs, lint" in shown(app)


async def test_the_review_warns_of_unanswered_questions(tmp_path: Path) -> None:
    app, provider = asking(tmp_path, DB, EXTRAS)
    async with app.run_test(size=(100, 32)) as pilot:
        await open_dialog(pilot, app)
        await pilot.press("tab", "tab")
        await settle(pilot, lambda: "You have not answered all questions" in shown(app))
        await pilot.press("enter")
        await answered(pilot, app, provider)
        [answer] = await answers(app)
        assert answer.data == {"answers": {}}
        assert (await result(app)).text == "The user answered none of the questions."
        assert "User answered none of noeta's questions" in shown(app)


async def test_chat_about_this_lets_the_model_ask_back(tmp_path: Path) -> None:
    app, provider = asking(tmp_path, DB, then="What would you like to know?")
    async with app.run_test(size=(100, 32)) as pilot:
        await open_dialog(pilot, app)
        await pilot.press("4")
        await answered(pilot, app, provider)
        [answer] = await answers(app)
        assert answer.decision == "deny"
        said = (await result(app)).text
        assert said.startswith("The user did not answer these questions. To tell you how to "
                               "go on, they said:\nThe user wants to talk these questions over")
        assert said.endswith('- "Which database?"\n  (No answer provided)')
        assert len(provider.requests) == 2  # the turn went on
        text = shown(app)
        assert "User declined to answer questions" in text
        assert "· Which database? (SQLite / Postgres)" in text
        assert "What would you like to know?" in text


async def test_esc_declines_and_stops_the_turn(tmp_path: Path) -> None:
    app, provider = asking(tmp_path, DB)
    async with app.run_test(size=(100, 32)) as pilot:
        await open_dialog(pilot, app)
        await pilot.press("escape")
        await settle(pilot, lambda: not app.busy and "User declined" in shown(app))
        assert await answers(app) == []
        assert (await result(app)).text == "Question withdrawn by user (no answer provided)."
        assert len(provider.requests) == 1  # stopped: the model was not asked again
        assert "Interrupted" in shown(app)
        assert app.focused is app.query_one("#prompt")


async def test_options_with_a_preview_sit_beside_it(tmp_path: Path) -> None:
    layout = {"question": "Which layout?", "header": "Layout", "multiSelect": False,
              "options": [{"label": "Sidebar", "description": "left", "preview": "| nav | page |"},
                          {"label": "Top bar", "description": "top", "preview": "| nav |\n| page |"}]}
    app, provider = asking(tmp_path, layout)
    async with app.run_test(size=(100, 32)) as pilot:
        await open_dialog(pilot, app)
        [card] = list(app.query(QuestionCard))
        assert card.has_class("-previews")
        preview = app.query_one(".question-preview")
        assert "| nav | page |" in str(preview.render())
        assert not app.query_one(".question-other").display  # Claude Code's: no box here
        await pilot.press("down")
        await settle(pilot, lambda: "| nav |\n| page |" in str(preview.render()))
        await pilot.press("enter")
        await answered(pilot, app, provider)
        [answer] = await answers(app)
        assert answer.data == {"answers": {"0": {"selected": ["Top bar"]}}}
        assert 'selected preview:\n| nav |\n| page |' in (await result(app)).text
