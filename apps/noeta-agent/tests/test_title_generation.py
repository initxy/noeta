"""Async LLM generation of session titles.

Under the mock provider generate_title itself would return None (no real LLM
request), so these tests monkeypatch noeta.agent.host.service.generate_title
to inject a fixed title, verifying:
  - after the first turn ends (mock first turn asks a question →
    TaskSuspended) the title updates to the generated value
  - SSE receives a session_meta synthetic frame and replay does not include
    it (synthetic frames are not replayed)
  - later turn ends do not regenerate (title_generated is set)
"""
from __future__ import annotations

import time

from tests.conftest import create_session, login, read_sse, wait_status

_TITLE = "Platform report"


def _title_of(client, sid: str) -> str:
    return client.get(f"/api/v1/sessions/{sid}").json()["session"]["title"]


def _wait_title(client, sid: str, want: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _title_of(client, sid) == want:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"title did not update to {want!r}, currently {_title_of(client, sid)!r}"
    )


def test_title_generated_on_first_turn(make_client, monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_generate_title(settings, first_message, assistant_reply, task_id):
        calls.append((first_message, task_id))
        return _TITLE

    monkeypatch.setattr(
        "noeta.agent.host.service.generate_title", fake_generate_title
    )
    client = make_client()
    login(client)
    sid = create_session(client)

    goal = "Write a report on the data platform"
    resp = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": goal})
    assert resp.status_code == 202

    # Mock first turn asks a question → session enters waiting (first-turn end
    # triggers title generation)
    wait_status(client, sid, {"waiting"})

    # The title lands asynchronously; poll until updated
    _wait_title(client, sid, _TITLE)
    assert calls, "generate_title should have been called"
    # It receives the raw user input, and task_id is non-empty
    assert calls[0][0] == goal
    assert calls[0][1], "task_id should be non-empty"

    # Replay semantics: session_meta is a synthetic frame; the replay segment
    # (replay from the beginning) does not include it
    replayed = read_sse(client, sid, stop_types=("question",), timeout=10)
    assert all(
        e["event"] != "session_meta" for e in replayed
    ), "session_meta must not be replayed"

    # Answer the question → this turn continues to the end (skill → sandbox
    # off → end_turn)
    q = next(e for e in replayed if e["event"] == "question")["data"]
    qid = q["questions"][0]["id"]
    resp = client.post(
        f"/api/v1/sessions/{sid}/answer",
        json={"question_id": q["question_id"], "answers": {qid: {"choice_id": "eng"}}},
    )
    assert resp.status_code == 202
    wait_status(client, sid, {"idle"}, timeout=20)

    # No regeneration after the second segment ends (title_generated is set)
    time.sleep(0.5)
    assert len(calls) == 1, f"title should be generated once, got {len(calls)}"
    assert _title_of(client, sid) == _TITLE


def test_clean_title_strips_quotes_and_truncates():
    """Result-cleanup pure function: strips quotes/periods/newlines, truncates
    over-long titles, returns "" for empty."""
    from noeta.agent.host.title import _clean_title

    assert _clean_title('"Platform report"') == "Platform report"
    assert _clean_title("《Tracking plan》。") == "Tracking plan"
    # Newlines fold into spaces (models occasionally emit multiple lines;
    # collapse into a single-line title)
    assert _clean_title("Analyze churn\n") == "Analyze churn"
    assert _clean_title("Row a\nRow b") == "Row a Row b"
    # Truncated past 16 characters
    long = "abcdefghijklmnopqrst"
    assert len(_clean_title(long)) == 16
    # Empty / punctuation-only → empty string
    assert _clean_title("   。、  ") == ""
    assert _clean_title("") == ""


def test_generate_title_skips_mock_provider():
    """mock provider (no gateway credentials) → generate_title returns None
    directly, no request sent."""
    from noeta.agent.config import Settings
    from noeta.agent.host.title import generate_title

    s = Settings(llm_provider="mock")
    assert generate_title(s, "Write a report", None, "task-x") is None


def test_title_not_generated_when_fn_returns_none(make_client, monkeypatch):
    """Generation failure (returns None) → the title keeps the truncation
    fallback, title_generated stays unset, and a retry remains possible."""
    calls: list[int] = []

    def fake_generate_title(settings, first_message, assistant_reply, task_id):
        calls.append(1)
        return None

    monkeypatch.setattr(
        "noeta.agent.host.service.generate_title", fake_generate_title
    )
    client = make_client()
    login(client)
    sid = create_session(client)

    goal = "Help me analyze checkout conversion"
    client.post(f"/api/v1/sessions/{sid}/messages", json={"content": goal})
    wait_status(client, sid, {"waiting"})
    # Generation was called but returned None → the title keeps the
    # first-line truncation fallback
    time.sleep(0.5)
    assert calls, "generate_title should have been called"
    assert _title_of(client, sid) == goal[:40]
