"""Where a shell command's paths sit among its words, as Claude Code's
shell path check reads them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.plugins.permissions.shell_paths import command_paths
from noeta.toolkit.shell_syntax import parse


def paths(command: str) -> tuple[list[str], list[str]]:
    [cmd] = parse(command).commands
    found = command_paths(cmd.words)
    return [w.text for w in found.writes], [w.text for w in found.reads]


@pytest.mark.parametrize(("command", "writes", "reads"), [
    ("cp -t dest a b", ["dest", "a", "b"], ["dest", "a", "b"]),
    ("cp --target-directory=d a", ["a", "d"], ["a"]),
    ("mv a", ["a"], []),
    ("tee -a /dev/null log", ["log"], []),
    ("sed -f prog.sed -i f", ["prog.sed", "f"], ["prog.sed", "f"]),
    ("sed -e s/a/b/ f", [], ["f"]),
    ("git -C sub diff --stat a -- b", [], ["sub/a", "sub/b"]),
    ("git --work-tree=w grep -e x", [], ["w"]),
    ("git -c a=b grep x p", [], ["p"]),
    ("git status", [], []),
    ("rg x", [], ["."]),
    ("rg -g '*.py' x src", [], ["src"]),
    ("grep -r x", [], ["."]),
    ("grep --exclude-from=ex.txt x f", [], ["ex.txt", "f"]),
    ("grep --exclude-from ex.txt x f", [], ["ex.txt", "f"]),
    ("grep -rf pats.txt f", [], ["pats.txt", "f"]),
    ("grep -fpats.txt f", [], ["pats.txt", "f"]),
    ("grep -ex f", [], ["f"]),
    ("grep -- -x f", [], ["f"]),
    ("jq --slurpfile v data.json . in.json", [], ["data.json", "in.json"]),
    ("jq -f prog.jq in.json", [], ["prog.jq", "in.json"]),
    ("jq --from-file=prog.jq in.json", [], ["prog.jq", "in.json"]),
    ("awk -F , -f prog.awk in.csv", [], ["prog.awk", "in.csv"]),
    ("awk -e 1 -- in.csv", [], ["in.csv"]),
    ("find . -newer ref -name x", [], [".", "ref"]),
    ("find -L -- a", [], ["a"]),
    ("find -type f", [], ["."]),
    ("man -l page.1", [], ["page.1"]),
    ("man ls ./local.1", [], ["./local.1"]),
    ("tr -d x f", [], ["f"]),
    ("cd", [], ["~"]),
    ("ls", [], ["."]),
    ("time nohup -- cat a", [], ["a"]),
    ("timeout -s KILL 5s cat a", [], ["a"]),
    ("timeout x cat a", [], []),
    ("nice -5 -- cat a", [], ["a"]),
    ("nice cat a", [], ["a"]),
    ("stdbuf -o L cat a", [], ["a"]),
    ("env -u X B=1 cat a", [], ["a"]),
    ("command -p cat a", [], ["a"]),
    ("command -v cat", [], []),
    ("noglob cat a", [], ["a"]),
    ("python a.py", [], []),
    ("nice --adjustment=5 rm a", ["a"], []),
    ("nice -n 5 -- rm a", ["a"], []),
    ("xargs -n 1 rm a", ["a"], []),
    ("sudo -u root rm a", ["a"], []),
    ("ln -sf /etc/passwd a", ["/etc/passwd", "a"], []),
    ("install -m644 a b", ["a", "b"], ["a"]),
    ("stdbuf --output=L -- cat a", [], ["a"]),
])

def test_command_paths(command: str, writes: list[str], reads: list[str]) -> None:
    assert paths(command) == (writes, reads)


def test_trees_and_env_chdir() -> None:
    [cmd] = parse("env -C config rm -rf sub").commands
    found = command_paths(cmd.words)
    assert found.chdir == "config" and [w.text for w in found.trees] == ["sub"]
    [cmd] = parse("mv a b dest").commands
    assert [w.text for w in command_paths(cmd.words).trees] == ["a", "b"]
    [cmd] = parse("rm -f a").commands
    assert command_paths(cmd.words).trees == []


async def test_resolve_paths_expands_globs_and_links_in_one_call(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from noeta.plugins.permissions.shell_check import resolve_paths

    (tmp_path / "a.toml").write_text("")
    (tmp_path / "b.toml").write_text("")
    (tmp_path / "l").symlink_to(tmp_path / "a.toml")
    view: Any = SimpleNamespace(env=None)
    got = await resolve_paths(view, [(f"{tmp_path}/*.toml", True), (f"{tmp_path}/l", False)])
    assert (f"{tmp_path}/a.toml", f"{tmp_path}/a.toml") in got
    assert (f"{tmp_path}/b.toml", f"{tmp_path}/b.toml") in got
    assert (f"{tmp_path}/l", f"{tmp_path}/a.toml") in got

    class Failing:
        async def run(self, *_a: Any, **_k: Any) -> Any:
            raise OSError("down")

        async def realpath(self, path: str) -> str:
            return path + ".real"

    view = SimpleNamespace(env=Failing())
    assert await resolve_paths(view, [("/x", False)]) == [("/x", "/x.real")]
