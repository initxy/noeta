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


#: The headers the two ``skill_listing`` residents render under.
LISTING_ROSTER_HEADER = "The following skills are available via the `skill` tool:"
LISTING_ADDED_HEADER = "New skills are available via the `skill` tool:"


def _message_text(message: object) -> str:
    return "".join(
        getattr(block, "text", "") or ""
        for block in getattr(message, "content", ()) or ()
    )


def listing_texts(messages: object) -> list[str]:
    """Every rendered skill-listing message in ``messages``, in order."""
    out: list[str] = []
    for message in messages or ():  # type: ignore[attr-defined]
        text = _message_text(message)
        if text.startswith((LISTING_ROSTER_HEADER, LISTING_ADDED_HEADER)):
            out.append(text)
    return out


def parse_listing_lines(lines: object) -> dict[str, str]:
    """``name → summary`` from listing lines ``- name: summary`` / ``- name``
    (``""`` = listed by name only)."""
    out: dict[str, str] = {}
    for line in lines:  # type: ignore[attr-defined]
        assert line.startswith("- "), line
        name, _, summary = line[2:].partition(": ")
        out[name] = summary
    return out


def listing_entries(messages: object) -> dict[str, str]:
    """``name → summary`` across every rendered skill-listing message."""
    out: dict[str, str] = {}
    for text in listing_texts(messages):
        out.update(parse_listing_lines(text.split("\n")[1:]))
    return out


class CapturingRecorder:
    """A ``SessionRecorder`` stand-in that keeps every ``record_content`` call
    as ``(kind, name, ref, refresh)``."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, object, bool]] = []

    def record_content(
        self,
        *,
        kind: str,
        name: str,
        version: str,
        ref: object,
        policy: str,
        refresh: bool = True,
    ) -> None:
        self.records.append((kind, name, ref, refresh))


def init_listing(hooks: object, content_store: object) -> dict[str, list[str]]:
    """Run a build's ``init`` hooks (``SessionInputs.init_hooks`` /
    ``Engine.content_init_hooks``: ``(name, hook)`` pairs) and return the
    ``skill_listing`` residents they record as ``resident name → lines``."""
    from noeta.builtins.skills.impl.listing import decode_listing

    rec = CapturingRecorder()
    for _, hook in hooks:  # type: ignore[attr-defined]
        hook(rec)
    out: dict[str, list[str]] = {}
    for kind, name, ref, _ in rec.records:
        if kind == "skill_listing":
            _, lines = decode_listing(content_store.get(ref))  # type: ignore[attr-defined]
            out[name] = list(lines)
    return out
