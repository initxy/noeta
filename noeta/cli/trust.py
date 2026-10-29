"""Project trust (``noeta trust``): which projects' gated directories may
load, kept in ``~/.noeta/trusted.json``."""

from __future__ import annotations

from pathlib import Path

from noeta.cli import home_dir

#: The projects whose gated directories (``PROJECT_GATED_DIRS``) may load:
#: ``{"projects": [path, ...]}``,
#: written by ``noeta trust``.
TRUST_FILE = "trusted.json"


def trusted_projects() -> list[str]:
    import json

    path = home_dir.HOME / TRUST_FILE
    if not path.is_file():
        return []
    data = json.loads(path.read_text("utf-8"))
    projects = data.get("projects", []) if isinstance(data, dict) else []
    return [str(Path(p).expanduser().resolve()) for p in projects if isinstance(p, str)]


def trust_project(directory: str) -> str:
    """Add ``directory`` to the trusted projects; returns it resolved."""
    import json

    resolved = str(Path(directory).expanduser().resolve())
    projects = trusted_projects()
    if resolved not in projects:
        projects.append(resolved)
        home_dir.ensure_home()
        (home_dir.HOME / TRUST_FILE).write_text(
            json.dumps({"projects": projects}, indent=2) + "\n", "utf-8")
    return resolved


def project_trusted(directory: str) -> bool:
    """Has the person trusted the project at ``directory`` (``noeta trust``)?"""
    return str(Path(directory).expanduser().resolve()) in trusted_projects()


#: A project's directories that load only once it is trusted, with what each
#: holds: code, or what changes an agent's plugins, permissions and prompts.
PROJECT_GATED_DIRS = {
    ".noeta/plugins": "plugins",
    ".noeta/agents": "agent files",
    ".noeta/prompts": "prompt overrides",
    ".noeta/skills": "skills",
    ".agents/skills": "skills",
}


def untrusted_notice(cwd: str | None, sub: str) -> str | None:
    """The notice for ``<cwd>/<sub>`` left alone because the project is not
    trusted; ``None`` when it is absent or the project is trusted."""
    if not cwd:
        return None
    project = Path(cwd).expanduser().resolve()
    local = project / sub
    if not local.is_dir() or str(project) in trusted_projects():
        return None
    return (f"{local} was not loaded: this project is not trusted "
            f"(run `noeta trust` in {project} to load its {PROJECT_GATED_DIRS[sub]})")
