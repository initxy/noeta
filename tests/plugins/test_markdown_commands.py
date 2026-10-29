"""Markdown commands fill Claude Code's argument placeholders, as skills do."""

from __future__ import annotations

import pytest

from noeta.core.commands import expand_with
from noeta.core.extension import CommandContext
from noeta.plugins.markdown_commands import MarkdownCommands


@pytest.mark.parametrize(("body", "typed", "out"), [
    ("Review $ARGUMENTS now.", "/c a.py b.py", "Review a.py b.py now."),
    ("Compare $0 with $1.", "/c a.py 'b c.py'", "Compare a.py with b c.py."),
    ("First: $ARGUMENTS[0].", "/c a.py b.py", "First: a.py."),
    ("Explain this plugin.", "/c more please", "Explain this plugin.\nARGUMENTS: more please"),
    ("Explain this plugin.", "/c", "Explain this plugin."),
    ("Costs \\$1.", "/c x", "Costs $1.\nARGUMENTS: x"),
])
async def test_a_markdown_command_fills_its_arguments(body: str, typed: str, out: str) -> None:
    ext = MarkdownCommands([{"name": "c", "body": body}])
    assert await expand_with([ext], CommandContext(cwd="/"), typed) == out
