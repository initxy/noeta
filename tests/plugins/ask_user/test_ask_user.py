"""AskUserQuestion: 0.6's schema and limits, must-be-alone, park and answer,
withdrawal on stop, and how stale or invalid answers are handled."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.pending import pending
from noeta.core.store.sqlite import SqliteStore
from noeta.plugins.ask_user import (
    ALONE_ERROR,
    AnswerValidationError,
    AskUser,
    answer_question,
    validate_questions,
)
from noeta.plugins.permissions import Permissions
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


def q(**kw: Any) -> dict[str, Any]:
    base = {"question": "Which auth?", "header": "Auth method", "multiSelect": False,
            "options": [{"label": "OAuth (Recommended)", "description": "standard"},
                        {"label": "API key", "description": "simple"}]}
    return {**base, **kw}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def start(store: SqliteStore, provider: ScriptedProvider,
                tools: list[Any] = ()) -> tuple[Client, str, Any]:  # type: ignore[assignment]
    rt = runtime(store, provider, agent([Permissions(), AskUser()], tools))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    return client, tid, rt


def finished(evs: list[Any]) -> list[Any]:
    return [e.result for e in evs if isinstance(e, ToolFinished)]


def test_limits() -> None:
    assert isinstance(validate_questions({"questions": [q()]}), list)
    assert validate_questions({"questions": []}) == "questions must contain 1-4 items"
    assert validate_questions({"questions": [q()] * 5}) == "questions must contain 1-4 items"
    assert validate_questions({"questions": [q(header="x" * 13)]}) == \
        "header too long (max 12 chars)"
    assert validate_questions({"questions": [q(question="x" * 501)]}) == \
        "question too long (max 500)"
    assert validate_questions({"questions": [q(multiSelect="yes")]}) == \
        "each question needs a boolean multiSelect"
    one = [{"label": "a", "description": "d"}]
    assert validate_questions({"questions": [q(options=one)]}) == \
        "options must contain 2-4 items"
    dup = [{"label": "a", "description": "d"}] * 2
    assert validate_questions({"questions": [q(options=dup)]}) == "duplicate option label: 'a'"
    long = [{"label": "x" * 81, "description": "d"}, {"label": "b", "description": "d"}]
    assert validate_questions({"questions": [q(options=long)]}) == \
        "option label too long (max 80)"
    desc = [{"label": "a", "description": "x" * 301}, {"label": "b", "description": "d"}]
    assert validate_questions({"questions": [q(options=desc)]}) == \
        "option description too long (max 300)"


async def test_a_question_parks_and_the_answer_becomes_the_result(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", {"questions": [q()]})),
                                 reply("thanks")])
    client, tid, rt = await start(store, provider)
    assert (await client.get(tid)).waiting_on == "question"
    [item] = pending(await client.events(tid), tid)
    assert item.kind == "question" and item.detail["questions"][0]["id"] == "0"
    with pytest.raises(AnswerValidationError, match="not an option"):
        await answer_question(client, tid, item.request_id, {"0": {"selected": ["Nope"]}})
    with pytest.raises(AnswerValidationError, match="no pending"):
        await answer_question(client, tid, "stale", {"0": {"selected": ["API key"]}})
    await answer_question(client, tid, item.request_id,
                          {"answers": {"0": {"selected": ["API key"], "other": "  "}}})
    await rt.run_until_idle()
    [result] = finished(await events(client, tid))
    assert not result.is_error
    assert json.loads(result.text) == {"question_id": item.request_id, "answers": {
        "0": {"selected": ["API key"], "other": None}}}
    # 0.6's key order: sorted, nested maps too.
    assert result.text == ('{"answers": {"0": {"other": null, "selected": ["API key"]}}, '
                           f'"question_id": "{item.request_id}"}}')
    assert (await client.get(tid)).waiting_on == "input"


async def test_must_be_called_alone(store: SqliteStore) -> None:
    read = Fake("Read")
    provider = ScriptedProvider([reply("", ("Read", {}),
                                       ("AskUserQuestion", {"questions": [q()]})),
                                 reply("ok")])
    client, tid, _ = await start(store, provider, [read])
    results = finished(await events(client, tid))
    assert [(r.text, r.is_error) for r in results] == [(ALONE_ERROR, True)] * 2
    assert not read.runs


async def test_invalid_arguments_are_an_error_result(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", {"questions": "x"})),
                                 reply("ok")])
    client, tid, _ = await start(store, provider)
    [result] = finished(await events(client, tid))
    assert (result.text, result.is_error) == ("questions must be a list", True)


async def test_an_invalid_posted_answer_ends_the_question_with_an_error(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", {"questions": [q()]})),
                                 reply("ok")])
    client, tid, rt = await start(store, provider)
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, data={"0": {"other": "x" * 4001}})
    await rt.run_until_idle()
    [result] = finished(await events(client, tid))
    assert result.is_error
    assert result.text == ("The user's answer could not be used: "
                           "answer '0' other text too long (max 4000)")


async def test_stopping_withdraws_the_question(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", {"questions": [q()]}))])
    client, tid, rt = await start(store, provider)
    [item] = pending(await client.events(tid), tid)
    await client.cancel(tid, turn_only=True)
    await rt.run_until_idle()
    [result] = finished(await events(client, tid))
    assert result.is_error
    assert result.text == ("Question withdrawn by user (no answer provided).\n"
                           f'{{"question_id": "{item.request_id}", "withdrawn": true}}')
    assert (await client.get(tid)).waiting_on == "input"
    assert len(provider.requests) == 1  # no model turn after the withdrawal
