"""``noeta`` with no arguments: the full-screen terminal UI (Textual)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.cli.config import Settings


def run_tui(settings: Settings, *, cwd: str, resume: str | None = None,
            server: str | None = None, agent: str = "main",
            permission_mode: str | None = None) -> None:
    from noeta.cli.tui.app import NoetaApp

    backend: Any
    if server:
        import os

        from noeta.host.remote import RemoteApp

        backend = RemoteApp(server, token=os.environ.get("NOETA_SERVER_TOKEN"))
    else:
        from noeta.cli.config import from_settings

        backend = from_settings(settings, cwd=cwd, own_tasks_only=True)
    NoetaApp(backend, agent=agent, model=settings.model, cwd=cwd, resume=resume,
             permission_mode=permission_mode, settings=settings).run()
