"""noeta's terminal UI: ``noeta`` with no arguments opens it (the
entry-point group ``noeta.frontends``, name ``tui``)."""

from __future__ import annotations

from typing import Any

from noeta.host.app import FrontEndStart

__all__ = ["run"]


def run(app: Any, start: FrontEndStart) -> None:
    """Run the terminal UI on ``app`` (an ``AppAPI``: ``LocalApp`` or
    ``RemoteApp``) until the person quits; exits with the app's code when it
    is not 0 (``-c`` with nothing to continue). An error that ended it is
    raised again here, after the terminal is restored."""
    from noeta_tui.app import NoetaApp

    tui = NoetaApp(app, agent=start.agent, model=start.model, cwd=start.cwd,
                   resume=start.resume, permission_mode=start.permission_mode,
                   task_config=start.task_config, on_server=start.on_server, start=start)
    tui.run()
    if tui.crashed is not None:
        raise tui.crashed
    if tui.return_code:
        raise SystemExit(tui.return_code)
