"""Skill protocol: parse ``<root>/<name>/SKILL.md`` into a Registry
that :class:`ThreeSegmentComposer` consumes via its
``skill_renderer`` seam (issue 21)."""

from __future__ import annotations

from .indexer import (
    SkillDescription,
    SkillIndexer,
    SkillRegistry,
    build_skill_renderer,
)


__all__ = [
    "SkillDescription",
    "SkillIndexer",
    "SkillRegistry",
    "build_skill_renderer",
]
