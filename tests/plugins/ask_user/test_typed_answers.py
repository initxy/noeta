"""What a person types becomes a valid answer: ``typed_answer`` for front
ends, and a plain string accepted by the tool for a single question."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished, Waiting
from noeta.core.runner import Runtime
from noeta.host.questions import next_question, typed_answer
from noeta.plugins.ask_user import AskUser, normalize_answer, validate_questions
from noeta.testing import ScriptedProvider, reply


def question(text: str, *labels: str, multi: bool = False) -> dict[str, Any]:
    return {"question": text, "header": "H", "multiSelect": multi,
            "options": [{"label": lb, "description": "d"} for lb in labels]}


def detail(*qs: dict[str, Any]) -> dict[str, Any]:
    parsed = validate_questions({"questions": list(qs)})
    assert isinstance(parsed, list)
    return {"questions": parsed, "on": "question"}


def test_one_question_label_or_other_text() -> None:
    d = detail(question("Which db?", "SQLite", "Postgres"))
    assert typed_answer(d, "  sqlite ") == {"0": {"selected": ["SQLite"]}}
    assert typed_answer(d, "use mysql") == {"0": {"other": "use mysql"}}
    assert next_question(d, typed_answer(d, "x")) is None
    normalize_answer(typed_answer(d, "postgres"), d["questions"])


def test_multi_select_takes_comma_separated_labels() -> None:
    d = detail(question("Which?", "a", "b", "c", multi=True))
    assert typed_answer(d, "a, C") == {"0": {"selected": ["a", "c"]}}
    assert typed_answer(d, "a, z") == {"0": {"other": "a, z"}}
    single = detail(question("Which?", "a", "b"))
    assert typed_answer(single, "a, b") == {"0": {"other": "a, b"}}


def test_several_questions_one_line_each_or_json() -> None:
    d = detail(question("First?", "x", "y"), question("Second?", "p", "q"))
    first = typed_answer(d, "x")
    assert first == {"0": {"selected": ["x"]}}
    nxt = next_question(d, first)
    assert nxt is not None and nxt["question"] == "Second?"
    both = typed_answer(d, "whatever", first)
    assert both == {"0": {"selected": ["x"]}, "1": {"other": "whatever"}}
    assert next_question(d, both) is None
    typed = typed_answer(d, '{"answers": {"0": {"selected": ["y"]}, "1": {"other": "z"}}}')
    assert normalize_answer(typed, d["questions"])["0"]["selected"] == ["y"]


async def _no_sleep(_: float) -> None:
    return None


async def test_the_tool_takes_a_plain_string_for_one_question(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    q = {"questions": [question("Which db?", "sqlite", "pg")]}
    provider = ScriptedProvider([reply("", ("AskUserQuestion", q)), reply("done")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=(AskUser(),))},
                 provider_for=lambda _m: provider, env_for=lambda _s: None, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [waiting] = [r.event for r in await client.events(tid) if isinstance(r.event, Waiting)]
    await client.answer(tid, waiting.ids[0], data="sqlite")  # what a host sends as typed
    await rt.run_until_idle()
    [done] = [r.event for r in await client.events(tid) if isinstance(r.event, ToolFinished)]
    assert not done.result.is_error
    assert done.result.text.startswith('The user answered your questions: "Which db?"="sqlite"')
    await store.close()


def test_a_json_answer_may_leave_questions_unanswered() -> None:
    d = {**detail(question("First?", "x", "y"), question("Second?", "p", "q")),
         "partial": True}
    typed = typed_answer(d, '{"1": {"selected": ["p"]}}')
    assert next_question(d, typed) is None
    assert normalize_answer(typed, d["questions"], partial=True) == {
        "1": {"selected": ["p"], "other": None}}
    # Without "partial" the other questions are still asked one by one.
    strict = typed_answer({**d, "partial": False}, '{"1": {"selected": ["p"]}}')
    assert next_question(d, strict) is not None
