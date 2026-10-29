"""The ``Skills`` extension: discovery, the listing, the ``skill`` tool,
activation, ``/<name>``, ``run_skill_script`` and ``allowed-tools``."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import msgspec

from noeta.core import records
from noeta.core.events import Recorded
from noeta.core.execenv import ExecEnv
from noeta.core.extension import (
    Allow,
    Command,
    CommandContext,
    Deny,
    Extension,
    Gate,
    Reminder,
    Rewrite,
    TaskView,
)
from noeta.core.tool import Tool, ToolOutput, text_output
from noeta.core.types import Message, Origin, ToolUse, user
from noeta.plugins.skills import _frontmatter
from noeta.plugins.skills.gates import (
    Enforcement,
    allowed_tools_gate,
    pre_approved,
    script_gate,
)
from noeta.plugins.skills.listing import listing_lines, render_listing
from noeta.plugins.skills.registry import (
    SkillRegistry,
    load_registry,
    render_activation,
    render_body,
    render_command_body,
)
from noeta.plugins.skills.script import (
    SKILL_SCRIPT_TOOL_NAME,
    RunSkillScriptTool,
)
from noeta.plugins.skills.tool import (
    SKILL_TOOL,
    SkillTool,
    activation_data,
    skill_name,
    unlocked_skills,
)
from noeta.toolkit import permission_rules as pr
from noeta.toolkit.commands import command_message, parse_command_message
from noeta.toolkit.context import new_user_messages, task_env
from noeta.toolkit.inline_commands import (
    InlineCommandError,
    has_commands,
    run_inline_commands,
    strip_commands,
)
from noeta.toolkit.tokens import resident_budget

__all__ = [
    "SKILL_TOOL",
    "SkillTool",
    "Skills",
]

_log = logging.getLogger(__name__)
#: Workspaces already warned about as untrusted: every agent's Skills reads
#: the same directories, so the warning goes out once per process (for the
#: newest :data:`WARNED_KEEP` workspaces; a server sees many).
_warned_untrusted: dict[str, None] = {}
WARNED_KEEP = 1024

AGENT_TOOL = "Agent"
#: Fork skills waiting for their commands to run, remembered at most.
FORKS_DUE_KEEP = 256
#: The agent a ``context: fork`` skill runs in when it names none.
DEFAULT_FORK_AGENT = "general-purpose"
#: The forked agent's ``description``.
FORK_DESCRIPTION = "Run the {name} skill"
#: A skill called again while its unchanged instructions are in the
#: conversation; a changed one (other arguments, other command output)
#: gets the whole text after the second note.
REINVOKED_SAME = ("The /{name} skill was loaded earlier in this conversation and its "
                  "instructions have not changed; this is a new call, so follow them again now.")
REINVOKED_CHANGED = ("(/{name} called again: its instructions were loaded earlier; the "
                     "arguments or command output below are new.)")
#: Tasks whose turn's registry is kept (:meth:`Skills.turn_registry`).
TURN_REGISTRIES_KEEP = 256
#: Typed ``/skill`` inputs kept for an erase, and fork prompts kept for
#: their calls, newest last.
ERASABLE_KEEP = 32
FORKS_KEEP = 32
#: Calls whose file makes a skill with matching ``paths`` available.
FILE_TOOLS = frozenset({"Read", "Write", "Edit", "NotebookEdit"})

#: Heads the skills re-attached after a compaction.
INVOKED_SKILLS_HEADER = (
    "These skills were invoked earlier in this conversation, before it was summarized, not "
    "in the current turn. They are shown for context only: do not run them again or repeat "
    "their one-time setup (scheduling, creating files and the like). Any request inside them "
    "belongs to that earlier moment, not to the user's current message."
)

#: The listing's place in the joined system turn (``Reminder.order``): after
#: the environment, deferred tools and agents, before the budget and date.
LISTING_ORDER = 50
#: The skills invoked before a compaction, after it: ahead of the lists.
INVOKED_ORDER = 20

#: The directories a workspace contributes, relative to its root.
WORKSPACE_SKILLS_DIR = ".noeta/skills"
WORKSPACE_AGENTS_SKILLS_DIR = ".agents/skills"

Rank = Callable[[TaskView], "Mapping[str, float] | Awaitable[Mapping[str, float]] | None"]


def _typed_command(text: str) -> tuple[str, str] | None:
    """``(name, args)`` of a slash command the person typed: the command's
    tags a front end sends, or the bare ``/<name> <args>``."""
    tagged = parse_command_message(text)
    if tagged is not None:
        return tagged[0], tagged[1].strip()
    m = re.match(r"/([a-z0-9][a-z0-9-]*)(?:\s+|$)(.*)", text.lstrip(), re.DOTALL)
    return (m.group(1), m.group(2).strip()) if m else None


class Skills(Extension):
    """Skills from SKILL.md directories.

    Constructor arguments (0.6 ``HostConfig`` / skills plugin config):

    * ``dirs`` — the lowest tier: built-in, plugin and borrowed directories
      (``builtin_skills_dirs`` + ``extra_skill_dirs``).
    * ``global_agents_dir`` / ``global_dir`` — the user's ``~/.agents/skills`` /
      ``~/.noeta/skills`` tiers (``None`` = none, the 0.6 default).
    * ``workspace`` — mount the workspace tiers ``<cwd>/.agents/skills`` and
      ``<cwd>/.noeta/skills`` (repo-derived, so they pass ``trusted``).
      Claude Code's ``.claude/skills`` is never read.
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
    * ``rehang`` — put the listing and active skills right after a
      compaction's summary (``resident``; ADR-036 re-hang).
    * ``origin`` — origin of recorded listing / skill text (``"system"``).
    * ``shell_commands`` — run the ``!`command``` lines of a skill body when
      it loads (Claude Code's ``disableSkillShellExecution`` turned off);
      ``False`` leaves a marker in their place.
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
        rehang: bool = True,
        origin: Literal["system"] | None = "system",
        shell_commands: bool = True,
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
        self.rehang = rehang
        self.origin = origin
        self.shell_commands = shell_commands
        self._skill_tool = SkillTool(self)
        self._script_tool = RunSkillScriptTool(self.name)
        #: The registry a task's turn last loaded, by task: ``(turn key,
        #: registry)`` (see :meth:`turn_registry`).
        self._turn_registries: dict[str, tuple[tuple[Any, ...], SkillRegistry]] = {}
        #: Fork skills whose commands wait until every check passed the
        #: ``Skill`` call: ``(task, call id)`` -> ``(Agent input, activation)``.
        self._forks_due: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}

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
            elif cwd not in _warned_untrusted:
                skipped = [d for d in gated if os.path.isdir(d)]
                if skipped:  # nothing is skipped where the directories do not exist
                    if len(_warned_untrusted) >= WARNED_KEEP:
                        del _warned_untrusted[next(iter(_warned_untrusted))]
                    _warned_untrusted[cwd] = None
                    _log.warning("workspace %s is not trusted; skipping its skill "
                                 "directories: %s", cwd, ", ".join(skipped))
        return out

    async def registry(self, env: ExecEnv) -> SkillRegistry:
        return await load_registry(env, self.tiers(env.cwd))

    async def turn_registry(self, view: TaskView) -> SkillRegistry:
        """The skills as found once per turn of the task: loaded again when
        new input was taken (a skill installed since shows up with the
        person's next message), not after every file a call touched."""
        env = task_env(view.env, view.state.config)
        tiers = self.tiers(env.cwd)
        key = (view.state.inbox_read, env.cwd, tuple(tiers))
        cached = self._turn_registries.pop(view.task_id, None)
        if cached is not None and cached[0] == key:
            registry = cached[1]
        else:
            registry = await load_registry(env, tiers)
        self._turn_registries[view.task_id] = (key, registry)  # most recent last
        while len(self._turn_registries) > TURN_REGISTRIES_KEEP:
            del self._turn_registries[next(iter(self._turn_registries))]
        return registry

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"listings": [], "active": {}}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = state or self.initial_state()
        if kind == "listing":
            return {**state, "listings": [*state["listings"], data]}
        if kind == "invoked_rehold":
            return {**state, "invoked": data.get("text")}
        if kind == "invoked_sent":
            return {**state, "invoked": None}
        if kind == "listing_rehold":
            return {**state, "listings": [{**listing, "held": True}
                                          for listing in state["listings"]]}
        if kind == "listing_sent":
            return {**state, "listings": [{k: v for k, v in listing.items() if k != "held"}
                                          for listing in state["listings"]]}
        if kind == "activated":
            entry = {k: v for k, v in data.items() if k not in ("text", "input")}
            name, active = data["name"], state["active"]
            if data.get("input") is not None:
                # What a typed /name replaced, given back if a hook blocks it.
                typed, at = dict(state.get("typed") or {}), str(data["input"])
                typed[at] = [*typed.get(at, ()), [name, active.get(name)]]
                # Only a message the model has not seen yet is blocked: the
                # newest few are kept.
                state = {**state, "typed": dict(list(typed.items())[-ERASABLE_KEEP:])}
            return {**state, "active": {**active, name: entry}}
        if kind == records.ERASED_KIND:
            typed = dict(state.get("typed") or {})
            active = dict(state["active"])
            for seq in data["seqs"]:
                for name, prior in reversed(typed.pop(str(seq), ())):
                    if prior is None:
                        active.pop(name, None)
                    else:
                        active[name] = prior
            return {**state, "active": active, "typed": typed}
        if kind == "forked":
            forks = {**(state.get("forks") or {}), data["call_id"]: data["prompt"]}
            # Needed until the call runs: the reply's calls, at most.
            return {**state, "forks": dict(list(forks.items())[-FORKS_KEEP:])}
        if kind == "unlocked":
            have = list(state.get("unlocked") or [])
            return {**state, "unlocked": have + [n for n in data["names"] if n not in have]}
        return state

    @staticmethod
    def announced(state: Any) -> set[str]:
        return {n for listing in (state or {}).get("listings", []) for n in listing["names"]}

    # ------------------------------------------------------------ hooks

    def tools(self, view: TaskView) -> Sequence[Tool]:
        # Offered even with no skill listed, as Claude Code does: the prefix
        # does not move when the first skill is installed.
        tools: list[Tool] = [self._skill_tool]
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
        return [Command(name=n, description=d, template=command_message(n, "{args}"))
                for n, d in sorted(found.items())]

    async def on_start(self, view: TaskView) -> None:
        state = view.ext_state(self.name) or {}
        env = task_env(view.env, view.state.config)
        registry: SkillRegistry | None = None
        if not state.get("listings"):
            registry = await self.registry(env)
            lines = listing_lines(registry, budget_tokens=self._budget(view),
                                  rank=await self._rank(view), unlocked=unlocked_skills(state))
            if lines:
                # Held for the first request's system turn, after the prompt.
                view.record(self.name, "listing", {"names": [n for n, _ in lines],
                                                   "lines": [t for _, t in lines],
                                                   "held": True})
        pending = [n for n in self.preload if n not in state.get("active", {})]
        if pending:
            registry = registry or await self.registry(env)
            for name in pending:
                desc = registry.get(name)
                if desc is None:
                    _log.warning("skills: preload %r is not an indexed skill; skipped", name)
                    continue
                data = activation_data(desc, "preload", cwd=view.cwd)
                try:
                    data = await self._expand(view, data, f"skill-preload-{name}")
                except InlineCommandError as exc:
                    _log.warning("skills: preload %r skipped: %s", name, exc)
                    continue
                self._record_invocation(view, data, None, self.origin)

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """The listing taken at start, in the environment's system turn
        (Claude Code's place for it)."""
        state = view.ext_state(self.name) or {}
        out: list[Reminder] = []
        if state.get("invoked"):
            out.append(Reminder(text=state["invoked"], kind="invoked_sent", data={},
                                role="system", order=INVOKED_ORDER))
        held = [line for listing in state.get("listings", []) if listing.get("held")
                for line in listing["lines"]]
        if held:
            out.append(Reminder(text=render_listing(True, held), kind="listing_sent", data={},
                                role="system", order=LISTING_ORDER))
        return out

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        messages = new_user_messages(view.state, recorded.seq)
        if not messages:
            return
        state = view.ext_state(self.name) or {}
        registry = await self.turn_registry(view)
        # Skills installed since the last listing are announced with the
        # person's next message (the first listing, and with it the
        # ``skill`` tool, when the task started with none).
        if state.get("listings"):
            announced = self.announced(state)
            lines = listing_lines(registry, budget_tokens=self._budget(view),
                                  unlocked=unlocked_skills(state))
            added = [(n, line) for n, line in lines if n not in announced]
            if added:
                self._record_listing(view, added, first=False)
        else:
            lines = listing_lines(registry, budget_tokens=self._budget(view),
                                  rank=await self._rank(view), unlocked=unlocked_skills(state))
            if lines:
                self._record_listing(view, lines, first=True)
        for message in messages:
            typed = _typed_command(message.text)
            desc = registry.get(typed[0]) if typed else None
            if desc is not None and desc.paths and desc.name not in unlocked_skills(state):
                desc = None
            if typed and desc is not None:
                prior = (state.get("active") or {}).get(desc.name)
                data = activation_data(desc, "user", typed[1], view.cwd)
                # The body rides in the person's turn as plain text, after
                # the command's tags (Claude Code's slash-command shape).
                data["text"] = render_command_body(desc, typed[1], view.cwd)
                try:
                    data = await self._expand(view, data, f"skill-{recorded.seq}")
                except InlineCommandError as exc:
                    view.notify(str(exc))
                    view.record(self.name, "load_failed", {"name": desc.name},
                                message=user(f"The skill could not load: {exc}",
                                             origin="inline"))
                    continue
                self._record_invocation(view, {**data, "input": recorded.seq}, prior,
                                        "inline")

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        if call.name in FILE_TOOLS and not output.is_error:
            await self._unlock(view, call)
        data = output.meta.get("skill_activation")
        if call.name != SKILL_TOOL or output.is_error or not isinstance(data, dict):
            return output
        prior = ((view.ext_state(self.name) or {}).get("active") or {}).get(data["name"])
        try:
            data = await self._expand(view, data, call.id)
        except InlineCommandError as exc:
            return text_output(str(exc), is_error=True)
        self._record_invocation(view, data, prior, self.origin)
        meta = {k: v for k, v in output.meta.items() if k != "skill_activation"}
        meta["skill"] = data["name"]
        return msgspec.structs.replace(output, meta=meta)

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        if call.name == SKILL_TOOL:
            return await self._fork(view, call)
        if call.name == AGENT_TOOL:
            due = self._forks_due.pop((view.task_id, call.id), None)
            if due is not None and due[0] == call.input:
                return await self._expand_fork(view, call, due[1])
        state = view.ext_state(self.name) or {}
        active: dict[str, Any] = state.get("active") or {}
        if call.name == SKILL_SCRIPT_TOOL_NAME and self.scripts:
            return script_gate(call, active)
        restricted = allowed_tools_gate(call.name, active, self.tool_enforcement)
        if restricted is None and pre_approved(view, call, active):
            return Allow(approve=True)
        return restricted

    # ------------------------------------------------------------ helpers

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

    async def _fork(self, view: TaskView, call: ToolUse) -> Gate | None:
        """A ``context: fork`` skill runs as an ``Agent`` call: a sub-agent
        of the skill's ``agent`` type gets the rendered body as its prompt
        and its result comes back as any agent's does. Without an ``Agent``
        tool the skill loads inline."""
        name, args = skill_name(call.input.get("skill")), call.input.get("args")
        if name is None or (args is not None and not isinstance(args, str)):
            return None
        state = view.ext_state(self.name) or {}
        registry = await self.turn_registry(view)
        desc = registry.get(name)
        if desc is None or not desc.fork or name not in registry.model_invocable(
                unlocked_skills(state)) or view.find_tool(AGENT_TOOL) is None:
            return None
        prompt = (state.get("forks") or {}).get(call.id)
        due: dict[str, Any] | None = None
        if prompt is None:
            data = activation_data(desc, "model", args, view.cwd)
            data["text"] = prompt = render_body(desc, args, view.cwd)
            if has_commands(prompt) and self.shell_commands:
                # Run once the rewritten call is gated: a deny of this
                # ``Skill`` call (any plugin's) has stopped it by then.
                due = data
            else:
                prompt = (await self._expand(view, data, call.id))["text"]
                view.record(self.name, "forked", {"call_id": call.id, "name": name,
                                                  "prompt": prompt})
        agent_input = {"description": FORK_DESCRIPTION.format(name=name), "prompt": prompt,
                       "subagent_type": desc.agent or DEFAULT_FORK_AGENT}
        if due is not None:
            if len(self._forks_due) >= FORKS_DUE_KEEP:
                self._forks_due.pop(next(iter(self._forks_due)))
            self._forks_due[(view.task_id, call.id)] = (agent_input, due)
        return Rewrite(name=AGENT_TOOL, input=agent_input)

    async def _expand_fork(self, view: TaskView, call: ToolUse,
                           data: dict[str, Any]) -> Gate:
        """A fork skill's commands, run now that its ``Skill`` call passed."""
        try:
            prompt = (await self._expand(view, data, call.id))["text"]
        except InlineCommandError as exc:
            return Deny(reason=str(exc))
        view.record(self.name, "forked", {"call_id": call.id, "name": data["name"],
                                          "prompt": prompt})
        return Rewrite(name=AGENT_TOOL, input={**call.input, "prompt": prompt})

    async def _unlock(self, view: TaskView, call: ToolUse) -> None:
        """Offer the skills whose ``paths`` match the file this call touched,
        announced like a newly installed skill."""
        path = next((call.input[k] for k in ("file_path", "notebook_path")
                     if isinstance(call.input.get(k), str) and call.input[k]), None)
        if path is None:
            return
        state = view.ext_state(self.name) or {}
        unlocked = unlocked_skills(state)
        registry = await self.turn_registry(view)
        waiting = [d for d in registry.conditional() if d.name not in unlocked]
        if not waiting:
            return
        cwd = view.cwd or os.getcwd()
        ctx = pr.RuleContext(root=cwd, home=os.path.expanduser("~"), cwd=cwd)
        target = path if os.path.isabs(path) else os.path.join(cwd, path)
        rel = os.path.relpath(os.path.normpath(target), cwd)
        if rel.startswith(".."):
            return
        names = [d.name for d in waiting
                 if any(pr.path_pattern_matches(p, target, ctx) for p in d.paths)]
        if not names:
            return
        view.record(self.name, "unlocked", {"names": names})
        lines = listing_lines(registry, budget_tokens=self._budget(view),
                              unlocked={*unlocked, *names})
        fresh = [(n, line) for n, line in lines if n in names and n not in self.announced(state)]
        if fresh:
            self._record_listing(view, fresh, first=not state.get("listings"))

    async def _expand(self, view: TaskView, data: dict[str, Any], call_id: str
                      ) -> dict[str, Any]:
        """``data`` with the commands of its text run (or blanked when the
        host turned them off); the text they produced is kept as
        ``rendered`` so a compaction re-attaches it without running them
        again."""
        text = data["text"]
        if not has_commands(text):
            return data
        if not self.shell_commands:
            text = strip_commands(text)
        else:
            text = await run_inline_commands(view, text, skill=data["name"], call_id=call_id,
                                             approve_rules=data.get("allow_rules") or ())
        return {**data, "text": text, "rendered": text}

    def _record_listing(self, view: TaskView, lines: Sequence[tuple[str, str]],
                        *, first: bool) -> None:
        names = [n for n, _ in lines]
        texts = [line for _, line in lines]
        view.record(self.name, "listing", {"names": names, "lines": texts},
                    message=user(render_listing(first, texts), origin=self.origin))

    def _record_invocation(self, view: TaskView, data: dict[str, Any],
                           prior: Mapping[str, Any] | None, origin: Origin | None) -> None:
        """A skill invoked, by the model or a typed ``/name``: its text the
        first time; a short note when the same text is already in the
        conversation; a note and the whole text when its arguments or
        command output changed (Claude Code's)."""
        if prior is not None and (prior.get("args"), prior.get("rendered")) == (
                data.get("args"), data.get("rendered")):
            args = f" Arguments: {data['args']}" if data.get("args") else ""
            view.record(self.name, "reinvoked", {"name": data["name"]}, message=user(
                REINVOKED_SAME.format(name=data["name"]) + args, origin=origin))
            return
        if prior is not None:
            data = {**data, "text": REINVOKED_CHANGED.format(name=data["name"]) + "\n\n"
                    + data["text"]}
        view.record(self.name, "activated", data, message=user(data["text"], origin=origin))

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After a compaction, given again with the next request in its
        system turn: the skills invoked before it, in one note that says not
        to run them again, and the whole listing so far."""
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
                    text = entry.get("rendered") or strip_commands(
                        render_activation(desc, entry.get("args"), view.cwd), "")
                    parts.append(f"### Skill: {desc.name}\nPath: {desc.base_dir or desc.name}"
                                 f"\n\n{text}")
            if len(parts) > 1 and view.keeps_tail is not None:
                view.record(self.name, "invoked_rehold", {"text": "\n\n".join(parts)})
        if view.keeps_tail is not None and state.get("listings"):
            view.record(self.name, "listing_rehold", {})
        return out
