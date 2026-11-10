"""An error the ``noeta`` command did not expect prints one line and where its
report went, not a traceback; ``--verbose`` / ``NOETA_DEBUG=1`` print the
traceback too; Ctrl-C exits 130 quietly."""

from __future__ import annotations

from pathlib import Path

import pytest

import noeta.cli.main as cli
from noeta.cli import home_dir


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "home"
    monkeypatch.setattr(home_dir, "HOME", where)
    monkeypatch.delenv("NOETA_DEBUG", raising=False)
    return where


def _raise(exc: BaseException) -> None:
    def run(_argv: list[str]) -> None:
        raise exc
    return run  # type: ignore[return-value]


def _exit(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as done:
        cli.main(argv)
    return int(done.value.code or 0)


def test_an_unexpected_error_prints_one_line_and_writes_a_report(
        home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "_main", _raise(RuntimeError("the disk is on fire\nsecond line")))
    assert _exit(["serve", "--port", "1"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    lines = err.splitlines()
    assert lines[0] == "noeta: unexpected error: RuntimeError: the disk is on fire"
    reports = list((home / "logs").glob("crash-*.txt"))
    assert len(reports) == 1 and str(reports[0]) in lines[1]
    assert "NOETA_DEBUG=1" in lines[1]
    text = reports[0].read_text("utf-8")
    assert "Traceback" in text and "RuntimeError: the disk is on fire" in text
    assert "serve --port 1" in text and text.startswith("noeta ")
    assert reports[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("argv", "env"), [(["-p", "hi", "--verbose"], ""), (["-p", "hi"], "1")])
def test_verbose_or_noeta_debug_prints_the_traceback(
        argv: list[str], env: str, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    if env:
        monkeypatch.setenv("NOETA_DEBUG", env)
    monkeypatch.setattr(cli, "_main", _raise(KeyError("x")))
    assert _exit(argv) == 1
    err = capsys.readouterr().err
    assert "Traceback" in err and "noeta: unexpected error: KeyError: 'x'" in err
    assert "the full report is in" in err


def test_an_import_error_not_of_an_extra_is_reported_as_a_crash(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "_main", _raise(ModuleNotFoundError("no module x", name="x")))
    assert _exit([]) == 1
    assert "unexpected error: ModuleNotFoundError: no module x" in capsys.readouterr().err


def test_ctrl_c_exits_130_quietly(monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "_main", _raise(KeyboardInterrupt()))
    assert _exit([]) == 130
    assert capsys.readouterr().err == ""


def test_a_report_that_cannot_be_written_falls_back_to_the_traceback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(home_dir, "HOME", blocker / "home")
    monkeypatch.setattr(cli, "_main", _raise(OSError("nope")))
    assert _exit([]) == 1
    err = capsys.readouterr().err
    assert "Traceback" in err and "the full report is in" not in err
