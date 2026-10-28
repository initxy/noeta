"""The ``Skills`` extension: discovery, the listing, the ``skill`` tool,
activation, ``/<name>``, ``run_skill_script`` and ``allowed-tools``."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import msgspec

from noeta.core.events import Recorded
from noeta.core.execenv import ExecEnv
from noeta.core.extension import Ask, Command, CommandContext, Deny, Extension, Gate, TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ToolUse, user
from noeta.plugins.skills import _frontmatter
from noeta.plugins.skills.allowed_tools import resolve_skill_allowed_tools
from noeta.plugins.skills.listing import listing_lines, render_listing
from noeta.plugins.skills.registry import (
    SkillDescription,
    SkillRegistry,
    load_registry,
    render_activation,
)
from noeta.plugins.skills.script import (
    SKILL_SCRIPT_TOOL_NAME,
    RunSkillScriptTool,
    is_skill_script_resource,
)
from noeta.toolkit.context import new_user_messages, resident_budget, task_env

__all__ = [
    "CONTROL_TOOLS",
    "SKILL_NOT_ALONE",
    "SKILL_TOOL",
    "SkillTool",
    "Skills",
]

_log = logging.getLogger(__name__)

SKILL_TOOL = "skill"

#: Tools that, like ``skill``, decide the shape of the response they are in:
#: ``skill`` beside one of them refuses the whole response.
CONTROL_TOOLS: frozenset[str] = frozenset({"skill", "Task", "TodoWrite", "AskUserQuestion"})

#: Heads the skills re-attached after a compaction.
INVOKED_SKILLS_HEADER = (
    "These skills were invoked earlier in this conversation, before it was summarized, not "
    "in the current turn. They are shown for context only: do not run them again or repeat "
    "their one-time setup (scheduling, creating files and the like). Any request inside them "
    "belongs to that earlier moment, not to the user's current message."
)

SKILL_NOT_ALONE = (
    "Skill not loaded: skill must be the only tool call in its response, "
    "because its instructions arrive with the step after it. The other tool "
    "calls in this response did run - their results are here. Call skill by "
    "itself now if you still need it."
)
_NOTHING_RAN = (
    "Nothing in this response ran: skill must be called on its "
    "own, once per response. Call skill by itself first, then "
    "re-issue the other calls in a separate response."
)
_SKILL_PROP_DESCRIPTION = "Name of the skill to activate, from the skill listing in your context."
_ARGS_PROP_DESCRIPTION = "Optional arguments for the skill."
_SKILL_NAME_MAX_LEN = 200

#: The directories a workspace contributes, relative to its root.
WORKSPACE_SKILLS_DIR = ".noeta/skills"
WORKSPACE_AGENTS_SKILLS_DIR = ".agents/skills"

Rank = Callable[[TaskView], "Mapping[str, float] | Awaitable[Mapping[str, float]] | None"]
Enforcement = Literal["off", "approval", "deny"]


def _skill_description() -> str:
    path = resources.files("noeta.plugins.skills").joinpath("descriptions", "skill.md")
    return path.read_text(encoding="utf-8").rstrip("\n")


class SkillTool:
    """``skill(skill, args?)``: the same schema bytes for every task. The listing
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
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": _SKILL_PROP_DESCRIPTION},
                "args": {"type": "string", "description": _ARGS_PROP_DESCRIPTION},
            },
            "required": ["skill"],
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        name = input.get("skill")
        if not isinstance(name, str) or not name:
            return text_output("skill must be a non-empty string", is_error=True)
        if len(name) > _SKILL_NAME_MAX_LEN:
            return text_output(f"skill too long (max {_SKILL_NAME_MAX_LEN})", is_error=True)
        registry = await self._owner.registry(task_env(ctx.env, {}))
        menu = registry.model_invocable()
        desc = registry.get(name)
        if desc is None or name not in menu:
            available = ", ".join(menu) if menu else "(none)"
            return text_output(f"unknown skill {name!r}; available: {available}", is_error=True)
        args = input.get("args")
        if args is not None and not isinstance(args, str):
            return text_output("args must be a string", is_error=True)
        return text_output(
            f"Skill '{name}' loaded; its instructions will appear in your "
            f"context from your next step.",
            skill_activation=_activation_data(desc, "model", args),
        )


def _activation_data(desc: SkillDescription, source: str,
                     args: str | None = None) -> dict[str, Any]:
    raw = desc.meta("allowed-tools")
    allowed: list[str] | None = None
    if raw is not None:
        [(_, grant)] = resolve_skill_allowed_tools(((desc.name, raw),))
        allowed = sorted(grant)
    return {
        "name": desc.name,
        "source": source,
        "dir": desc.base_dir,
        "scripts": [r for r in desc.resources if is_skill_script_resource(r)],
        "allowed_tools": allowed,
        "args": args,
        "text": render_activation(desc, args),
    }


