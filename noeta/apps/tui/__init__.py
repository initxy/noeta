"""``noeta`` with no arguments: the full-screen terminal UI (Textual)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.apps.config import Settings


def run_tui(settings: Settings, *, cwd: str, resume: str | None = None,
            server: str | None = None, agent: str = "main",
            permission_mode: str | None = None) -> None:
    from noeta.apps.config import open_store
    from noeta.apps.local import LocalApp
    from noeta.apps.tui.app import NoetaApp
    from noeta.presets import default_agents

    backend: Any
    if server:
        import os

        from noeta.apps.remote import RemoteApp

        backend = RemoteApp(server, token=os.environ.get("NOETA_SERVER_TOKEN"))
    else:
        from noeta.apps.config import HOME

        backend = LocalApp(open_store(settings.store_url),
                           default_agents(settings.model, memory_root=str(HOME / "memory")),
                           settings.make_provider(), own_tasks_only=True)
    NoetaApp(backend, agent=agent, model=settings.model, cwd=cwd, resume=resume,
             permission_mode=permission_mode).run()
