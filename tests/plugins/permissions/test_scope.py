"""What a shell command or a path reaches: read-only commands, file
commands and protected paths."""

from __future__ import annotations

import pytest

from noeta.plugins.permissions.scope import Scope, file_command_paths, is_protected, read_only

S = Scope(dirs=("/proj", "/lib"), home="/home/u")


@pytest.mark.parametrize("command", [
    "ls", "cat a.txt; echo \"---\"; ls /proj/tests 2>/dev/null || echo none",
    "sleep 2; echo done", "find . -name '*.py' | xargs wc -l", "git status",
    "git log --oneline -5", "grep -rn foo src", "cat /lib/x", "git diff HEAD~1",
])
def test_read_only_inside(command: str) -> None:
    assert read_only(command, S)


@pytest.mark.parametrize("command", [
    "ls /etc", "cat ~/.ssh/id_rsa", "cat ../other/x", "find . -delete", "echo hi > out.txt",
    "cat <<EOF\nx\nEOF", "echo $(whoami)", "python3 -c 'print(1)'", "git push",
    "find . | sed s/a/b/", "sort -o out a", "git branch -D x", "FOO=1 ls",
    "for i in 1 2; do echo $i; done", "rg --pre cat x",
])
def test_not_read_only(command: str) -> None:
    assert not read_only(command, S)


def test_file_command_paths() -> None:
    assert file_command_paths("mkdir -p out", S) == ["/proj/out"]
    assert file_command_paths("sed -i s/a/b/ src/x.py", S) == ["/proj/src/x.py"]
    assert file_command_paths("sed s/a/b/ src/x.py", S) is None
    assert file_command_paths("rm $HOME/x", S) is None


@pytest.mark.parametrize(("path", "protected"), [
    ("/proj/.git/config", True), ("/proj/.vscode/tasks.json", True),
    ("/proj/.claude/settings.json", True), ("/proj/.claude/skills/x/SKILL.md", False),
    ("/proj/.envrc", True), ("/proj/sub/.bashrc", True), ("/proj/src/a.py", False),
    ("/home/u/.config/git/config", True), ("/proj/.noeta/agents/main.md", True),
    ("/proj/.pre-commit-config.yaml", True),
])
def test_protected_paths(path: str, protected: bool) -> None:
    assert is_protected(path) is protected
