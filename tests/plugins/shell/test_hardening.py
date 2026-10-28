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
        "find . -maxdepth 0 {-exec,touch} /tmp/x {} +",
        "rg --files {--pre,touch} x",
        "git diff {--output,/dev/null}",
        "grep -rn foo *.py",
        "ls a?",
        "ls a[bc]",
        "ls ~",
        "ls ~/other",
        "ls --dir=~/x",
    ],
)
def test_unquoted_expansion_is_never_allowlisted(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is False


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.py'",
        'find . -name "*.py"',
        "find . -name \\*.py",
        "rg 'a{1,2}' src",
        'grep -rn "x[0-9]" .',
        "git log HEAD~1",
        "git log -n 5 --oneline",
    ],
)
def test_quoted_or_escaped_expansion_stays_allowlisted(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is True


# -- what the scout prompts recommend runs, the rest needs sign-off ----------


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "ls -la packages",
        "find . -name shell_rules.py",
        "find packages -type f -name '*.md'",
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
        "pytest -q",
        "uv run pytest -x",
        "npm test",
        "pnpm test",
        "rg foo src",
        "grep -rn foo .",
    ],
)
def test_representative_invocations_match(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is True


@pytest.mark.parametrize(
    "command",
    [
        "git log --ext-diff",
        "find packages -type f -name *.md",
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
        "find . -delete",
        "find . -exec rm {} ;",
        "rg --pre=cat x",
        "rg --pre cat x",
        "rg --hostname-bin x",
        "pytest --pdb",
        "uv run python x.py",
        "git diff --output=x",
        "git status --ignored",
        "ls 'unbalanced",
        "",
    ],
)
def test_state_changing_and_unbounded_commands_do_not_match(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is False


def test_test_runner_rules_are_marked_as_running_workspace_code() -> None:
    marked = {r.label for r in DEFAULT_SHELL_RULES if r.runs_workspace_code}
    assert marked == {"pytest", "uv_run_pytest", "npm_test", "pnpm_test"}


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
    assert rule is not None and rule.harden is not None
    argv = parse_argv(command)
    assert argv is not None
    assert rule.harden(argv) == hardened


def test_non_git_rules_do_not_harden() -> None:
    rule = matching_rule("ls -la", _RULES)
    assert rule is not None and rule.harden is None


# -- rule plumbing -------------------------------------------------------------


def test_rule_matches_program_and_subcommand_exactly() -> None:
    rule = HardeningRule("npm", "start", lambda tail: True, "npm_start")
    assert rule.matches(["npm", "start", "--x"])
    assert not rule.matches(["npm"])
    assert not rule.matches(["npm", "test"])
    assert not rule.matches(["npx", "start"])
    assert not rule.matches([])
