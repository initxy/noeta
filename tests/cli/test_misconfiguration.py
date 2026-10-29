"""Common misconfigurations end the command with one line naming what to set,
not a traceback."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _env(tmp_path: Path, **more: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("NOETA_", "ANTHROPIC_", "OPENAI_"))}
    env.update(NOETA_HOME=str(home), HOME=str(tmp_path / "user"),
               NOETA_STORE=f"sqlite:///{tmp_path / 't.db'}", **more)
    return env


@pytest.mark.parametrize(("provider", "also"), [
    ({"NOETA_PROVIDER": "openai_compat", "NOETA_BASE_URL": "http://127.0.0.1:9/v1"},
     "OPENAI_API_KEY"),
    ({}, "ANTHROPIC_API_KEY"),
])
def test_no_api_key_names_the_variables_to_set(
        tmp_path: Path, provider: dict[str, str], also: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "noeta.cli.main", "-p", "hi", "--cwd", str(tmp_path)],
        capture_output=True, text=True, timeout=120, check=False,
        env=_env(tmp_path, NOETA_MODEL="sonnet", **provider))
    assert done.returncode == 1, done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    line = done.stderr.strip().splitlines()[-1]
    assert line.startswith("noeta: no API key") and "NOETA_API_KEY" in line and also in line
