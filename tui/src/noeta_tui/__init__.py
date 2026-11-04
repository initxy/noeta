"""noeta's terminal UI: ``noeta`` with no arguments opens it (the
entry-point group ``noeta.frontends``, name ``tui``)."""

from __future__ import annotations

from typing import Any

from noeta.host.app import FrontEndStart

__all__ = ["run"]


def run(app: Any, start: FrontEndStart) -> None:
    """Run the terminal UI on ``app`` (an ``AppAPI``: ``LocalApp`` or
    ``RemoteApp``) until the person quits."""
    from noeta_tui.app import NoetaApp

    NoetaApp(app, agent=start.agent, model=start.model, cwd=start.cwd, resume=start.resume,
             permission_mode=start.permission_mode, task_config=start.task_config,
             on_server=start.on_server, start=start).run()