class Skills(Extension):
    """Skills from SKILL.md directories.

    Constructor arguments (0.6 ``HostConfig`` / skills plugin config):

    * ``dirs`` — the lowest tier: built-in, plugin and borrowed directories
      (``builtin_skills_dirs`` + ``extra_skill_dirs``, e.g. ``~/.claude/skills``).
    * ``global_agents_dir`` / ``global_dir`` — the user's ``~/.agents/skills`` /
      ``~/.noeta/skills`` tiers (``None`` = none, the 0.6 default).
    * ``workspace`` — mount the workspace tiers ``<cwd>/.agents/skills`` and
      ``<cwd>/.noeta/skills`` (repo-derived, so they pass ``trusted``).
    * ``skills_dir`` — replaces the workspace tiers with this one directory
      (relative to the workspace), not trust-gated (0.6 ``skills_dir``).
    * ``trusted`` — ``trusted(workspace_path) -> bool`` gate for the
      repo-derived tiers; ``None`` = open (0.6 ``workspace_skills_trust``).
    * ``budget_tokens`` — listing budget; ``None`` = 1 % of the task model's
      window, at most 4,096 (0.6 ``menu_budget_tokens``).
    * ``rank`` — ``rank(view) -> {skill: score}`` (sync or async), asked once
      when the listing is recorded; higher keeps more of its summary over
      budget (0.6 ``skill_menu_rank_resolver``). ``noeta.plugins.skills.usage``
      has a store-wide usage ranker a host can pass.
    * ``preload`` — skills activated at task start (0.6 ``Options.skills``),
      ``disable-model-invocation`` ones included.
    * ``scripts`` — offer ``run_skill_script`` (0.6 ``allow_skill_scripts``).
    * ``tool_enforcement`` — ``allowed-tools`` of active skills: ``off``,
      ``approval`` (ask) or ``deny`` (0.6 ``skill_tool_enforcement``).
    * ``control_tools`` — tool names ``skill`` may not share a response with.
    * ``rehang`` — put the listing and active skills right after a
      compaction's summary (``resident``; ADR-036 re-hang).
    * ``origin`` — origin of recorded listing / skill text (``"system"``).
    """

    name = "skills"

    def __init__(
        self,
        *,
        dirs: Sequence[str | Path] = (),
        global_agents_dir: str | Path | None = None,
        global_dir: str | Path | None = None,
        workspace: bool = True,
        skills_dir: str | Path | None = None,
        trusted: Callable[[str], bool] | None = None,
        budget_tokens: int | None = None,
        rank: Rank | None = None,
        preload: Sequence[str] = (),
        scripts: bool = False,
        tool_enforcement: Enforcement = "off",
        control_tools: frozenset[str] = CONTROL_TOOLS,
        rehang: bool = True,
        origin: Literal["system"] | None = "system",
    ) -> None:
        if budget_tokens is not None and budget_tokens <= 0:
            raise ValueError("Skills: budget_tokens must be a positive int")
        if tool_enforcement not in ("off", "approval", "deny"):
            raise ValueError(f"Skills: unknown tool_enforcement {tool_enforcement!r}")
        self.dirs = [os.path.expanduser(str(d)) for d in dirs]
        self.global_agents_dir = os.path.expanduser(str(global_agents_dir)) \
            if global_agents_dir is not None else None
        self.global_dir = os.path.expanduser(str(global_dir)) if global_dir is not None else None
        self.workspace = workspace
        self.skills_dir = str(skills_dir) if skills_dir is not None else None
        self.trusted = trusted
        self.budget_tokens = budget_tokens
        self.rank = rank
        self.preload = list(preload)
        self.scripts = scripts
        self.tool_enforcement = tool_enforcement
        self.control_tools = control_tools
        self.rehang = rehang
        self.origin = origin
        self._skill_tool = SkillTool(self)
        self._script_tool = RunSkillScriptTool(self.name)

    # ------------------------------------------------------------ discovery

    def tiers(self, cwd: str) -> list[tuple[str, int]]:
        """``(directory, keep-order tier)``, low precedence first."""
        out = [(d, 0) for d in self.dirs]
        if self.global_agents_dir is not None:
            out.append((self.global_agents_dir, 1))
        if self.global_dir is not None:
            out.append((self.global_dir, 2))
        if self.skills_dir is not None:
            out.append((os.path.join(cwd, self.skills_dir), 4))
        elif self.workspace:
            gated = [os.path.join(cwd, WORKSPACE_AGENTS_SKILLS_DIR),
                     os.path.join(cwd, WORKSPACE_SKILLS_DIR)]
            if self.trusted is None or self.trusted(cwd):
                out += [(gated[0], 3), (gated[1], 4)]
            else:
                _log.warning("workspace %s is not trusted; skipping its skill directories: %s",
                             cwd, ", ".join(gated))
        return out

    async def registry(self, env: ExecEnv) -> SkillRegistry:
        return await load_registry(env, self.tiers(env.cwd))

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"listings": [], "active": {}}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = state or self.initial_state()
        if kind == "listing":
            return {**state, "listings": [*state["listings"], data]}
        if kind == "activated":
            entry = {k: v for k, v in data.items() if k != "text"}
            return {**state, "active": {**state["active"], data["name"]: entry}}
        return state

    @staticmethod
    def announced(state: Any) -> set[str]:
        return {n for listing in (state or {}).get("listings", []) for n in listing["names"]}

    # ------------------------------------------------------------ hooks

    def tools(self, view: TaskView) -> Sequence[Tool]:
        state = view.ext_state(self.name) or {}
        tools: list[Tool] = []
        if any(listing["names"] for listing in state.get("listings", [])):
            tools.append(self._skill_tool)
        if self.scripts:
            tools.append(self._script_tool)
        return tools

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        """``/<name>`` for every skill found locally (front ends run in the
        workspace); typing it activates the skill for that message."""
        return await asyncio.to_thread(self._commands, ctx.cwd)

    def _commands(self, cwd: str) -> list[Command]:
        found: dict[str, str] = {}
        for root, _tier in self.tiers(cwd):
            for path in sorted(Path(root).rglob("SKILL.md")) if Path(root).is_dir() else []:
                try:
                    fields, _body, _w = _frontmatter.parse(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, _frontmatter.FrontmatterError):
                    continue
                name = fields.get("name")
                if name and re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                    found[name] = fields.get("description", "")
        return [Command(name=n, description=d, template=f"/{n} {{args}}")
                for n, d in sorted(found.items())]

    async def on_start(self, view: TaskView) -> None:
        state = view.ext_state(self.name) or {}
        env = task_env(view.env, view.state.config)
        registry: SkillRegistry | None = None
        if not state.get("listings"):
            registry = await self.registry(env)
            lines = listing_lines(registry, budget_tokens=self._budget(view),
                                  rank=await self._rank(view))
            if lines:
                self._record_listing(view, lines, first=True)
        pending = [n for n in self.preload if n not in state.get("active", {})]
        if pending:
            registry = registry or await self.registry(env)
            for name in pending:
                desc = registry.get(name)
                if desc is None:
                    _log.warning("skills: preload %r is not an indexed skill; skipped", name)
                    continue
                self._record_activation(view, _activation_data(desc, "preload"))

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        messages = new_user_messages(view.state, recorded.seq)
        if not messages:
            return
        state = view.ext_state(self.name) or {}
        env = task_env(view.env, view.state.config)
        registry = await self.registry(env)
        # Skills installed since the last listing are announced with the
        # person's next message (the first listing, and with it the
        # ``skill`` tool, when the task started with none).
        if state.get("listings"):
            announced = self.announced(state)
            lines = listing_lines(registry, budget_tokens=self._budget(view))
            added = [(n, line) for n, line in lines if n not in announced]
            if added:
                self._record_listing(view, added, first=False)
        else:
            lines = listing_lines(registry, budget_tokens=self._budget(view),
                                  rank=await self._rank(view))
            if lines:
                self._record_listing(view, lines, first=True)
        for message in messages:
            m = re.match(r"/([a-z0-9][a-z0-9-]*)(?:\s+|$)(.*)", message.text.lstrip(),
                         re.DOTALL)
            desc = registry.get(m.group(1)) if m else None
            if m and desc is not None and desc.name not in state.get("active", {}):
                self._record_activation(view, _activation_data(desc, "user",
                                                               m.group(2).strip()))

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        data = output.meta.get("skill_activation")
        if call.name != SKILL_TOOL or output.is_error or not isinstance(data, dict):
            return output
        if data["name"] not in (view.ext_state(self.name) or {}).get("active", {}):
            self._record_activation(view, data)
        meta = {k: v for k, v in output.meta.items() if k != "skill_activation"}
        meta["skill"] = data["name"]
        return msgspec.structs.replace(output, meta=meta)

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        calls = [c.use for c in view.state.calls]
        skill_calls = [c for c in calls if c.name == SKILL_TOOL]
        if skill_calls:
            others = [c for c in calls if c.name != SKILL_TOOL]
            if len(skill_calls) == 1 and others and not any(
                    o.name in self.control_tools for o in others):
                if call.name == SKILL_TOOL:
                    return Deny(reason=SKILL_NOT_ALONE)
            elif len(skill_calls) != 1 or others:
                return Deny(reason=_NOTHING_RAN)
        if call.name == SKILL_TOOL:
            return None
        state = view.ext_state(self.name) or {}
        active: dict[str, Any] = state.get("active") or {}
        if call.name == SKILL_SCRIPT_TOOL_NAME and self.scripts:
            return _script_gate(call, active)
        return self._allowed_tools_gate(call.name, active)

    # ------------------------------------------------------------ helpers

    def _allowed_tools_gate(self, tool: str, active: Mapping[str, Any]) -> Gate | None:
        """Enforcement is on only when the mode is on and an active skill
        declares ``allowed-tools``; the grant is the union over them."""
        if self.tool_enforcement == "off":
            return None
        declaring = sorted(n for n, a in active.items() if a.get("allowed_tools") is not None)
        if not declaring:
            return None
        granted = {t for n in declaring for t in active[n]["allowed_tools"]}
        if tool in granted:
            return None
        reason = f"tool {tool!r} not in the allowed-tools grant of active skills {declaring!r}"
        if self.tool_enforcement == "deny":
            return Deny(reason=reason)
        return Ask(prompt=reason, detail={"tool": tool, "skills": declaring})

    def _budget(self, view: TaskView) -> int:
        if self.budget_tokens is not None:
            return self.budget_tokens
        return resident_budget(str(view.state.config.get("model") or ""))

    async def _rank(self, view: TaskView) -> Mapping[str, float] | None:
        if self.rank is None:
            return None
        try:
            got = self.rank(view)
            if inspect.isawaitable(got):
                got = await got
        except Exception:  # noqa: BLE001 — a rank is a nicety; never fail the task on it
            _log.warning("skills: rank resolver failed; keeping tier order", exc_info=True)
            return None
        return dict(got) if got else None

    def _record_listing(self, view: TaskView, lines: Sequence[tuple[str, str]],
                        *, first: bool) -> None:
        names = [n for n, _ in lines]
        texts = [line for _, line in lines]
        view.record(self.name, "listing", {"names": names, "lines": texts},
                    message=user(render_listing(first, texts), origin=self.origin))

    def _record_activation(self, view: TaskView, data: dict[str, Any]) -> None:
        view.record(self.name, "activated", data,
                    message=user(data["text"], origin=self.origin))

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After a compaction: the skills invoked before it, in one message
        that says not to run them again, then the whole listing so far."""
        if not self.rehang:
            return ()
        state = view.ext_state(self.name) or {}
        out: list[Message] = []
        active = state.get("active") or {}
        if active:
            registry = await self.registry(task_env(view.env, view.state.config))
            parts = [INVOKED_SKILLS_HEADER]
            for name, entry in active.items():
                desc = registry.get(name)
                if desc is not None:
                    parts.append(f"### Skill: {desc.name}\nPath: {desc.base_dir or desc.name}"
                                 f"\n\n{render_activation(desc, entry.get('args'))}")
            if len(parts) > 1:
                out.append(user("\n\n".join(parts), origin=self.origin))
        lines = [line for listing in state.get("listings", []) for line in listing["lines"]]
        if lines:
            out.append(user(render_listing(True, lines), origin=self.origin))
        return out


def _script_gate(call: ToolUse, active: Mapping[str, Any]) -> Gate:
    """Fail closed: malformed, inactive or undiscovered => deny; a
    discovered script of an active skill => ask. Never allow."""
    skill = call.input.get("skill")
    relpath = call.input.get("relpath")
    if not (isinstance(skill, str) and skill):
        return Deny(reason="run_skill_script requires a non-empty 'skill' string")
    if not (isinstance(relpath, str) and relpath):
        return Deny(reason="run_skill_script requires a non-empty 'relpath' string")
    if skill not in active:
        return Deny(reason=f"skill {skill!r} is not active; cannot run its scripts")
    if relpath not in (active[skill].get("scripts") or []):
        return Deny(reason=f"{skill!r}/{relpath!r} is not a discovered skill script")
    return Ask(prompt=f"running skill script {skill!r}/{relpath!r} requires approval",
               detail={"skill": skill, "relpath": relpath, "args": call.input.get("args", [])})
