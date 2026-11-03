"""The ``Skill`` tool, and what activating a skill records (its text,
directory, scripts and ``allowed-tools`` grant)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.skills.registry import (
    SkillDescription,
    SkillRegistry,
    former_name,
    render_command_body,
    substitute_variables,
)
from noeta.plugins.skills.script import is_skill_script_resource
from noeta.toolkit.allowed_tools import allow_rule_texts, granted_tools
from noeta.toolkit.context import task_env
from noeta.toolkit.tools import SCHEMA_DIALECT, load_description

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from noeta.core.extension import TaskView
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


class SkillTool:
    """``Skill(skill, args?)``: the same schema bytes for every task. The listing
    in the conversation names the skills; the tool validates against the
    live roster (a skill installed since the listing is accepted, a removed
    one is not)."""

    name = SKILL_TOOL
    read_only = False
    safe_while_planning = True
    clearable = False

    def __init__(self, owner: Skills) -> None:
        self._owner = owner
        self.description = load_description("noeta.plugins.skills", "skill")
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
        state = ctx.state.get(self._owner.name)
        registry = await self._owner.registry(task_env(ctx.env, {}), state)
        menu = registry.model_invocable(unlocked_skills(state))
        desc = registry.get(name)
        if desc is not None and not desc.model_invocable:
            return text_output(USER_ONLY_SKILL.format(name=name), is_error=True)
        if desc is None or name not in menu:
            return text_output(not_found(name, registry, menu), is_error=True)
        args = input.get("args")
        if args is not None and not isinstance(args, str):
            return text_output("args must be a string", is_error=True)
        return text_output(
            f"Starting skill: {name}. Its instructions follow.",
            skill_activation=activation_data(desc, "model", args, ctx.env.cwd,
                                             ctx.root_id or ctx.task_id),
        )


    async def rule_inputs(self, input: Mapping[str, Any],
                          view: TaskView) -> Sequence[tuple[dict[str, Any], bool]]:
        """The call as it reads under the skill's former name (its
        frontmatter ``name``, :func:`former_name`): permission rules written
        before skills were named by their directories still apply. A deny or
        ask rule naming it applies; an allow rule only while no other skill
        answers to that name (``True``)."""
        name = skill_name(input.get("skill"))
        if name is None:
            return ()
        registry = await self._owner.turn_registry(view)
        desc = registry.get(name)
        former = former_name(desc) if desc is not None else None
        if desc is None or former is None:
            return ()
        return (({**input, "skill": former}, registry.formerly(former) is desc),)


def not_found(name: str, registry: SkillRegistry, menu: Sequence[str]) -> str:
    """Why ``name`` is not a skill the model may load, and what it may have
    meant: the plugin skill ending in ``:<name>``, the skills of that name
    scoped to directories (Claude Code names neither by the bare name), or
    a name within a small edit distance."""
    head = f"There is no skill named {name}."
    scoped = [d.name for d in registry.scoped(name) if d.name in menu]
    full = [] if ":" in name else [n for n in menu if n.rsplit(":", 1)[-1] == name
                                   and n != name and n not in scoped]
    if full and len(full) + len(scoped) > 1:
        return (f"{head} Several skills have that name: {', '.join([*full, *scoped])}. Call one "
                "by its full name; a skill scoped to a directory is for the files under it.")
    if full:
        return f"{head} Did you mean {full[0]}? Call it by that full name."
    if scoped:
        return (f"{head} Skills of that name scoped to directories exist: "
                f"{', '.join(scoped)}. Call the one whose directory holds the files you are "
                "working on.")
    near = closest_name(name, menu)
    return head + (f" Did you mean {near}?" if near is not None else "")


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
                    args: str | None = None, cwd: str | None = None,
                    root_id: str | None = None, effort: str | None = None) -> dict[str, Any]:
    """What activating ``desc`` records (``source``: ``model`` / ``user`` /
    ``preload``); ``root_id`` and ``effort`` fill the skill's variables."""
    raw = desc.meta("allowed-tools")
    allowed: list[str] | None = None
    if raw is not None:
        allowed = list(granted_tools(raw, skill=desc.name))
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
        "text": render_command_body(desc, args, cwd, root_id, effort),
    }


def unlocked_skills(state: Any) -> frozenset[str]:
    """The skills with ``paths`` a touched file has made available."""
    return frozenset((state or {}).get("unlocked") or ())
