"""AskUserQuestion: schema and limits, park and answer, the prose result,
withdrawal on stop, and how stale or invalid answers are handled."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.host.pending import pending
from noeta.host.questions import answer_question
from noeta.plugins.ask_user import (
    INPUT_SCHEMA,
    SINGLE_OPTION_ERROR,
    AnswerValidationError,
    AskUser,
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
    assert validate_questions({"questions": [q(multiSelect="yes")]}) == \
        "multiSelect must be a boolean"
    no_multi = {k: v for k, v in q().items() if k != "multiSelect"}
    [default] = validate_questions({"questions": [no_multi]})
    assert default["multiSelect"] is False
    one = [{"label": "a", "description": "d"}]
    assert validate_questions({"questions": [q(options=one)]}) == SINGLE_OPTION_ERROR
    assert validate_questions({"questions": [q(), q(question="B?", options=one)]}) == \
        SINGLE_OPTION_ERROR
    assert validate_questions({"questions": [q(options=one * 5)]}) == \
        "options must contain 2-4 items"
    dup = [{"label": "a", "description": "d"}] * 2
    assert validate_questions({"questions": [q(options=dup)]}) == "duplicate option label: 'a'"
    assert validate_questions({"questions": [q(options=[{"label": "a"}, *one])]}) == \
        "each option needs a description string"


def test_lengths_are_guidance_not_limits() -> None:
    # The 12-character header and the 1-5 word label are what the schema
    # asks for; a longer one is still shown, not refused.
    long = [{"label": "x" * 81, "description": ""}, {"label": "b", "description": "x" * 301}]
    [one] = validate_questions({"questions": [
        q(header="x" * 13, question="x" * 501, options=long)]})
    assert one["header"] == "x" * 13 and one["options"][0]["label"] == "x" * 81


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
    assert result.text == ('The user answered your questions: "Which auth?"="API key". '
                           "Go on with these answers in mind.")
    assert (await client.get(tid)).waiting_on == "input"


async def test_it_may_share_a_reply_with_other_calls(store: SqliteStore) -> None:
    read = Fake("Read")
    preview = [{"label": "A", "description": "d", "preview": "+--+\n|a |"},
               {"label": "B", "description": "d"}]
    provider = ScriptedProvider([reply("", ("Read", {}), ("AskUserQuestion", {"questions": [
                                     q(), q(question="Layout?", header="UI", options=preview)]})),
                                 reply("ok")])
    client, tid, rt = await start(store, provider, [read])
    assert read.runs and (await client.get(tid)).waiting_on == "question"
    [item] = pending(await client.events(tid), tid)
    await answer_question(client, tid, item.request_id, {
        "0": {"other": "Passkeys"}, "1": {"selected": ["A"]}})
    await rt.run_until_idle()
    result = finished(await events(client, tid))[-1]
    # "Passkeys" is typed text, not an option: the model is told to read it as written.
    assert result.text == ('The user replied: "Which auth?"="Passkeys", '
                           '"Layout?"="A" selected preview:\n+--+\n|a |. '
                           "What they wrote may ask for clarification, for changes, or for "
                           "you to stop: read it closely and do what it actually says.")


def test_schema_shape() -> None:
    assert INPUT_SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert INPUT_SCHEMA["additionalProperties"] is False
    item = INPUT_SCHEMA["properties"]["questions"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["multiSelect"]["default"] is False
    option = item["properties"]["options"]["items"]
    assert option["additionalProperties"] is False and "preview" in option["properties"]
    assert {"answers", "annotations", "metadata"} <= set(INPUT_SCHEMA["properties"])


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
    [_item] = pending(await client.events(tid), tid)
    await client.cancel(tid, turn_only=True)
    await rt.run_until_idle()
    [result] = finished(await events(client, tid))
    assert result.is_error
    assert result.text == "Question withdrawn by user (no answer provided)."
    assert (await client.get(tid)).waiting_on == "input"
    assert len(provider.requests) == 1  # no model turn after the withdrawal


def test_question_texts_must_differ() -> None:
    error = validate_questions({"questions": [q(), q(header="Again")]})
    assert isinstance(error, str) and "Which auth?" in error
