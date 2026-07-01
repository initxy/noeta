"""Shared skill test fixtures: two shapes for writing SKILL.md."""
from pathlib import Path


def write_skill(ws: Path, name: str, description: str = "") -> None:
    """Frontmatter variant: write standard frontmatter + placeholder body to
    ``ws/.noeta/skills/<name>/SKILL.md``.

    ``description`` is a required frontmatter key for the indexer; the value
    may be empty (an empty value renders the menu entry as a bare name).
    """
    skill_dir = ws / ".noeta" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        f"Body of the {name} skill.\n"
    )
    (skill_dir / "SKILL.md").write_text(frontmatter, encoding="utf-8")


def write_skill_raw(skills_dir: Path, name: str, body: str) -> None:
    """Raw variant: write ``body`` verbatim to ``skills_dir/<name>/SKILL.md``."""
    skill_pkg = skills_dir / name
    skill_pkg.mkdir(parents=True)
    (skill_pkg / "SKILL.md").write_text(body, encoding="utf-8")
