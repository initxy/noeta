"""Which file wins when several name an MCP server of one name: one order for
the MCP plugin (which starts servers) and ``noeta mcp`` (which lists, gets
and removes them), so the two never pick different servers.

Claude Code's order, highest first: managed > local > project > user. In
noeta's places: the settings files keep the precedence they have for every
other key (lowest first: the user's, the project's ``settings.json``, its
``settings.local.json``, ``--settings``, the managed file, as
``noeta/cli/settings_files.py`` lists them), so the managed file wins and
``--settings`` ranks above local, as it does for every setting; a project's
``.noeta/mcp.json`` (Claude Code's project scope, ``.mcp.json``) ranks just
below that project's own settings files, above the user's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

__all__ = ["PROJECT_FILE", "mcp_sources", "own_settings_file"]

#: A project's MCP servers, relative to its directory.
PROJECT_FILE = ".noeta/mcp.json"


def own_settings_file(path: str | Path, project: str | Path) -> bool:
    """``path`` is one of ``project``'s own settings files (in its
    ``.noeta``)."""
    try:
        return Path(path).expanduser().resolve().parent == \
            Path(project).expanduser().resolve() / ".noeta"
    except OSError:
        return False


def mcp_sources(settings_files: Sequence[str | Path],
                project_dirs: Iterable[str | Path] = ()) -> list[tuple[Path, Path | None]]:
    """The files same-named MCP servers are taken from, highest precedence
    first, each with the project it is the ``.noeta/mcp.json`` of (``None``
    for a settings file). ``settings_files`` are lowest precedence first;
    each of ``project_dirs``' ``.noeta/mcp.json`` goes just below the
    lowest of that project's own settings files among them (at the bottom
    when there is none)."""
    low_first: list[tuple[Path, Path | None]] = [(Path(f), None) for f in settings_files]
    for d in project_dirs:
        project = Path(d)
        at = next((i for i, (f, owner) in enumerate(low_first)
                   if owner is None and own_settings_file(f, project)), 0)
        low_first.insert(at, (project / PROJECT_FILE, project))
    return low_first[::-1]
