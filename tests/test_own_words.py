"""Model-visible texts are noeta's own words. Sentences found copied from
Claude Code outside the parity recordings (so the recorded-text check in
``scripts/parity/own_words.py`` cannot see them) stay gone."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

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
