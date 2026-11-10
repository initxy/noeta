"""An error that ends the terminal UI is handed back to the ``noeta`` command
(which prints one line and writes a crash report) instead of Textual's
traceback with every frame's locals."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

import noeta_tui
from noeta.host.app import FrontEndStart
from noeta.testing import ScriptedProvider
from noeta_tui.app import NoetaApp

from support import make_app


async def test_an_error_is_kept_and_textual_prints_no_traceback(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider())

    def boom() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        async with app.run_test() as pilot:
            app.call_later(boom)
            await pilot.pause()
            await pilot.pause()
    assert isinstance(app.crashed, RuntimeError)
    assert not app._exit_renderables


def test_run_raises_the_error_after_the_app_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    def crashed_run(self: NoetaApp, *_a: Any, **_kw: Any) -> None:
        self.crashed = ValueError("broken")

    monkeypatch.setattr(NoetaApp, "run", crashed_run)
    monkeypatch.setattr(NoetaApp, "__init__", lambda self, *_a, **_kw: None)
    with pytest.raises(ValueError, match="broken"):
        noeta_tui.run(object(), FrontEndStart(agent="main", cwd="/", model="m"))
