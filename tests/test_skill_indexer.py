"""SkillIndexer turns a directory tree of SKILL.md files into a registry.

One malformed file must never cost a user their whole skill set, so invalid
entries are skipped with a warning instead of aborting the scan. Duplicate
names resolve by POSIX-normalised sort order rather than raw directory
iteration, so the same tree indexes identically on every machine — the
alternative is a registry that silently differs between checkouts.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from noeta.builtins.skills.impl import SkillIndexer
from noeta.builtins.skills.impl.indexer import SkillRegistry


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _skill_doc(
    name: str,
    description: str,
    body: str = "",
    *,
    version: str | None = None,
    priority: int | None = None,
) -> str:
    fm_lines = [f"name: {name}", f"description: {description}"]
    if version is not None:
        fm_lines.append(f"version: {version}")
    if priority is not None:
        fm_lines.append(f"priority: {priority}")
    return "---\n" + "\n".join(fm_lines) + "\n---\n" + body


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_index_returns_registry_with_parsed_skills(tmp_path: Path) -> None:
    _write(tmp_path / "alpha" / "SKILL.md", _skill_doc("alpha", "Alpha skill", "alpha body\n"))
    _write(tmp_path / "beta" / "SKILL.md", _skill_doc("beta", "Beta skill", "beta body\n"))

    registry = SkillIndexer(tmp_path).index()

    assert set(registry.names()) == {"alpha", "beta"}
    alpha = registry.get("alpha")
    assert alpha is not None
    assert alpha.name == "alpha"
    assert alpha.description == "Alpha skill"
    assert alpha.body == "alpha body\n"
    assert alpha.version == "1"
    assert alpha.priority == 100
    assert alpha.source_path == tmp_path / "alpha" / "SKILL.md"


def test_index_nested_directories_found_by_recursive_walk(tmp_path: Path) -> None:
    _write(
        tmp_path / "a" / "deep" / "nested" / "SKILL.md",
        _skill_doc("deepskill", "Deep nested skill"),
    )

    registry = SkillIndexer(tmp_path).index()

    assert registry.get("deepskill") is not None


def test_index_follows_symlinked_skill_directory(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    target = tmp_path / "real-skill"
    _write(
        target / "SKILL.md",
        _skill_doc("architecture-diagram", "Symlinked skill"),
    )
    (root / "architecture-diagram").symlink_to(target, target_is_directory=True)

    registry = SkillIndexer(root).index()

    desc = registry.get("architecture-diagram")
    assert desc is not None
    assert desc.source_path == root / "architecture-diagram" / "SKILL.md"


def test_index_symlink_cycle_does_not_loop(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write(root / "alpha" / "SKILL.md", _skill_doc("alpha", "Alpha skill"))
    (root / "alpha" / "loop").symlink_to(root, target_is_directory=True)

    registry = SkillIndexer(root).index()

    assert registry.names() == ("alpha",)


def test_index_returns_empty_registry_when_root_missing(tmp_path: Path) -> None:
    registry = SkillIndexer(tmp_path / "does-not-exist").index()
    assert isinstance(registry, SkillRegistry)
    assert registry.names() == ()


def test_index_uses_optional_field_values(tmp_path: Path) -> None:
    _write(
        tmp_path / "k" / "SKILL.md",
        _skill_doc("k", "K skill", version="3", priority=10),
    )
    registry = SkillIndexer(tmp_path).index()
    k = registry.get("k")
    assert k is not None
    assert k.version == "3"
    assert k.priority == 10


# ---------------------------------------------------------------------------
# Invalid handling — skip + WARNING; index continues
# ---------------------------------------------------------------------------


def test_invalid_frontmatter_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(tmp_path / "good" / "SKILL.md", _skill_doc("good", "valid"))
    _write(tmp_path / "bad" / "SKILL.md", "no-frontmatter at all\n")

    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()

    assert set(registry.names()) == {"good"}
    assert any(
        "bad/SKILL.md" in record.getMessage() or "bad\\SKILL.md" in record.getMessage()
        for record in caplog.records
    )


def test_missing_required_name_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(
        tmp_path / "anon" / "SKILL.md",
        "---\ndescription: orphan\n---\nbody\n",
    )
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    assert registry.names() == ()
    assert any("missing required key 'name'" in r.getMessage() for r in caplog.records)


def test_missing_required_description_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(
        tmp_path / "x" / "SKILL.md",
        "---\nname: x\n---\n",
    )
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    assert registry.names() == ()
    assert any(
        "missing required key 'description'" in r.getMessage() for r in caplog.records
    )


def test_invalid_name_format_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(tmp_path / "bad" / "SKILL.md", _skill_doc("Bad_Name!", "no"))
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    assert registry.names() == ()
    assert any("invalid 'name' value" in r.getMessage() for r in caplog.records)


def test_invalid_priority_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(
        tmp_path / "p" / "SKILL.md",
        "---\nname: p\ndescription: pdesc\npriority: not-a-number\n---\n",
    )
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    assert registry.names() == ()
    assert any("invalid 'priority'" in r.getMessage() for r in caplog.records)


def test_typo_of_known_key_skipped_for_missing_required(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``descrption:`` typo is tolerated as metadata, so the file is skipped
    for the *missing* required ``description`` rather than for the unknown
    key — the price of accepting third-party skills that carry arbitrary
    frontmatter."""
    _write(
        tmp_path / "tp" / "SKILL.md",
        "---\nname: tp\ndescrption: typo\n---\nbody\n",
    )
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    assert "tp" not in registry.names()
    assert any(
        "missing required key 'description'" in r.getMessage()
        for r in caplog.records
    )


