"""The pure shell-command classification: the metacharacter and expansion
checks, subcommand splitting, the long-leading-``sleep`` check and the
files a command shows."""

from __future__ import annotations

from typing import Any

import pytest

from noeta.toolkit.shell import (
    has_shell_meta,
    has_unquoted_expansion,
    long_leading_sleep,
    parse_argv,
    read_files,
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


@pytest.mark.parametrize(("command", "files"), [
    ("cat src/a.py", [("src/a.py", False)]),
    ("cat -n a.py", [("a.py", False)]),
    ("nl a.py", [("a.py", False)]),
    ("bat -p a.py", [("a.py", False)]),
    ("cat a.py && echo --- && cat 'b c.py'; cat d.py",
     [("a.py", False), ("b c.py", False), ("d.py", False)]),
    ("head -n 20 a.py", [("a.py", False)]),
    ("head -5 a.py", [("a.py", False)]),
    ("tail --lines=3 a.py", [("a.py", False)]),
    ("sed -n '3,9p' a.py", [("a.py", False)]),
    ("sed -n 4p a.py", [("a.py", False)]),
    ("grep -n TODO a.py", [("a.py", True)]),
    ("rg -i -C 2 todo a.py", [("a.py", True)]),
    # Not counted: pipes and redirections, two files to one program,
    # another program in the chain, a sed that is not a quiet range print,
    # grep beside anything else, a glob, stdin.
    ("cat a.py | head", []),
    ("cat a.py > b.py", []),
    ("cat a.py b.py", []),
    ("cat a.py && ls", []),
    ("ls; cat a.py", []),
    ("sed -i '1p' a.py", []),
    ("sed '1,2p' a.py", []),
    ("sed -n 's/a/b/p' a.py", []),
    ("head -n 0 a.py", []),
    ("cat -A a.py", []),
    ("cat -", []),
    ("grep x a.py && cat b.py", []),
    ("grep x *.py", []),
    ("grep -r x src", []),
    ("echo hi", []),
    ("cat 'unbalanced", []),
])
def test_files_a_command_shows(command: str, files: list[tuple[str, bool]]) -> None:
    """Claude Code's count of the files a ``Bash`` call read (its
    read-file state takes them in, so a compaction restores them)."""
    assert read_files(command) == files
