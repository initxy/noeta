"""A command whose optional dependency is not installed names the extra to
install instead of failing with a raw ``ModuleNotFoundError``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from noeta.cli import main as cli


def _hide(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    """Make ``import <module>`` fail as if it were not installed, and drop
    the cached noeta modules that import it, so they import again."""
    for name in list(sys.modules):
        if name.startswith(tuple(f"{m}." for m in modules)) or name.startswith(
                ("noeta.host.", "noeta.backends.stores.postgres", "noeta.cli.tui")):
            monkeypatch.delitem(sys.modules, name)
    for name in modules:
        monkeypatch.setitem(sys.modules, name, None)


def _exit_message(argv: list[str]) -> str:
    with pytest.raises(SystemExit) as done:
        cli.main(argv)
    return str(done.value.code)


@pytest.mark.parametrize(("argv", "hidden", "extra"), [
    (["serve"], ("uvicorn",), "server"),
    (["serve"], ("starlette",), "server"),
    (["acp"], ("acp",), "acp"),
    (["acp", "--server", "http://127.0.0.1:1"], ("acp",), "acp"),
    ([], ("textual",), "tui"),
])
def test_a_command_names_its_missing_extra(argv: list[str], hidden: tuple[str, ...], extra: str,
                                           tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    _hide(monkeypatch, *hidden)
    message = _exit_message(["--model", "m", "--cwd", str(tmp_path), *argv])
    assert f"pip install 'noeta[{extra}]'" in message
    assert "Traceback" not in message


def test_a_postgres_store_names_the_postgres_extra(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    _hide(monkeypatch, "psycopg", "psycopg_pool")
    message = _exit_message(["--model", "m", "--cwd", str(tmp_path),
                             "--store", "postgres://u@127.0.0.1:1/db", "worker"])
    assert "pip install 'noeta[postgres]'" in message


def test_another_missing_module_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the extras' own modules are translated; a bug importing
    something else still raises."""
    def broken(_argv: list[str]) -> None:
        raise ModuleNotFoundError("No module named 'nowhere'", name="nowhere")

    monkeypatch.setattr(cli, "_front", broken)
    with pytest.raises(ModuleNotFoundError):
        cli.main(["--model", "m"])
