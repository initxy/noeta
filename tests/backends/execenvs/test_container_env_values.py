"""``ContainerExecEnv`` keeps variable values off the ``docker`` command
line (where ``ps`` shows them to every user): ``-e KEY`` names a variable,
its value travels in the client's environment; its batch ``stat`` works
with GNU and BusyBox ``stat``. Driven through a fake ``docker`` (one that
records what it was given, or runs the command here), so no daemon is
needed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from noeta.backends.execenvs.container import ContainerExecEnv, start_container


def _fake_docker(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "calls"
    fake = tmp_path / "docker"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" "secret=$SECRET" >> {log}\n')
    fake.chmod(0o755)
    return str(fake), log


async def test_exec_values_are_not_on_the_command_line(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)
    env = ContainerExecEnv("box", env={"SECRET": "base"}, docker=docker)
    result = await env.run(["true"], env={"SECRET": "s3cr3t", "LANG": "C.UTF-8"})
    assert result.exit_code == 0
    argv, value = log.read_text().splitlines()[:2]
    assert "s3cr3t" not in argv and "base" not in argv
    assert " -e SECRET " in f" {argv} " and "-e LANG=C.UTF-8" in argv
    assert value == "secret=s3cr3t"


async def test_start_container_values_are_not_on_the_command_line(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)
    await start_container("img", name="box", env={"SECRET": "s3cr3t"}, docker=docker)
    argv, value = log.read_text().splitlines()[:2]
    assert "s3cr3t" not in argv and value == "secret=s3cr3t"


def _local_docker(tmp_path: Path, shell: str, bin_dir: Path) -> str:
    """A ``docker`` that runs the ``sh -c`` it is given here, with ``bin_dir``
    first on the path and ``shell`` as the shell."""
    fake = tmp_path / "docker-local"
    fake.write_text('#!/bin/sh\nwhile [ "$1" != sh ]; do shift; done; shift\n'
                    f'PATH="{bin_dir}:$PATH" exec {shell} "$@"\n')
    fake.chmod(0o755)
    return str(fake)


@pytest.mark.parametrize("stat", ["gnu", "busybox"])
async def test_stat_many_works_with_gnu_and_busybox_stat(tmp_path: Path, stat: str) -> None:
    # BusyBox ``stat`` (Alpine images) has no ``--printf``: the batch then
    # asks path by path in the same command.
    busybox = shutil.which("busybox")
    if stat == "busybox" and busybox is None:
        pytest.skip("busybox not installed")
    if stat == "gnu" and subprocess.run(["stat", "--version"], capture_output=True,
                                        check=False).returncode != 0:
        pytest.skip("this machine's stat is not GNU's (macOS: BSD's)")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if stat == "busybox":
        assert busybox is not None
        (bin_dir / "stat").symlink_to(busybox)
    shell = f"{busybox} sh" if stat == "busybox" else "/bin/sh"
    work = tmp_path / "w"
    (work / "sub").mkdir(parents=True)
    (work / "a b.txt").write_text("hello")
    env = ContainerExecEnv("box", cwd=str(work), docker=_local_docker(tmp_path, shell, bin_dir))
    one, sub, gone = await env.stat_many(["a b.txt", "sub", "nope"])
    assert one is not None and one.size == 5 and not one.is_dir
    assert int(one.mtime) == int((work / "a b.txt").stat().st_mtime)
    assert sub is not None and sub.is_dir and gone is None
