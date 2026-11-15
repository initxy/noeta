"""Warnings noeta logs, on stderr as ``noeta: warning: …`` lines: coloured on
a terminal, plain otherwise or under ``NO_COLOR``; once, however often the
command installs it; info lines stay out."""

from __future__ import annotations

import io
import logging

import pytest

from noeta.cli import stderr_log


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _say(monkeypatch: pytest.MonkeyPatch, stream: io.StringIO) -> str:
    monkeypatch.setattr("sys.stderr", stream)
    stderr_log.install()
    stderr_log.install()
    log = logging.getLogger("noeta.test_stderr_log")
    log.info("not shown")
    log.warning("no price is known for model %r", "m")
    log.error("the store is gone")
    return stream.getvalue()


def test_lines_are_plain_off_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _say(monkeypatch, io.StringIO()) == (
        "noeta: warning: no price is known for model 'm'\n"
        "noeta: error: the store is gone\n")


def test_a_terminal_gets_a_coloured_label_and_a_dimmed_warning(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    warning, error = _say(monkeypatch, Terminal()).splitlines()
    assert warning == ("\x1b[2mnoeta:\x1b[0m \x1b[1;33mwarning:\x1b[0m "
                       "\x1b[2mno price is known for model 'm'\x1b[0m")
    assert error == "\x1b[2mnoeta:\x1b[0m \x1b[1;31merror:\x1b[0m the store is gone"


def test_no_color_keeps_a_terminal_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert _say(monkeypatch, Terminal()).startswith("noeta: warning: ")
