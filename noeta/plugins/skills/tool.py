"""The ``Skill`` tool, and what activating a skill records (its text,
directory, scripts and ``allowed-tools`` grant)."""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING, Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.skills.allowed_tools import allow_rule_texts, resolve_skill_allowed_tools
from noeta.plugins.skills.registry import (
    SkillDescription,
    render_activation,
    substitute_variables,
)
from noeta.plugins.skills.script import is_skill_script_resource
from noeta.toolkit.context import task_env
from noeta.toolkit.tools import SCHEMA_DIALECT

if TYPE_CHECKING:
    from collections.abc import Sequence

    from noeta.plugins.skills.extension import Skills

SKILL_TOOL = "Skill"

_SKILL_PROP_DESCRIPTION = ("The skill's name exactly as the skill listing gives it, without "
                           "a leading slash; never a guessed name.")
_ARGS_PROP_DESCRIPTION = "Optional text passed to the skill."
#: A ``disable-model-invocation`` skill called through the tool.
USER_ONLY_SKILL = (
    "The skill {name} is marked disable-model-invocation: only the user can start it, so "
    "the Skill tool will not run it. Ask the user to type /{name} themselves, and do not "
    "carry out its steps by other means.")


def skill_name(raw: Any) -> str | None:
    """The name a ``Skill`` call asks for: trimmed, without a leading ``/``
    (the model often writes the slash command); ``None`` when empty."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return (name[1:] if name.startswith("/") else name) or None


def _skill_description() -> str:
    path = resources.files("noeta.plugins.skills").joinpath("descriptions", "skill.md")
    return path.read_text(encoding="utf-8").rstrip("\n")


class SkillTool:
    """``Skill(skill, args?)``: the same schema bytes for every task. The listing
    in the conversation names the skills; the tool validates against the
    live roster (a skill installed since the listing is accepted, a removed
    one is not)."""

    name = SKILL_TOOL
    read_only = False
    clearable = False

    def __init__(self, owner: Skills) -> None:
        self._owner = owner
        self.description = _skill_description()
        self.input_schema: dict[str, Any] = {
            "$schema": SCHEMA_DIALECT,
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": _SKILL_PROP_DESCRIPTION},
                "args": {"type": "string", "description": _ARGS_PROP_DESCRIPTION},
            },
            "required": ["skill"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        raw = input.get("skill")
        name = skill_name(raw)
        if name is None:
            return text_output(f"Not a skill name: {raw!r}", is_error=True)
        registry = await self._owner.registry(task_env(ctx.env, {}))
        menu = registry.model_invocable(unlocked_skills(ctx.state.get(self._owner.name)))
        desc = registry.get(name)
        if desc is not None and not desc.model_invocable:
            return text_output(USER_ONLY_SKILL.format(name=name), is_error=True)
        if desc is None or name not in menu:
            near = closest_name(name, menu)
            hint = f" Did you mean {near}?" if near is not None else ""
            return text_output(f"There is no skill named {name}.{hint}", is_error=True)
        args = input.get("args")
        if args is not None and not isinstance(args, str):
            return text_output("args must be a string", is_error=True)
        return text_output(
            f"Skill '{name}' loaded; its instructions will appear in your "
            f"context from your next step.",
            skill_activation=activation_data(desc, "model", args, ctx.env.cwd),
        )


def closest_name(name: str, names: Sequence[str], max_distance: int = 2) -> str | None:
    """The name in ``names`` nearest to ``name`` by edit distance, if within
    ``max_distance`` (first in order on a tie)."""
    best: tuple[int, str] | None = None
    for candidate in names:
        d = _edit_distance(name, candidate)
        if d <= max_distance and (best is None or d < best[0]):
            best = (d, candidate)
    return best[1] if best else None


def _edit_distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def activation_data(desc: SkillDescription, source: str,
                    args: str | None = None, cwd: str | None = None) -> dict[str, Any]:
    """What activating ``desc`` records (``source``: ``model`` / ``user`` /
    ``preload``)."""
    raw = desc.meta("allowed-tools")
    allowed: list[str] | None = None
    if raw is not None:
        [(_, grant)] = resolve_skill_allowed_tools(((desc.name, raw),))
        allowed = sorted(grant)
    rules = [substitute_variables(r, desc.base_dir, cwd)
             for r in allow_rule_texts(raw)] if raw is not None else []
    return {
        "name": desc.name,
        "source": source,
        "dir": desc.base_dir,
        "scripts": [r for r in desc.resources if is_skill_script_resource(r)],
        "allowed_tools": allowed,
        "allow_rules": rules,
        "args": args,
        "text": render_activation(desc, args, cwd),
    }


def unlocked_skills(state: Any) -> frozenset[str]:
    """The skills with ``paths`` a touched file has made available."""
    return frozenset((state or {}).get("unlocked") or ())
