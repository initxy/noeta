"""The pure shell-command classification: the metacharacter and expansion
checks, subcommand splitting and the long-leading-``sleep`` check."""

from __future__ import annotations

from typing import Any

import pytest

from noeta.toolkit.shell import (
    has_shell_meta,
    has_unquoted_expansion,
    long_leading_sleep,
    parse_argv,
    subcommands,
)


def test_meta_and_expansion_predicates() -> None:
    assert has_shell_meta("a; b") and has_shell_meta("a\nb") and not has_shell_meta("a b")
    assert has_unquoted_expansion("a*") and not has_unquoted_expansion("'a*'")
    assert has_unquoted_expansion("x=~/y") and not has_unquoted_expansion("HEAD~1")
    assert parse_argv("a 'b c'") == ["a", "b c"]
    assert parse_argv("a 'b") is None


# -- long leading sleep ------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "described"),
    [
        ("sleep 110", "standalone sleep 110"),
        ("sleep 25", "standalone sleep 25"),
        ("sleep 30.5", "standalone sleep 30.5"),
        ("sleep 600 && tail -5 out.log", "sleep 600 followed by: tail -5 out.log"),
        ("  sleep 120; cat a | grep b", "sleep 120 followed by: cat a grep b"),
        ("sleep 24", None),
        ("sleep 5 && make", None),
        ("echo hi && sleep 300", None),
        ("sleep 1m", None),
        ("until test -f done; do sleep 2; done", None),
        ("echo 'sleep 300; x'", None),
    ],
)
def test_long_leading_sleep_matches_claude_code(command: str, described: Any) -> None:
    assert long_leading_sleep(command) == described


def test_subcommands_split_outside_quotes_only() -> None:
    assert subcommands("a && b || c; d | e & f") == ["a", "b", "c", "d", "e", "f"]
    assert subcommands("echo 'a; b' && echo \"c \\\" ; d\"") == [
        "echo 'a; b'",
        'echo "c \\" ; d"',
    ]
    assert subcommands("  ") == []
