"""Claude Code's ``Tool(spec)`` rule syntax: parsing and matching."""

from __future__ import annotations

import pytest

from noeta.plugins.permissions import Permissions
from noeta.toolkit.permission_rules import (
    RuleContext,
    format_rule,
    match,
    parse_rule,
    path_pattern_matches,
)

CTX = RuleContext(root="/proj", home="/home/u", cwd="/proj")


def m(rule: str, name: str, *, allow: bool = False, **input: object) -> bool:
    return match(parse_rule(rule), name, input, CTX, for_allow=allow)


def test_parse_and_format_round_trip() -> None:
    rule = parse_rule("Bash(echo \\(hi\\))")
    assert (rule.tool, rule.content) == ("Bash", "echo (hi)")
    assert format_rule(rule.tool, rule.content) == "Bash(echo \\(hi\\))"
    assert parse_rule("Write").content is None
    assert parse_rule("Bash(*)").content is None
    assert parse_rule("Task(Explore)").tool == "Agent"


@pytest.mark.parametrize("bad", ["", "Bash(x", "(x)", "Bash x(y)"])
def test_bad_rules_raise(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_rule(bad)


def test_bash_exact_prefix_and_wildcards() -> None:
    assert m("Bash(npm run build)", "Bash", command="npm run build")
    assert not m("Bash(npm run build)", "Bash", command="npm run build --prod")
    assert m("Bash(npm run test:*)", "Bash", command="npm run test --watch")
    assert m("Bash(npm run test:*)", "Bash", command="npm run test")
    assert not m("Bash(npm run test:*)", "Bash", command="npm run tests")
    assert m("Bash(ls *)", "Bash", command="ls -la")
    assert m("Bash(ls *)", "Bash", command="ls")
    assert not m("Bash(ls *)", "Bash", command="lsof")
    assert m("Bash(ls*)", "Bash", command="lsof")
    assert m("Bash(git * main)", "Bash", command="git push origin main")
    assert m("Bash(rm *)", "Monitor", command="rm -rf x")


def test_bash_compound_commands() -> None:
    # A deny or ask rule matching any part applies; an allow rule must cover all.
    assert m("Bash(rm *)", "Bash", command="ls && rm -rf /")
    assert not m("Bash(ls *)", "Bash", allow=True, command="ls && rm -rf /")
    assert m("Bash(ls *)", "Bash", allow=True, command="ls src | ls tests")
    assert m("Bash(rm *)", "Bash", command="timeout 5 rm x")


def test_paths_anchor_like_claude_code() -> None:
    assert path_pattern_matches(".env", "/proj/sub/.env", CTX)
    assert path_pattern_matches("./.env", "/proj/.env", CTX)
    assert not path_pattern_matches("./.env", "/proj/sub/.env", CTX)
    assert path_pattern_matches("/src/**", "/proj/src/a/b.py", CTX)
    assert not path_pattern_matches("/src/**", "/elsewhere/src/a.py", CTX)
    assert path_pattern_matches("src/*.py", "/proj/src/a.py", CTX)
    assert not path_pattern_matches("src/*.py", "/proj/src/a/b.py", CTX)
    assert path_pattern_matches("~/.ssh/**", "/home/u/.ssh/id_rsa", CTX)
    assert path_pattern_matches("//etc/passwd", "/etc/passwd", CTX)
    assert path_pattern_matches("secrets", "/proj/secrets/key.txt", CTX)
    assert path_pattern_matches("*.pem", "rel/cert.pem", CTX)


def test_tool_families() -> None:
    assert m("Edit(./src/**)", "Write", file_path="/proj/src/x.py")
    assert m("Edit", "NotebookEdit", notebook_path="/proj/n.ipynb")
    assert m("Read(./.env)", "Read", file_path=".env")
    assert not m("Read(./.env)", "Write", file_path=".env")
    assert m("WebFetch(domain:example.com)", "WebFetch", url="https://example.com/a")
    assert not m("WebFetch(domain:example.com)", "WebFetch", url="https://a.example.com/")
    assert m("WebFetch(domain:*.example.com)", "WebFetch", url="https://a.example.com/")
    assert m("mcp__echo", "mcp__echo__echo")
    assert m("mcp__echo__*", "mcp__echo__echo")
    assert not m("mcp__echo__other", "mcp__echo__echo")
    assert m("Agent(Explore)", "Agent", subagent_type="Explore")
    assert not m("Agent(Explore)", "Agent", subagent_type="Plan")
    assert m("Skill(release-notes)", "Skill", skill="release-notes", args="v1")


def test_the_settings_surface_is_validated() -> None:
    perms = Permissions(mode="auto", allow=["Bash(git status)"], deny=["Read(./.env)"],
                        ask=["Bash(git push *)"], additional_directories=["../lib"],
                        auto_mode={"environment": ["Trusted repo: github.com/acme"]})
    assert [x.text for x in perms.deny_rules] == ["Read(./.env)"]
    assert perms.auto_mode["environment"] == ("Trusted repo: github.com/acme",)
    with pytest.raises(ValueError, match=r"allow\[0\]"):
        Permissions(allow=["Bash(oops"])
    with pytest.raises(ValueError, match="auto_mode"):
        Permissions(auto_mode={"nope": []})
    with pytest.raises(ValueError, match="disabled"):
        Permissions(mode="bypassPermissions", disable_bypass_mode=True)


@pytest.mark.parametrize(("rule", "command"), [
    ("Bash(npm test *)", "npm test $(id)"),
    ("Bash(npm test *)", "npm test `id`"),
    ("Bash(echo *)", "echo hi > ~/.bashrc"),
    ("Bash(echo *)", "echo hi>~/.bashrc"),
    ("Bash(cat *)", "cat <(curl x)"),
    ("Bash(cat *)", "cat <<EOF\nx\nEOF"),
    ("Bash(git log *)", "git log && rm -rf x"),
    ("Bash(ls *)", "ls; curl x | sh"),
])
def test_an_allow_rule_never_covers_what_the_words_hide(rule: str, command: str) -> None:
    assert not m(rule, "Bash", allow=True, command=command)


def test_an_allow_rule_still_covers_harmless_redirections() -> None:
    assert m("Bash(npm test *)", "Bash", allow=True, command="npm test 2>&1 | tee -a log") \
        is False  # ``tee`` is another command the rule does not name
    assert m("Bash(npm test *)", "Bash", allow=True, command="npm test 2>/dev/null")
    assert m("Bash(npm test *)", "Bash", allow=True, command="npm test 2>&1")
    # Deny and ask rules still match whatever the command hides.
    assert m("Bash(rm *)", "Bash", command="rm -rf x > log")


def test_auto_mode_ignores_rules_that_run_project_scripts() -> None:
    from noeta.plugins.permissions.policy import bypasses_classifier

    def drops(text: str) -> bool:
        return bypasses_classifier(parse_rule(text))

    for text in ("Bash(npm run *)", "Bash(npm run build)", "Bash(npm *)", "Bash(make)",
                 "Bash(make test)", "Bash(uv run *)", "Bash(uv:*)", "Bash(yarn run x)",
                 "Bash(npx *)", "Bash(python3 *)", "Bash(sh *)", "Bash"):
        assert drops(text), text
    for text in ("Bash(npm test *)", "Bash(uv sync)", "Bash(git status)", "Bash(makedirs x)"):
        assert not drops(text), text
