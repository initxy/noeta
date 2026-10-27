"""``noeta`` with no arguments: the full-screen terminal UI (Textual)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.config import Settings


def run_tui(settings: Settings, *, cwd: str, resume: str | None = None,
            server: str | None = None, agent: str = "main") -> None:
    from noeta.config import open_store
    from noeta.local import LocalApp
    from noeta.presets import main_agent
    from noeta.tui.app import NoetaApp

    backend: Any
    if server:
        import os

        from noeta.remote import RemoteApp

        backend = RemoteApp(server, token=os.environ.get("NOETA_SERVER_TOKEN"))
    else:
        backend = LocalApp(open_store(settings.store_url), [main_agent(settings.model)],
                           settings.make_provider())
    NoetaApp(backend, agent=agent, model=settings.model, cwd=cwd, resume=resume).run()
