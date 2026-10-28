"""What a shell command or a path reaches: read-only commands, file
commands and protected paths."""

from __future__ import annotations

import pytest

from noeta.plugins.permissions.scope import (
    Scope,
    file_command_paths,
    is_protected,
    parts,
    read_only,
)

S = Scope(dirs=("/proj", "/lib"), home="/home/u")


@pytest.mark.parametrize("command", [
    "ls", "cat a.txt; echo \"---\"; ls /proj/tests 2>/dev/null || echo none",
    "sleep 2; echo done", "find . -name '*.py' | xargs wc -l", "git status",
    "git log --oneline -5", "grep -rn foo src", "cat /lib/x", "git diff HEAD~1",
    # Commands Claude Code ran without its classifier in the recordings.
    "find . -name .venv -prune -o -name '.*' -type d -prune -o -name '__pycache__' -prune "
    "-o -name '*.py' -type f -print0 | xargs -0 wc -l",
    "find . \\( -name '.venv' -o -name '.*' \\) -prune -o -name '*.py' -type f -print0 "
    "| xargs -0 wc -l",
    "find /proj -name '*.py' -type f | sort",
    "pgrep -f \"sleep 600\" && echo \"STILL RUNNING\" || echo \"NOT RUNNING\"",
    "sleep 25; echo bg-done &", "ls -la src", "head -n 5 a.txt", "cd src && cat a.py",
    "git -C /proj log -p", "rg -n foo src", "wc -l src/*.py", "sort -u a.txt | uniq -c",
    "git branch --list 'x*'", "git config --get user.name", "ls 2>&1 | head -2",
    "git log --format=%H -n 3 -- src", "jq -r .name package.json", "date +%Y",
    # A leading glob where no flag of the program can do harm, a pattern flag's glob,
    # and ``[``.
    "ls *.py", "cat *.md", "wc -l *.py", "ls -d */", "head -n 3 *.txt", "cat *", "ls -- *",
    "grep -rn foo --include=*.py .", "grep -rn foo --include *.py --exclude-dir=.* .",
    "rg foo -g *.py", "rg foo --glob *.test.ts src", "rg foo --iglob=*.PY",
    "[ -d src ]", "[ -f a.txt ] && cat a.txt", "test -d src", "du -sh *", "stat *.py",
])
def test_read_only_inside(command: str) -> None:
    assert read_only(command, S)


#: Each one writes a file, runs a program the text does not show, or reads
#: outside the working directories: none may skip the permission check.
ADVERSARIAL = [
    # The review's six.
    "echo x>~/.bashrc", "cat p>.git/hooks/pre-commit", "sort -S1M --compress-program=sh p",
    "git grep --open-files-in-pager=sh x", "uniq p .git/config", "tree -o out",
    # Redirections, attached or not.
    "echo hi > out.txt", "ls >| f", "ls &> f", "ls &>> f", "ls 2> f", "ls 2>>f",
    "cat <> f", "ls 3>f", "cat a 1>&2 2>x", "git log --format=%H > out", "ls >&f",
    "tail -f a > b", "cat < /etc/passwd",
    # Substitutions, heredocs, subshells, comments, expansions.
    "cat <<EOF\nx\nEOF", "cat <<< x", "echo $(whoami)", "cat `id`", "cat <(id)",
    "diff a >(tee x)", "echo \"$(id)\"", "(ls)", "{ ls; }", "ls # x", "cat $HOME/x",
    "cat ${X}", "echo {a,b}", "ls ~root",
    # A leading glob may expand to a flag of a program that runs or writes.
    "sort *", "rg x *", "uniq *", "tree *", "diff * x", "sort -- *", "rg x -g *",
    "rg x -g *sh", "rg x -g */*.py", "rg x -g*.py", "rg x -g *.{py,sh}",
    "cat $X*", "ls *.{py,md}", "[ -f $X ]", "[ -f a ] > x",
    # Programs that run others or write, and their flags.
    "sort -o out a", "sort --output=x a", "sort -T /tmp a", "git grep -O x", "git grep -Osh x",
    "git -c core.pager=sh log", "git --exec-path=/x log", "git --config-env=a=b log",
    "git -p log", "git diff --output=x", "git log --output=/tmp/x", "git diff --ext-diff",
    "git grep --no-index x /etc", "find . -exec rm {} +", "find . -execdir sh {} +",
    "find . -delete", "find . -fprint out", "find . -files0-from x", "rg --pre cat x",
    "rg --pre=sh x", "xargs rm", "find . | xargs sh -c id", "xargs -I{} sh -c {}",
    "env ls", "command ls", "nice ls", "less a", "yq -i . a", "printenv", "date -s 2020",
    "hostname evil", "jq -n env", "jq -f x.jq a", "file -C -m x", "awk '{print}' a",
    "sed -n p a", "python3 -c 'print(1)'", "FOO=1 ls", "GIT_EXTERNAL_DIFF=sh git diff",
    "./cat a", "/bin/cat a", "for i in 1 2; do echo $i; done",
    # Writes git can do.
    "git push", "git tag v1", "git branch new", "git branch -D x", "git reflog expire --all",
    "git config user.name x", "git stash drop", "git remote add x y",
    # Reads outside, including through a changed directory.
    "ls /etc", "cat ~/.ssh/id_rsa", "cat ../other/x", "cd .. && cat x", "cd /etc; cat passwd",
    "cd sub | cat ../../etc/passwd", "cd nonexistent; cat ../x", "cd -; cat x",
    "pushd /etc && cat passwd", "find . | sed s/a/b/",
]


