"""``scripts/parity/own_words.py`` reports where noeta's model-visible texts
repeat Claude Code's recorded text; it is advisory (ADR-020) and never
fails."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.parity import own_words  # noqa: E402


def test_the_recorded_text_check_finds_a_shared_run(tmp_path: Path) -> None:
    said = ("Before any edit, look at the whole file and keep its style; never invent an API "
            "that the codebase does not already have.")
    session = tmp_path / "rec" / "m1"
    session.mkdir(parents=True)
    (session / "0001.json").write_text(json.dumps({"body": {
        "system": [{"type": "text", "text": said}],
        "messages": [{"role": "assistant", "content": [{"type": "text", "text": (
            "model text is never indexed, however long it is")}]}]}}))
    raw = own_words.index([tmp_path / "rec"])
    indexed = {raw[i:i + own_words.DIGEST] for i in range(0, len(raw), own_words.DIGEST)}
    # A shared run of SHARED characters anywhere is caught, whitespace aside.
    for start in range(0, 40):
        copied = "Note: " + said[start:start + own_words.SHARED].replace(" ", "\n  ") + " etc"
        assert own_words.matches(copied, indexed, []), start
    assert not own_words.matches("model text is never indexed, however long it is", indexed, [])
    # A text copied from its first character on is one run too.
    assert own_words.matches(said, indexed, []) == [(0, said[:own_words.SHARED + 20])]
    # An allowed literal is cut out first.
    head = said[:own_words.SHARED]
    assert not own_words.matches(head + " but then ours", indexed, [head])


def test_no_index_of_claude_code_text_is_committed() -> None:
    assert not list((ROOT / "parity").rglob("cc-windows.bin"))


def test_recordings_are_read_from_outside_the_repository(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("2.1.312", "2.1.312-r2", "2.1.312-r2-attempts", "0.0.1"):
        (tmp_path / name / "m1").mkdir(parents=True)
        (tmp_path / name / "m1" / "0001.json").write_text(json.dumps({"body": {}}))
    monkeypatch.setenv(own_words.RECORDINGS_ENV, str(tmp_path))
    assert [p.name for p in own_words.recordings()] == ["2.1.312", "2.1.312-r2"]

    monkeypatch.setenv(own_words.RECORDINGS_ENV, str(tmp_path / "absent"))
    assert own_words.recordings() == []
    assert own_words.main([]) == 0
    assert capsys.readouterr().out.startswith("own-words: skipped, no Claude Code recordings")


def test_a_deferred_tools_description_is_indexed() -> None:
    """Claude Code sends a loaded deferred tool's full description in
    ``tools`` (``defer_loading: true``), so the check covers it; a deferred
    tool no recording loads has no Claude Code text to compare."""
    said = "Fetches a page and answers a question about it with a small, fast model."
    body = {"tools": [{"name": "WebFetch", "defer_loading": True, "description": said}]}
    assert said in list(own_words.claude_code_texts(body))


def test_a_shared_run_is_reported_and_never_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    said = (ROOT / "noeta" / "plugins" / "subtasks" / "descriptions" / "agent.md").read_text(
        "utf-8")
    (tmp_path / "2.1.312" / "m1").mkdir(parents=True)
    (tmp_path / "2.1.312" / "m1" / "0001.json").write_text(json.dumps({"body": {
        "tools": [{"name": "Agent", "description": said}]}}))
    monkeypatch.setenv(own_words.RECORDINGS_ENV, str(tmp_path))
    assert own_words.main([]) == 0
    out = capsys.readouterr().out
    assert "agent.md" in out and "own-words (advisory):" in out
