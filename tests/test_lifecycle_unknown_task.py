"""Lifecycle verbs refuse an unknown ``task_id`` BEFORE writing anything.

``cancel`` / ``interrupt`` / ``close`` / ``reopen`` write their control-plane
marker through ``system_emit``, which appends to whatever ``task_id`` it is
handed and creates the stream as a side effect. A verb that wrote first and
folded afterwards therefore turned one typo into permanent corruption: the
stream's genesis became ``TaskCancelled`` / ``TurnInterrupted``, every later
``fold`` of it raised, and that took the whole-log read models down with it —
``list_task_summaries`` scans every stream, so ONE poisoned id broke the
sessions list for the entire store, recoverable only by deleting the task.

So the invariant these tests pin is ordering, not just the error type: after a
refused call the log must be byte-for-byte untouched. The "already poisoned"
case is covered too, because a store written by an older version still carries
such streams and the verbs must give a typed refusal there rather than the raw
``ValueError`` fold produces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from tests._sdk_session import official_registry as official_agent_registry

from noeta.client import SdkHost
from noeta.execution.driver import (
    InteractionDriver,
    TaskAlreadyTerminalError,
    UnknownTaskError,
    multi_turn_policy_wrapper,
)
from noeta.protocols.events import TaskCancelledPayload
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.read_models.tasks import list_task_summaries
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


UNKNOWN = "no-such-task"


def _driver(tmp_path: Path) -> tuple[InteractionDriver, SdkHost, InMemoryEventLog]:
    ws = tmp_path / "ws"
    ws.mkdir()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    host = SdkHost(
        event_log=event_log,
        content_store=InMemoryContentStore(),
        dispatcher=dispatcher,
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="done")],
                    usage=Usage(uncached=1, output=1),
                    raw={},
                )
            ]
        ),
        model="gpt-test",
        workspace_dir=ws,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )
    return InteractionDriver(host), host, event_log


def _verbs(driver: InteractionDriver) -> dict[str, Callable[[str], Any]]:
    return {
        "cancel": driver.cancel,
        "interrupt": driver.interrupt,
        "close": driver.close,
        "reopen": driver.reopen,
    }


@pytest.mark.parametrize("verb", ["cancel", "interrupt", "close", "reopen"])
def test_unknown_task_id_is_refused_without_writing(
    tmp_path: Path, verb: str
) -> None:
    driver, _host, event_log = _driver(tmp_path)

    with pytest.raises(UnknownTaskError) as excinfo:
        _verbs(driver)[verb](UNKNOWN)

    assert excinfo.value.code == "unknown_task"
    assert excinfo.value.verb == verb
    assert excinfo.value.reason == "no events"
    # The whole point: nothing durable happened, so no stream was minted.
    assert [s.task_id for s in event_log.list_task_streams()] == []
    assert event_log.read(UNKNOWN) == []


@pytest.mark.parametrize("verb", ["cancel", "interrupt", "close", "reopen"])
def test_orphan_genesis_stream_is_refused_not_re_read(
    tmp_path: Path, verb: str
) -> None:
    """A stream already poisoned by an older version refuses with a TYPED error.

    Such a store exists in the wild, and a verb that let fold's raw ``ValueError``
    escape would give a caller nothing structural to branch on.
    """
    driver, _host, event_log = _driver(tmp_path)
    event_log.system_emit(
        task_id=UNKNOWN,
        type="TaskCancelled",
        payload=TaskCancelledPayload(reason="from an older runtime", cascade=False),
        actor="test",
        origin="system",
    )
    before = list(event_log.read(UNKNOWN))

    with pytest.raises(UnknownTaskError) as excinfo:
        _verbs(driver)[verb](UNKNOWN)

    assert excinfo.value.reason == "first event is 'TaskCancelled', expected TaskCreated"
    assert event_log.read(UNKNOWN) == before  # refused, not appended to


def test_a_poisoned_stream_no_longer_grows_under_repeated_calls(
    tmp_path: Path,
) -> None:
    """The corruption was self-compounding only through the unguarded verbs;
    with the guard in place a caller retrying a bad id adds nothing."""
    driver, _host, event_log = _driver(tmp_path)
    for _ in range(3):
        with pytest.raises(UnknownTaskError):
            driver.cancel(UNKNOWN)
        with pytest.raises(UnknownTaskError):
            driver.interrupt(UNKNOWN)
    assert [s.task_id for s in event_log.list_task_streams()] == []


def test_whole_log_read_models_survive_a_refused_cancel(tmp_path: Path) -> None:
    """The failure mode that made this a data-corruption bug rather than a
    bad error message: ONE poisoned stream broke ``task_summaries`` for the
    entire store, because it folds every stream it finds."""
    driver, host, event_log = _driver(tmp_path)
    started = driver.start(goal="a real conversation", agent="main")

    with pytest.raises(UnknownTaskError):
        driver.cancel(UNKNOWN)

    rows = list_task_summaries(event_log, event_log, host.content_store)
    assert [r["task_id"] for r in rows] == [started.task_id]


def test_terminal_still_wins_its_own_error(tmp_path: Path) -> None:
    """The new guard runs alongside the terminal check, not instead of it —
    a cancelled conversation is known, so it keeps ``task_already_terminal``."""
    driver, _host, _event_log = _driver(tmp_path)
    started = driver.start(goal="a real conversation", agent="main")
    driver.cancel(started.task_id)

    with pytest.raises(TaskAlreadyTerminalError) as excinfo:
        driver.cancel(started.task_id)
    assert excinfo.value.code == "task_already_terminal"


def test_live_conversation_is_untouched_by_the_guard(tmp_path: Path) -> None:
    """The happy path keeps working for all four verbs — the guard adds a
    read, not a refusal."""
    driver, _host, event_log = _driver(tmp_path)
    started = driver.start(goal="a real conversation", agent="main")
    task_id = started.task_id

    driver.interrupt(task_id, reason="stop")
    driver.close(task_id)
    driver.reopen(task_id)
    driver.cancel(task_id)

    types = [e.type for e in event_log.read(task_id)]
    assert types[0] == "TaskCreated"
    for expected in (
        "TurnInterrupted",
        "ConversationClosed",
        "ConversationReopened",
        "TaskCancelled",
    ):
        assert expected in types
