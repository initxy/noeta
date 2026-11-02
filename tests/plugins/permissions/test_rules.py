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
    assert m("Skill(release-notes)", "Skill", skill=" /release-notes")


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


@pytest.mark.parametrize(("rule", "command"), [
    ("Bash(rm *)", "\\rm -rf x"),
    ("Bash(rm *)", "'rm' -rf x"),
    ("Bash(rm *)", '"rm" -rf x'),
    ("Bash(rm *)", "r\\m -rf x"),
    ("Bash(rm *)", "command rm -rf x"),
    ("Bash(rm *)", "env rm -rf x"),
    ("Bash(rm *)", "env -i X=1 rm -rf x"),
    ("Bash(rm *)", "X=1 rm -rf x"),
    ("Bash(rm *)", "builtin exec rm -rf x"),
    ("Bash(rm *)", "nohup time -p rm -rf x"),
    ("Bash(rm *)", "timeout -s KILL 5 rm -rf x"),
    ("Bash(rm *)", "ls && 'rm' -rf x"),
    ("Bash(git push *)", "\\git push origin main"),
    ("Bash(git push *)", "g'i't push origin main"),
    ("Bash(git push *)", "git 'push' origin main"),
    ("Bash(git push:*)", "command git push"),
    ("Bash(git push *)", "git -C . push origin main"),
    ("Bash(git push *)", "git -c core.x=y --no-pager -P push origin main"),
    ("Bash(git push *)", "git --git-dir=.git --work-tree . push origin main"),
    ("Bash(git push *)", "env git -C sub --namespace n push origin main"),
])
def test_deny_and_ask_rules_see_through_quotes_and_runners(rule: str, command: str) -> None:
    assert m(rule, "Bash", command=command)
    assert not m(rule, "Bash", allow=True, command=command)


def test_a_lookup_is_not_a_run() -> None:
    assert not m("Bash(rm *)", "Bash", command="command -v rm")
    assert not m("Bash(rm *)", "Bash", command="echo rm")


def test_auto_mode_ignores_rules_that_run_project_scripts() -> None:
    from noeta.plugins.permissions.policy import bypasses_classifier

    def drops(text: str) -> bool:
        return bypasses_classifier(parse_rule(text))

    for text in ("Bash(npm run *)", "Bash(npm run:*)", "Bash(npm *)", "Bash(make *)",
                 "Bash(uv run *)", "Bash(uv:*)", "Bash(yarn run x *)",
                 "Bash(npx *)", "Bash(python3 *)", "Bash(sh *)", "Bash"):
        assert drops(text), text
    # An exact command stays, as in Claude Code.
    for text in ("Bash(npm test *)", "Bash(uv sync)", "Bash(git status)", "Bash(makedirs x)",
                 "Bash(uv run pytest)", "Bash(npm run build)", "Bash(make)", "Bash(make test)",
                 "Bash(python3 -m pytest)"):
        assert not drops(text), text


def test_a_rule_with_an_mcp_tools_old_name_still_matches(
        caplog: pytest.LogCaptureFixture) -> None:
    """MCP names now keep the server's case and are not cut: a rule written
    under the old naming (alias lower-cased and cut to 32 characters, names
    cut to 64 with a hash) still covers the tool, and the log says so."""
    import hashlib

    assert m("mcp__github__delete_repo", "mcp__GitHub__delete_repo")
    assert m("mcp__github", "mcp__GitHub__delete_repo")
    assert m("mcp__github__*", "mcp__GitHub__delete_repo")
    assert not m("mcp__gitlab", "mcp__GitHub__delete_repo")
    assert "only by its old name" in caplog.text
    server = "Very-Long-Server-Name-With-Caps-ABCDEFGHIJ"
    assert m(f"mcp__{server.lower()[:32]}", f"mcp__{server}__t")
    raw = "q" * 70
    old = "mcp__gh__" + raw[:46] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:8]
    assert m(old, f"mcp__gh__{raw}")
    # Rules for tools other than MCP ones are not widened.
    assert not m("bash", "Bash", command="ls")
