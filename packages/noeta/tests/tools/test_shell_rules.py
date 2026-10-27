"""The pure shell-command classification: allowlist matching, the curated
rules and their hardening, and the long-leading-``sleep`` check."""

from __future__ import annotations

from typing import Any

import pytest
from noeta.tools.shell_rules import (
    DEFAULT_SHELL_RULES,
    AllowRule,
    build_allowlist,
    command_in_allowlist,
    has_shell_meta,
    has_unquoted_expansion,
    long_leading_sleep,
    matching_rule,
    parse_argv,
    rule_from_spec,
    rule_spec_from_command,
    subcommands,
)

_RULES = build_allowlist((), base_rules=DEFAULT_SHELL_RULES)


# -- unquoted expansion is never allowlisted --------------------------------


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
        "git log -p packages/noeta-sdk",
        "git log --name-only --no-merges",
        "git diff",
        "git diff --stat",
        "git diff -- packages/noeta-sdk",
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
    rule = AllowRule("npm", "start", lambda tail: True, "npm_start")
    assert rule.matches(["npm", "start", "--x"])
    assert not rule.matches(["npm"])
    assert not rule.matches(["npm", "test"])
    assert not rule.matches(["npx", "start"])
    assert not rule.matches([])


def test_rule_from_spec_accepts_any_tail_and_labels() -> None:
    rule = rule_from_spec({"program": "npm", "subcommand": "start"})
    assert rule.label == "npm_start" and rule.matches(["npm", "start", "anything"])
    bare = rule_from_spec({"program": "make", "label": "mk"})
    assert bare.label == "mk" and bare.subcommand is None


@pytest.mark.parametrize(
    "spec", [{}, {"program": ""}, {"program": 3}, {"program": "x", "subcommand": 1}]
)
def test_rule_from_spec_rejects_malformed(spec: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        rule_from_spec(spec)


def test_build_allowlist_keeps_base_rules_first() -> None:
    rules = build_allowlist([{"program": "make"}], base_rules=DEFAULT_SHELL_RULES)
    assert rules[: len(DEFAULT_SHELL_RULES)] == DEFAULT_SHELL_RULES
    assert rules[-1].program == "make"
    assert build_allowlist() == ()
    assert command_in_allowlist("make all", rules)
    assert not command_in_allowlist("make all", DEFAULT_SHELL_RULES)


def test_rule_spec_from_command() -> None:
    assert rule_spec_from_command("npm start --port 3") == {
        "program": "npm",
        "subcommand": "start",
    }
    assert rule_spec_from_command("ls") == {"program": "ls"}
    assert rule_spec_from_command("ls | cat") is None
    assert rule_spec_from_command("ls 'x") is None
    assert rule_spec_from_command("") is None


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
