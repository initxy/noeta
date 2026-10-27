"""``ContainerExecEnv`` keeps variable values off the ``docker`` command
line (where ``ps`` shows them to every user): ``-e KEY`` names a variable,
its value travels in the client's environment. Driven through a fake
``docker`` that records what it was given, so no daemon is needed."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.execenv.container import ContainerExecEnv, start_container


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
