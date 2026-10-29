"""What an "always" approval of a shell command remembers."""

from __future__ import annotations

import pytest

from noeta.plugins.permissions.rule_specs import rule_spec_from_command, rule_spec_text
from noeta.toolkit.permission_rules import shell_rule_matches


@pytest.mark.parametrize(("command", "rule"), [
    ("npm start --port 3", "npm start *"),
    ("git push origin feat", "git push *"),
    ("ls -la", "ls *"),
    ("ls", "ls *"),
    ("make", "make *"),
    # destructive file programs, interpreters and wrappers: the exact command
    ("rm -rf build", "rm -rf build"),
    ("mv a b", "mv a b"),
    ("python -c print", "python -c print"),
    ("python script.py", "python script.py"),
    ("sudo apt install x", "sudo apt install x"),
    ("bash -c 'echo hi'", "bash -c 'echo hi'"),
    ("FOO=1 make", "FOO=1 make"),
    ("./run.sh --fast", "./run.sh --fast"),
])
def test_rule_spec_from_command(command: str, rule: str) -> None:
    assert rule_spec_from_command(command) == {"rule": rule}


@pytest.mark.parametrize("command", ["ls | cat", "ls 'x", "", "rm *.o", "cat ~/x"])
def test_rule_spec_from_command_remembers_nothing_for_unmatchable_commands(
        command: str) -> None:
    assert rule_spec_from_command(command) is None


def test_a_remembered_approval_never_widens_past_its_rule() -> None:
    def allowed(approved: str, later: str) -> bool:
        spec = rule_spec_from_command(approved)
        assert spec is not None
        return shell_rule_matches(spec["rule"], later, every=True)

    assert allowed("rm -rf build", "rm -rf build")
    assert not allowed("rm -rf build", "rm -rf /")
    assert not allowed("rm -rf build", "rm -rf build /")
    assert not allowed("python -c print", "python -c 'import os'")
    assert allowed("git push origin feat", "git push origin main")
    assert not allowed("git push origin feat", "git pull")
    assert allowed("npm start", "npm start --x")
    assert not allowed("npm start", "npm test")


def test_rule_spec_text() -> None:
    assert rule_spec_text({"rule": "git push *"}) == "git push *"
    assert rule_spec_text({"program": "npm", "subcommand": "start"}) == "npm start *"
    assert rule_spec_text({"program": "make"}) == "make *"
