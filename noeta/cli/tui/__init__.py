"""``noeta`` with no arguments: the full-screen terminal UI (Textual)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.cli.config import Settings


def run_tui(settings: Settings, *, cwd: str, resume: str | None = None,
            server: str | None = None, agent: str | None = None,
            permission_mode: str | None = None,
            task_config: dict[str, Any] | None = None,
            plugin_dirs: Sequence[str] = ()) -> None:
    """``task_config``: with ``server``, what new tasks ask of it (a ``cwd``
    there, a ``model``); the server's own choices otherwise. ``agent``: the
    agent new tasks run, else the settings' (``LocalApp.main_agent``), else
    ``main``. ``plugin_dirs``: ``--plugin-dir``."""
    from noeta.cli.tui.app import NoetaApp

    backend: Any
    if server:
        from noeta.cli.server_token import server_problem, server_token
        from noeta.host.remote import RemoteApp

        token = server_token()
        if (problem := server_problem(server, token)) is not None:
            raise SystemExit(f"noeta: {problem}")
        backend = RemoteApp(server, token=token)
    else:
        from noeta.cli.config import from_settings

        backend = from_settings(settings, cwd=cwd, own_tasks_only=True,
                                plugin_dirs=plugin_dirs)
    config = dict(task_config or {}) if server else {"cwd": cwd, "model": settings.model}
    agent = agent or getattr(backend, "main_agent", None) or "main"
    NoetaApp(backend, agent=agent, model=str(config.get("model") or "the server's model"),
             cwd=cwd, resume=resume, permission_mode=permission_mode, settings=settings,
             task_config=config, on_server=bool(server)).run()
