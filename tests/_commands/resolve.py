"""Resolve a slash command into an executable payload.

For ``"prompt"`` commands this yields the agent to fork to, the built-in skill
to activate, and the arguments to pass through to the skill (as ``$ARGUMENTS``).
For ``"local"`` commands (``/help``, ``/agents``, ``/skills``) this yields
rendered text.

Architecture note: the generic resolution logic lives in
:mod:`noeta.execution.commands`.  This module only supplies the product-specific
local renderers and delegates to the generic helper with the product's
``BUILTIN_COMMANDS`` bound.
"""

from __future__ import annotations

from noeta.execution.commands import (
    CommandResolution,
    first_sentence,
    render_help,
    resolve_command as _resolve_generic,
)

from .registry import BUILTIN_COMMANDS

__all__ = [
    "CommandResolution",
    "resolve_command",
]


def _render_help() -> str:
    return render_help(BUILTIN_COMMANDS)


def _render_agents() -> str:
    # Import lazily to avoid a package-import cycle at module load.
    # No longer import roster; use the noeta.presets official four-pack.
    from noeta.presets import official_specs

    specs = official_specs()
    lines: list[str] = []
    # Render only the four canonical names (no longer show the default alias row).
    for name in sorted(specs):
        spec = specs[name]
        summary = first_sentence(spec.instructions)
        lines.append(f"{name} — {summary}")
    return "\n".join(lines)


def _render_skills() -> str:
    from tests._builtin_skills import load_builtin_skills

    registry = load_builtin_skills()
    lines: list[str] = []
    for name in sorted(registry.names()):
        skill = registry.get(name)
        if skill is None:
            continue
        lines.append(f"{name} — {skill.description}")
    return "\n".join(lines)


_LOCAL_RENDERERS = {
    "help": _render_help,
    "agents": _render_agents,
    "skill": _render_skills,
    "skills": _render_skills,
}


def resolve_command(name: str, arguments: str = "") -> CommandResolution:
    """Resolve a slash command by name against the built-in catalog.

    Args:
        name: Command name without the leading slash.
        arguments: User-supplied arguments, passed through to prompt commands as
            ``$ARGUMENTS``.  Ignored by local commands.

    Returns:
        A :class:`CommandResolution`.

    Raises:
        KeyError: If ``name`` is not a known command.
    """

    return _resolve_generic(
        name,
        arguments,
        commands=BUILTIN_COMMANDS,
        local_renderers=_LOCAL_RENDERERS,
    )