@pytest.mark.parametrize("command", ADVERSARIAL)
def test_not_read_only(command: str) -> None:
    assert not read_only(command, S)


def test_the_adversarial_table_is_wide() -> None:
    assert len(ADVERSARIAL) >= 40


def test_relative_paths_resolve_from_the_shells_directory() -> None:
    assert read_only("cat x", S.at("/proj/sub"))
    assert not read_only("cat ../../etc/passwd", S.at("/proj/sub"))
    assert read_only("cat ../a", S.at("/proj/sub"))


def test_file_command_paths() -> None:
    assert file_command_paths("mkdir -p out", S) == ["/proj/out"]
    assert file_command_paths("sed -i s/a/b/ src/x.py", S) == ["/proj/src/x.py"]
    assert file_command_paths("sed s/a/b/ src/x.py", S) is None
    assert file_command_paths("rm $HOME/x", S) is None
    # What a file command writes besides its paths makes it not one.
    assert file_command_paths("cp a b>~/.bashrc", S) is None
    assert file_command_paths("sed -i '1e touch x' a", S) is None
    assert file_command_paths("sed -i 's/a/b/w /tmp/x' a", S) is None
    assert file_command_paths("sed -i'/tmp/*' s/a/b/ a", S) is None
    assert file_command_paths("rm -r .*", S) is None
    assert file_command_paths("mv --target-directory=/x a", S) is None
    assert file_command_paths("mv -t /x a", S) == ["/x", "/proj/a"]


def test_a_changed_directory_moves_later_paths() -> None:
    found = parts("cd .git && sed -i s/a/b/ config", S)
    assert found is not None
    assert found[1].writes == ("/proj/config", "/proj/.git/config")
    assert any(is_protected(p) for p in found[1].writes)


@pytest.mark.parametrize(("path", "protected"), [
    ("/proj/.git/config", True), ("/proj/.vscode/tasks.json", True),
    ("/proj/.claude/settings.json", True), ("/proj/.claude/skills/x/SKILL.md", False),
    ("/proj/.envrc", True), ("/proj/sub/.bashrc", True), ("/proj/src/a.py", False),
    ("/home/u/.config/git/config", True), ("/proj/.noeta/agents/main.md", True),
    ("/proj/.noeta/skills/x/SKILL.md", False), ("/proj/.GIT/hooks/pre-commit", True),
    ("/proj/.noeta/settings.json", True),
    ("/proj/.pre-commit-config.yaml", True),
])
def test_protected_paths(path: str, protected: bool) -> None:
    assert is_protected(path) is protected
