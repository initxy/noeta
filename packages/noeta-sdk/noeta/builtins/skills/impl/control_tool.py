"""``skill`` — the model-driven skill-invocation control tool.

A control tool, not an Engine/ToolRuntime tool: a ``skill`` call activates a
named skill via a ``StatePatchDecision``, the same channel pre-loop activations
use. This module owns the tool's whole story — provider schema, menu-name
validator, response→neutral-Decision translate body, and its ``skill.md``
description. It builds on kernel-side neutral mechanism only (the control-tool
mount types, ``ControlTranslateContext``, and shared helpers like
``enum_roster_prop`` that several plugins reuse), and is reached solely through
the plugin loader's ``ref`` resolution.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    ControlTranslateContext,
    ack_patch_decision,
    enum_roster_prop,
    validate_required_string,
)
from noeta.protocols.decisions import Decision, TaskStatePatch
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolUseBlock,
)
from noeta.protocols.resources import load_markdown

from .indexer import strip_argument_placeholder


__all__ = [
    "MENU_DESCRIPTION_MAX_CHARS",
    "SKILL_TOOL",
    "skill_tool_schema",
    "make_skill_translate",
    "make_skills_control_tool",
]


#: Model-visible **control** tool name for model-driven skill menu selection.
SKILL_TOOL = "skill"


#: Per-skill budget for the roster line. Every menu description concatenates
#: into ONE property description that sits inside the stable-prefix hash, so an
#: author's unbounded ``description:`` is unbounded prompt on every single turn
#: of every session that indexes it. The cap is per skill, not per menu: the
#: entry count is bounded by the workspace, the description length is not.
MENU_DESCRIPTION_MAX_CHARS = 1024

_TRUNCATION_MARKER = "… (truncated)"


_SKILL_DESCRIPTION = load_markdown(__package__, "skill")


def skill_tool_schema(
    menu: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """Provider-visible schema for :data:`SKILL_TOOL`.

    A control tool (never an Engine/ToolRuntime tool): a ``skill`` call
    activates a named skill via a ``StatePatchDecision`` (``activate_skills``
    patch), the same channel pre-loop activations use. Added to the Composer's
    ``control_action_schemas`` only when ``skill_invocation_enabled`` AND the
    workspace has at least one indexed skill.

    ``menu`` is a sorted sequence of ``(name, description)`` pairs. The name is
    rendered into the ``skill`` property's ``enum``; the description is appended
    as a human-readable roster. A single required ``skill`` string parameter.
    """
    skill_prop = enum_roster_prop("Name of the skill to activate.", menu)
    return {
        "type": "function",
        "function": {
            "name": SKILL_TOOL,
            "description": _SKILL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": skill_prop,
                },
                "required": ["skill"],
            },
        },
    }


# Max length for a skill name string (generous upper bound; the real gate is
# the menu enum, but this catches obviously-malformed payloads before we
# format the error roster).
_SKILL_NAME_MAX_LEN = 200


def _maybe_skill_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    menu_names: frozenset[str],
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Translate a ``skill`` control-tool call into a neutral
    :class:`StatePatchDecision`, or ``None`` when no ``skill`` is present.

    The ``skill`` call must be the **sole** tool call in the turn (mixed with
    any other tool → recoverable error ack, no state write). The ``skill``
    argument is validated against the menu set: a known name becomes a
    ``StatePatchDecision(activate_skills=[name])``; an unknown name becomes an
    error ack listing the available names so the model can retry.

    Duplicate activation is not special-cased — the success ack is returned and
    ``TaskStatePatch.apply`` unions ``activate_skills`` with
    ``state.active_skills`` order-preserving, so it de-duplicates.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    skill_blocks = [b for b in tool_uses if b.tool_name == SKILL_TOOL]
    if not skill_blocks:
        return None

    # Sole-call rule — exactly one `skill` block and nothing else.
    if len(skill_blocks) != len(tool_uses) or len(skill_blocks) != 1:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="skill must be the only tool call in the turn",
            valid=False,
        )

    block = skill_blocks[0]
    args = dict(block.arguments)
    ok, name_or_err = validate_required_string(
        args.get("skill"), "skill", _SKILL_NAME_MAX_LEN
    )
    if not ok:
        assert isinstance(name_or_err, str)
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=name_or_err,
            valid=False,
        )
    name = name_or_err
    assert isinstance(name, str)
    if name not in menu_names:
        available = ", ".join(sorted(menu_names)) if menu_names else "(none)"
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=f"unknown skill {name!r}; available: {available}",
            valid=False,
        )
    return ack_patch_decision(
        tool_uses,
        assistant_message,
        assistant_thinking,
        patch=TaskStatePatch(activate_skills=[name]),
        text=f"Skill '{name}' loaded; its instructions will appear in your "
        f"context from the next turn.",
        valid=True,
    )


def make_skill_translate(
    menu_names: frozenset[str],
) -> Callable[[ControlTranslateContext], Optional[Decision]]:
    """Build the ``skill`` translate closure over its indexed menu names.

    The closure captures ``menu_names`` so :func:`_maybe_skill_decision`
    validates an ordered skill against the same set the schema's enum was built
    from, without the neutral :class:`ControlTranslateContext` carrying a
    feature-named field.
    """

    def translate(ctx: ControlTranslateContext) -> Optional[Decision]:
        return _maybe_skill_decision(
            ctx.response,
            ctx.assistant_message,
            menu_names=menu_names,
            assistant_thinking=ctx.assistant_thinking,
        )

    return translate


def _menu_description(text: str) -> str:
    """Fit one skill's description into the roster budget.

    Truncation is a suffix marker rather than a hard cut, so the model can tell
    a clipped summary from a terse one and knows the body holds the rest. The
    result is exactly :data:`MENU_DESCRIPTION_MAX_CHARS` characters, marker
    included. ``$ARGUMENTS`` is excised first — the menu is a model-visible
    surface and Noeta has no argument channel to substitute from (same rule as
    the activation render; see ``indexer.strip_argument_placeholder``).
    """
    text = strip_argument_placeholder(text)
    if len(text) <= MENU_DESCRIPTION_MAX_CHARS:
        return text
    keep = MENU_DESCRIPTION_MAX_CHARS - len(_TRUNCATION_MARKER)
    return text[:keep] + _TRUNCATION_MARKER


def _skill_menu(
    registry: Any,
) -> tuple[tuple[tuple[str, str], ...], frozenset[str]]:
    """The ``skill`` tool's ``(menu, menu_names)``, derived from the plugin's
    own registry.

    ``menu`` is the sorted ``(name, description)`` tuple the schema renders;
    ``menu_names`` is the frozenset the translate closure validates against —
    and the mount's gate. An empty index reads the same as no registry: the
    tool is not grown.

    A skill whose frontmatter declares ``disable-model-invocation: true`` is
    excluded from **both**, so the model can neither see it nor name it. It
    stays in the Registry, which is what keeps the host preload channel
    (``Options.skills``, a seed activation) able to load it — the same split
    Claude Code draws between what the model may invoke and what the user may.
    """
    if registry is not None:
        entries: list[tuple[str, str]] = []
        for name in sorted(registry.names()):
            desc = registry.get(name)
            if desc is None or not getattr(desc, "model_invocable", True):
                continue
            entries.append((name, _menu_description(desc.description)))
        if entries:
            return tuple(entries), frozenset(name for name, _ in entries)
    return (), frozenset()


def make_skills_control_tool(
    registry: Any,
) -> Callable[[ControlToolBuildContext], Optional[ControlToolMount]]:
    """Build the ``skill`` control-tool mount factory over ``registry``.

    The returned factory closes over the pack's merged registry and rides
    ``PackContribution.control_tools`` into the builder's generic mount loop. It
    self-gates on the effective ``skill_invocation`` capability flag AND a
    non-empty indexed menu — mounting IS enablement. The rendered menu tuple may
    be empty (descriptions absent) while the tool is still grown.
    """

    def factory(ctx: ControlToolBuildContext) -> Optional[ControlToolMount]:
        if not ctx.flag("skill_invocation"):
            return None
        menu, menu_names = _skill_menu(registry)
        if not menu_names:
            return None
        return ControlToolMount(
            name=SKILL_TOOL,
            schema=skill_tool_schema(menu),
            translate=make_skill_translate(menu_names),
            routing_priority=400,
            schema_priority=400,
        )

    return factory
