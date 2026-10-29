"""Which commands ``Bash`` runs hardened: structural matching on the parsed
argv, the curated rules, and the ``git`` rewrites."""

from __future__ import annotations

import pytest

from noeta.plugins.shell.hardening import DEFAULT_SHELL_RULES, HardeningRule, matching_rule
from noeta.toolkit.shell import parse_argv

_RULES = DEFAULT_SHELL_RULES


def command_in_allowlist(command: str, rules: tuple[HardeningRule, ...]) -> bool:
    return matching_rule(command, rules) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git diff {--output,/dev/null}",
        "git diff *.py",
        "git log a?",
        "git log a[bc]",
        "git diff ~",
        "git log ~/other",
    ],
)
def test_unquoted_expansion_is_never_allowlisted(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is False


@pytest.mark.parametrize(
    "command",
    [
        "git diff '*.py'",
        'git diff "*.py"',
        "git diff \\*.py",
        "git log HEAD~1",
        "git log -n 5 --oneline",
    ],
)
def test_quoted_or_escaped_expansion_stays_allowlisted(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is True


# -- the git commands that only read run hardened; nothing else matches -----


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git status --short",
        "git log",
        "git log --oneline -n 20",
        "git log -5 --stat",
        "git log -p noeta/core",
        "git log --name-only --no-merges",
        "git diff",
        "git diff --stat",
        "git diff -- noeta/core",
    ],
)
def test_representative_invocations_match(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is True


@pytest.mark.parametrize(
    "command",
    [
        "git log --ext-diff",
        "ls -la",
        "pytest -q",
        "rg foo src",
        "git log --format=%H",
        "git commit -m wip",
        "git add .",
        "rm -rf /",
        "cat pyproject.toml",
        "head -n 5 CONTEXT.md",
        "tail -n 5 CONTEXT.md",
        "git status | cat",
        "ls; rm x",
        "echo $(id)",
        "git diff --output=x",
        "git status --ignored",
        "ls 'unbalanced",
        "",
    ],
)
def test_state_changing_and_unbounded_commands_do_not_match(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is False


# -- hardening ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "hardened"),
    [
        ("git status --short", ["git", "-c", "core.fsmonitor=", "status", "--short"]),
        (
            "git diff --stat -- a",
            [
                "git", "-c", "core.fsmonitor=", "diff", "--no-ext-diff",
                "--no-textconv", "--stat", "--", "a",
            ],
        ),
        ("git log -p", ["git", "log", "--no-ext-diff", "--no-textconv", "-p"]),
    ],
)
def test_git_rules_harden_their_argv(command: str, hardened: list[str]) -> None:
    rule = matching_rule(command, _RULES)
    assert rule is not None
    argv = parse_argv(command)
    assert argv is not None
    assert rule.harden(argv) == hardened


# -- rule plumbing -------------------------------------------------------------


def test_rule_matches_program_and_subcommand_exactly() -> None:
    rule = HardeningRule("npm", "start", lambda tail: True, lambda argv: argv)
    assert rule.matches(["npm", "start", "--x"])
    assert not rule.matches(["npm"])
    assert not rule.matches(["npm", "test"])
    assert not rule.matches(["npx", "start"])
    assert not rule.matches([])
