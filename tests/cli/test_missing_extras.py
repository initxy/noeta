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
                ("noeta.host.", "noeta.backends.stores.postgres")):
            module = sys.modules[name]
            parent, _, child = name.rpartition(".")
            if parent in sys.modules and getattr(sys.modules[parent], child, None) is module:
                # Importing it again rebinds the package attribute; put it
                # back with the module, or ``import a.b as c`` and
                # ``from a.b import x`` see two different modules afterwards.
                monkeypatch.setattr(sys.modules[parent], child, module)
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
])
def test_a_command_names_its_missing_extra(argv: list[str], hidden: tuple[str, ...], extra: str,
                                           tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    _hide(monkeypatch, *hidden)
    message = _exit_message(["--model", "m", "--cwd", str(tmp_path), *argv])
    assert f"pip install 'noeta[{extra}]'" in message
    # A reinstall keeps only the extras it names: the README's defaults stay.
    wanted = ",".join(dict.fromkeys((*cli.README_EXTRAS, extra)))
    assert f"uv tool install --reinstall 'noeta[{wanted}]'" in message
    assert "every extra" in message
    assert "Traceback" not in message


def test_a_postgres_store_names_the_postgres_extra(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    _hide(monkeypatch, "psycopg", "psycopg_pool")
    message = _exit_message(["--model", "m", "--cwd", str(tmp_path),
                             "--store", "postgres://u@127.0.0.1:1/db", "worker"])
    assert "pip install 'noeta[postgres]'" in message


def test_noeta_plugins_names_the_workflow_extra_when_its_engine_is_missing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    cli.main(["--cwd", str(tmp_path), "plugins"])
    assert "(missing)" not in capsys.readouterr().err
    monkeypatch.setattr(cli.importlib.util, "find_spec",
                        lambda name: None if name == "py_mini_racer" else object())
    cli.main(["--cwd", str(tmp_path), "plugins"])
    err = capsys.readouterr().err
    assert "(missing) workflow: the py_mini_racer module is not installed" in err
    assert "pip install 'noeta[workflow]'" in err


def test_another_missing_module_is_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """Only the extras' own modules are translated; a bug importing
    something else is reported as the unexpected error it is."""
    from noeta.cli import home_dir

    def broken(_argv: list[str]) -> None:
        raise ModuleNotFoundError("No module named 'nowhere'", name="nowhere")

    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    monkeypatch.setattr(cli, "_front", broken)
    assert _exit_message(["--model", "m"]) == "1"
    err = capsys.readouterr().err
    assert "unexpected error: ModuleNotFoundError: No module named 'nowhere'" in err
    assert "extra" not in err


def test_the_terminal_ui_is_found_by_its_entry_point() -> None:
    import noeta_tui

    assert cli.front_end("tui") is noeta_tui.run


def test_a_missing_terminal_ui_names_its_package(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.core import registry

    def missing(group: str, name: str) -> None:
        raise registry.UnknownName(f"no {group} entry point named {name!r}")

    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(registry, "load", missing)
    message = _exit_message(["--model", "m", "--cwd", str(tmp_path)])
    assert "noeta-tui, which is not installed" in message
    assert "uv tool install --reinstall 'noeta[tui]'" in message
    assert "pip install 'noeta-tui'" in message