def test_unknown_frontmatter_key_loads_as_metadata(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unknown key does not disqualify the skill: it is captured as opaque
    metadata, sorted by raw key name so the tuple is order-stable."""
    _write(
        tmp_path / "ef" / "SKILL.md",
        "---\nname: ef\ndescription: d\nextra_field: bar\n"
        "allowed-tools: [Read, Bash]\n---\n",
    )
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    assert "ef" in registry.names()
    desc = registry.get("ef")
    assert desc is not None
    # Inline lists stay verbatim strings — nothing here is YAML-parsed.
    assert desc.metadata == (
        ("allowed-tools", "[Read, Bash]"),
        ("extra_field", "bar"),
    )
    assert dict(desc.metadata)["allowed-tools"] == "[Read, Bash]"


def test_nested_unknown_metadata_block_loads_as_metadata(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Standard YAML-style nested metadata should not make an otherwise
    valid skill disappear from the registry."""
    _write(
        tmp_path / "lark-doc" / "SKILL.md",
        "---\n"
        "name: lark-doc\n"
        "version: 2.0.0\n"
        "description: \"Read and edit Lark docs\"\n"
        "metadata:\n"
        "  requires:\n"
        "    bins: [\"lark-cli\"]\n"
        "  cliHelp: \"lark-cli docs --help\"\n"
        "---\n"
        "# docs\n",
    )
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    desc = registry.get("lark-doc")
    assert desc is not None
    assert desc.version == "2.0.0"
    assert desc.description == "\"Read and edit Lark docs\""
    assert desc.metadata == (
        (
            "metadata",
            "requires:\n"
            "  bins: [\"lark-cli\"]\n"
            "cliHelp: \"lark-cli docs --help\"",
        ),
    )
    assert not any("invalid frontmatter" in r.getMessage() for r in caplog.records)


def test_folded_description_loads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(
        tmp_path / "lark-whiteboard" / "SKILL.md",
        "---\n"
        "name: lark-whiteboard\n"
        "description: >\n"
        "  Draw diagrams.\n"
        "  Edit whiteboards.\n"
        "metadata:\n"
        "  requires:\n"
        "    bins: [\"lark-cli\"]\n"
        "---\n"
        "body\n",
    )
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    desc = registry.get("lark-whiteboard")
    assert desc is not None
    assert desc.description == "Draw diagrams. Edit whiteboards."
    assert dict(desc.metadata)["metadata"] == (
        "requires:\n"
        "  bins: [\"lark-cli\"]"
    )
    assert not any("invalid frontmatter" in r.getMessage() for r in caplog.records)


def test_duplicate_frontmatter_key_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(
        tmp_path / "d" / "SKILL.md",
        "---\nname: d\ndescription: first\ndescription: second\n---\n",
    )
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()
    d = registry.get("d")
    assert d is not None
    assert d.description == "second"
    assert any("duplicate" in r.getMessage() for r in caplog.records)


def test_invalid_skill_does_not_break_index_of_valid_siblings(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "ok" / "SKILL.md", _skill_doc("ok", "ok"))
    _write(tmp_path / "broken" / "SKILL.md", "garbage")
    _write(tmp_path / "ok2" / "SKILL.md", _skill_doc("ok2", "ok2"))

    registry = SkillIndexer(tmp_path).index()
    assert set(registry.names()) == {"ok", "ok2"}


# ---------------------------------------------------------------------------
# Determinism — sorted POSIX path + first-wins
# ---------------------------------------------------------------------------


def test_duplicate_name_first_wins_by_sorted_posix_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Scan order is the normalised POSIX relative path ascending, so which
    of two same-named skills wins does not depend on disk creation order or
    on ``Path.rglob``'s natural ordering.

    The sort is asserted on ``as_posix()`` relative paths rather than raw
    ``Path`` ordering, which is what makes the answer identical on POSIX and
    Windows file systems.
    """
    # Created z-then-a so creation order contradicts the expected winner.
    z_path = tmp_path / "z-dir" / "SKILL.md"
    a_path = tmp_path / "a-dir" / "SKILL.md"
    _write(z_path, _skill_doc("dup", "z body", "Z\n"))
    _write(a_path, _skill_doc("dup", "a body", "A\n"))

    posix_sorted = sorted(
        [a_path.relative_to(tmp_path).as_posix(), z_path.relative_to(tmp_path).as_posix()]
    )
    assert posix_sorted == ["a-dir/SKILL.md", "z-dir/SKILL.md"]

    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.indexer")
    registry = SkillIndexer(tmp_path).index()

    dup = registry.get("dup")
    assert dup is not None
    assert dup.description == "a body"
    assert dup.body == "A\n"
    # The loser is logged so a shadowed skill is diagnosable.
    assert any(
        "duplicate name 'dup'" in r.getMessage() for r in caplog.records
    )


def test_scan_order_is_posix_relative_path_ascending(tmp_path: Path) -> None:
    """The enumeration order is not observable directly, so duplicate-name
    first-wins across three siblings is used to probe it: only the
    POSIX-first candidate can survive.
    """
    _write(tmp_path / "m" / "SKILL.md", _skill_doc("multi", "m"))
    _write(tmp_path / "a" / "SKILL.md", _skill_doc("multi", "a"))
    _write(tmp_path / "z" / "SKILL.md", _skill_doc("multi", "z"))

    registry = SkillIndexer(tmp_path).index()
    multi = registry.get("multi")
    assert multi is not None
    assert multi.description == "a"


def test_repeated_index_calls_produce_equal_registry(tmp_path: Path) -> None:
    """Indexing is a pure function of disk state: the same tree yields equal
    SkillDescription instances, ``source_path`` included."""
    _write(tmp_path / "a" / "SKILL.md", _skill_doc("a", "a"))
    _write(tmp_path / "b" / "SKILL.md", _skill_doc("b", "b"))
    r1 = SkillIndexer(tmp_path).index()
    r2 = SkillIndexer(tmp_path).index()
    assert r1.names() == r2.names()
    for name in r1.names():
        assert r1.get(name) == r2.get(name)


# ---------------------------------------------------------------------------
# CRLF body normalisation
# ---------------------------------------------------------------------------


def test_crlf_skill_md_produces_lf_body(tmp_path: Path) -> None:
    """CRLF line endings normalise to LF, so a checkout on Windows and one on
    Linux render byte-equal Messages from the same SKILL.md."""
    path = tmp_path / "win" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"---\r\nname: win\r\ndescription: crlf\r\n---\r\nline1\r\nline2\r\n"
    )
    registry = SkillIndexer(tmp_path).index()
    win = registry.get("win")
    assert win is not None
    assert "\r" not in win.body
    assert win.body == "line1\nline2\n"
