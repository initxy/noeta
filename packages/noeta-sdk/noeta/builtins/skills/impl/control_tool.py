"""``skill`` — the model-driven skill-invocation control tool, as part of the
``skills`` built-in (control-tool-surface S2b).

The ``skill`` control tool's whole story — its provider-visible schema, its
menu-name validator, its response→neutral-Decision translate body, and its
``skill.md`` description — moved out of the kernel's control band
(``noeta.policies.control_semantics``) into the ``skills`` built-in, joining the
rest of the skill subsystem (indexer / registry / script tool / session pack).
The move is byte-preserving (the S0 golden pins the schema bytes).

What this module imports back from the kernel is the neutral MECHANISM the tool
builds on: the control-tool mount types (``noeta.execution.control_tool``), the
decision-time ``ControlTranslateContext``, and the shared neutral helpers
``enum_roster_prop`` / ``ack_patch_decision`` / ``validate_required_string`` (used
by control tools in several plugins, so they stay kernel-side). The description
loads from the sibling ``skill.md`` via the shared L0 resource loader, exactly as
the fs tools load theirs.

Reached only through the plugin loader's ``ref`` resolution (the ``skills``
manifest's ``control_tool`` contribution); nothing imports it statically.
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


__all__ = [
    "SKILL_TOOL",
    "skill_tool_schema",
    "make_skill_translate",
    "make_skills_control_tool",
]


#: Model-visible **control** tool name for model-driven skill menu selection.
SKILL_TOOL = "skill"


_SKILL_DESCRIPTION = load_markdown(__package__, "skill")


def skill_tool_schema(
    menu: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """Provider-visible schema for :data:`SKILL_TOOL`.

    A **control** tool (never an Engine/ToolRuntime tool): a ``skill`` call
    activates a named skill via a ``StatePatchDecision`` (``activate_skills``
    patch), same channel pre-loop activations use. Added to the Composer's
    ``control_action_schemas`` only when ``skill_invocation_enabled`` AND
    the workspace has at least one indexed skill.

    ``menu`` is a sorted sequence of ``(name, description)`` pairs. The name
    is rendered into the ``skill`` property's ``enum``; the description is
    appended to its description as a human-readable roster, mirroring the
    ``spawn_subagent`` agent_directory pattern. A single required
    ``skill`` string parameter — no ``args``, no ``reason`` (D4).
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
    """D1/D4: translate a `skill` control-tool call into a neutral
    :class:`StatePatchDecision`, or ``None`` when no `skill` is present.

    Rules mirror the sibling control tools: the `skill` call must be
    the **sole** tool call in the turn (mixed with any other tool →
    recoverable error ack, no state write). The ``skill`` argument is
    validated against the sorted menu set: a known name becomes a
    ``StatePatchDecision(activate_skills=[name])`` whose ack confirms the
    skill is loaded and will appear from the next turn; an unknown name
    becomes an error ack listing the available names so the model can retry.

    Duplicate activation (the name is already in ``active_skills``) is NOT
    special-cased here: the same success ack is returned and the state
    merge de-duplicates (``TaskStatePatch.apply`` unions ``activate_skills``
    with ``state.active_skills`` order-preserving). The kernel emits
    ``messages_before`` → ``TaskStatePatched`` → ``messages_after``, same
    sequence as ``todo_write``.
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
    """Build the ``skill`` translate closure over its indexed menu names (D2).

    The closure captures ``menu_names`` so :func:`_maybe_skill_decision`
    validates an ordered skill against the same set the schema's enum was built
    from — WITHOUT the neutral :class:`ControlTranslateContext` carrying a
    feature-named ``skill_menu_names`` field.
    """

    def translate(ctx: ControlTranslateContext) -> Optional[Decision]:
        return _maybe_skill_decision(
            ctx.response,
            ctx.assistant_message,
            menu_names=menu_names,
            assistant_thinking=ctx.assistant_thinking,
        )

    return translate


def _skill_menu(
    registry: Any,
) -> tuple[tuple[tuple[str, str], ...], frozenset[str]]:
    """The ``skill`` tool's ``(menu, menu_names)``, derived from the plugin's
    own registry.

    ``menu`` is the sorted ``(name, description)`` tuple the schema renders;
    ``menu_names`` is the frozenset the translate closure validates against —
    and the mount's gate. An empty index reads the same as no registry: the
    tool is not grown. Bytes are identical to the kernel builder's retired
    ``_skill_menu``.
    """
    if registry is not None:
        skill_names = registry.names()
        if skill_names:
            menu = tuple(
                (name, desc.description)
                for name in sorted(skill_names)
                if (desc := registry.get(name)) is not None
            )
            return menu, frozenset(skill_names)
    return (), frozenset()


def make_skills_control_tool(
    registry: Any,
) -> Callable[[ControlToolBuildContext], Optional[ControlToolMount]]:
    """Build the ``skill`` control-tool mount factory over ``registry``.

    Called by this plugin's OWN session pack (spec §4.1: translate closures
    are session-factory outputs; spec §5: no kit, menu, or registry crosses
    into kernel code) — the returned factory closes over the pack's merged
    registry and rides ``PackContribution.control_tools`` into the builder's
    generic mount loop. It self-gates on the effective ``skill_invocation``
    capability flag AND a non-empty indexed menu — mounting IS enablement.
    Routing band 400, schema band 400 — the byte order the S0 golden pins.
    The rendered menu tuple may be empty (descriptions absent) while the
    tool is still grown, matching the old schema branch exactly.
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
