"""Model-visible texts are noeta's own words. Sentences found copied from
Claude Code outside the parity recordings (so the recorded-text check in
``scripts/parity/own_words.py`` cannot see them) stay gone."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.parity import own_words  # noqa: E402

#: Phrases of Claude Code's that were in noeta's texts, lower-cased.
COPIED = (
    "if the worktree has uncommitted files or commits not on the original branch",
    "will not remove a worktree entered this way",
    "send it again without notify_when_idle if it should still go",
    "ask the user to open this url in their browser to authorize the",
    "carries no authorization code. ask the user for the full address",
    "lists the direct children of a directory resource on an mcp server",
)


def _texts() -> str:
    parts = [p.read_text("utf-8") for p in sorted((ROOT / "noeta").rglob("*"))
             if p.suffix in (".py", ".md")]
    # Python's implicit concatenation splits sentences over lines: compare
    # the words only.
    joined = " ".join(parts).lower().replace('"\n', " ").replace("'\n", " ")
    return " ".join(w.strip("\"'") for w in joined.split())


@pytest.mark.parametrize("phrase", COPIED)
def test_a_copied_sentence_stays_gone(phrase: str) -> None:
    assert phrase not in _texts()


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


def test_noeta_repeats_no_recorded_text_of_claude_code() -> None:
    found = own_words.recordings()
    if not found:
        pytest.skip(f"no Claude Code recordings under {own_words.recordings_root()} "
                    f"(set {own_words.RECORDINGS_ENV})")
    assert own_words.check(own_words.load_index(found)) == []
