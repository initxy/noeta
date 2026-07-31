"""``skills`` built-in — implementation package (microkernel phase 2a).

The skill subsystem's material: the SKILL.md indexer and registry
(``indexer``), the frontmatter parser (``_frontmatter``), the
``run_skill_script`` tool (``script``), the ``allowed-tools`` grant resolver
(``allowed_tools``), and the session wiring + the ``session_pack``
contribution body (``wiring`` — microkernel phase 3). The kernel keeps only
the typed seams in ``noeta.execution.skills`` (``SkillsKit`` /
``activate_skills`` / the hash helpers); nothing imports this package
statically — the SDK resolves :func:`build_skills_session_pack` from the
manifest at client build.
"""

from __future__ import annotations

from .allowed_tools import resolve_skill_allowed_tools
from .control_tool import (
    SKILL_TOOL,
    make_skill_translate,
    make_skills_control_tool,
    skill_tool_schema,
)
from .indexer import (
    SkillDescription,
    SkillIndexer,
    SkillRegistry,
    build_skill_renderer,
)
from .script import (
    SKILL_SCRIPT_TOOL_NAME,
    RunSkillScriptTool,
    is_skill_script_resource,
)
from .wiring import (
    DEFAULT_SKILLS_SUBDIR,
    build_skill_composer,
    build_skill_script_wiring,
    build_skills_kit,
    build_skills_session_pack,
    extract_skill_allowed_tools_raw,
    load_workspace_skills,
    merge_skill_registries,
    resolve_skill_scripts,
    skill_content_kind,
)


__all__ = [
    "DEFAULT_SKILLS_SUBDIR",
    "SKILL_SCRIPT_TOOL_NAME",
    "SKILL_TOOL",
    "RunSkillScriptTool",
    "SkillDescription",
    "SkillIndexer",
    "SkillRegistry",
    "build_skill_composer",
    "build_skill_renderer",
    "build_skill_script_wiring",
    "build_skills_kit",
    "build_skills_session_pack",
    "extract_skill_allowed_tools_raw",
    "is_skill_script_resource",
    "load_workspace_skills",
    "make_skill_translate",
    "make_skills_control_tool",
    "merge_skill_registries",
    "resolve_skill_allowed_tools",
    "resolve_skill_scripts",
    "skill_content_kind",
    "skill_tool_schema",
]
