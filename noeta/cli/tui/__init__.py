"""``noeta`` with no arguments: the full-screen terminal UI (Textual)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.cli.config import Settings


def run_tui(settings: Settings, *, cwd: str, resume: str | None = None,
            server: str | None = None, agent: str = "main",
            permission_mode: str | None = None,
            task_config: dict[str, Any] | None = None) -> None:
    """``task_config``: with ``server``, what new tasks ask of it (a ``cwd``
    there, a ``model``); the server's own choices otherwise."""
    from noeta.cli.tui.app import NoetaApp

    backend: Any
    if server:
        from noeta.cli.server_token import server_token
        from noeta.host.remote import RemoteApp

        backend = RemoteApp(server, token=server_token())
    else:
        from noeta.cli.config import from_settings

        backend = from_settings(settings, cwd=cwd, own_tasks_only=True)
    config = dict(task_config or {}) if server else {"cwd": cwd, "model": settings.model}
    NoetaApp(backend, agent=agent, model=str(config.get("model") or "the server's model"),
             cwd=cwd, resume=resume, permission_mode=permission_mode, settings=settings,
             task_config=config, on_server=bool(server)).run()
