"""The ``Skills`` extension: discovery, the listing, the ``skill`` tool,
activation, ``/<name>``, ``run_skill_script`` and ``allowed-tools``."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import msgspec

from noeta.core import models, records
from noeta.core.client import ClientAPI
from noeta.core.errors import TurnDone
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
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, Origin, ToolUse, user
from noeta.plugins.skills import _frontmatter
from noeta.plugins.skills.gates import (
    Enforcement,
    allowed_tools_gate,
    pre_approved,
    script_gate,
)
from noeta.plugins.skills.guidance import drop_skill_guidance
from noeta.plugins.skills.listing import (
    MENU_DESCRIPTION_MAX_CHARS,
    listing_lines,
    render_listing,
    skill_digest,
)
from noeta.plugins.skills.registry import (
    SkillDescription,
    SkillRegistry,
    command_name,
    index_dir,
    load_registry,
    parse_skill,
    plugin_skill_name,
    render_body,
    render_command_body,
    skill_dir_name,
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
from noeta.plugins.skills.usage import UsageRank
from noeta.toolkit import permission_rules as pr
from noeta.toolkit.commands import command_message, parse_command_message
from noeta.toolkit.context import new_user_messages, task_env
from noeta.toolkit.inline_commands import (
    InlineCommandError,
    has_commands,
    run_inline_commands,
    strip_commands,
)
from noeta.toolkit.tokens import (
    LISTING_BUDGET_FRACTION,
    estimate_tokens,
    listing_budget_chars,
    prefix_within,
)

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
#: One checksum of every ``SKILL.md`` (and a commands directory's ``*.md``;
#: paths and text) under the directories given as arguments: changes when a
#: skill is added, removed or edited.
_FINGERPRINT = ('for d in "$@"; do case "$d" in */commands) n="*.md" ;; *) n=SKILL.md ;; '
                'esac; [ -d "$d" ] && find -L "$d" -type f -name "$n" -print -exec cat {} + ; '
                'done 2>/dev/null | cksum')
#: Calls whose file makes a skill with matching ``paths`` available and
#: finds the skill directories on its way (Claude Code's: not a file a
#: ``Bash`` command showed, which counts only for instruction files).
FILE_TOOLS = frozenset({"Read", "Write", "Edit"})
EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: Ancestor walks remembered, by working directory.
ANCESTORS_KEEP = 256
#: Loading one of these unscoped skills first looks for skill folders on
#: the way to the repository's changed files (Claude Code's, for its
#: review skills), at most this many directories.
CHANGED_FILES_SKILLS = frozenset({"verify", "code-review"})
CHANGED_DIRS_MAX = 100

#: Heads the skills re-attached after a compaction.
INVOKED_SKILLS_HEADER = (
    "These skills were invoked earlier in this conversation, before it was summarized, not "
    "in the current turn. They are shown for context only: do not run them again or repeat "
    "their one-time setup (scheduling, creating files and the like). Any request inside them "
    "belongs to that earlier moment, not to the user's current message."
)

#: Claude Code's caps on the skills given again after a compaction: each
#: body cut to 5,000 tokens, newest invoked first, 25,000 in all (a skill
#: that does not fit is left out).
INVOKED_SKILL_MAX_TOKENS = 5_000
INVOKED_SKILLS_BUDGET_TOKENS = 25_000
INVOKED_SKILL_CUT = ("\n\n[... the rest of this skill was cut at the summary; Read the skill "
                     "file if you need all of it]")

#: The listing's place in the joined system turn (``Reminder.order``): after
#: the environment, deferred tools and agents, before the budget and date.
LISTING_ORDER = 50
#: The skills invoked before a compaction, after it: ahead of the lists.
INVOKED_ORDER = 20

#: The directories a workspace contributes, relative to its root (and to
#: each directory above it up to the repository's root).
WORKSPACE_SKILLS_DIR = ".noeta/skills"
WORKSPACE_AGENTS_SKILLS_DIR = ".agents/skills"
WORKSPACE_COMMANDS_DIR = ".noeta/commands"
#: The keep-order tier of a skill directory found below the working
#: directory. Such a skill never shadows another of the same name (Claude
#: Code lists the others first and keeps the first of a name).
NESTED_TIER = 5
#: Announces the skills of a directory found below the working directory.
NESTED_SKILLS = "Skills found in {dir}, now available through the Skill tool:"
#: After an unscoped skill is loaded while skills of its name scoped to
#: directories exist (Claude Code's note), one line per scoped skill.
SCOPED_VARIANTS = (
    "Skills of the same name as \"{name}\", scoped to directories, exist here:\n{lines}\n"
    "The plain name always means the unscoped skill just loaded; a scoped one is reached only "
    "by its full name. If the files you are working on lie under one of those directories, "
    "call that skill with the Skill tool now and follow it instead, since it holds that "
    "directory's own instructions; if your changes reach into several, run each one that "
    "matches.")
SCOPED_VARIANT_LINE = "- `{name}`: for files under {scope}/"
#: A typed ``/<name>`` of a ``context: fork`` skill: its agent runs in the
#: background and no model call answers the message (Claude Code's).
TYPED_FORK = ("<local-command-stdout>The {name} skill is running in the background as agent "
              "{id}.</local-command-stdout>")

Rank = Callable[[TaskView], "Mapping[str, float] | Awaitable[Mapping[str, float]] | None"]

#: A skill named mid-message (``/<name>`` that is not the message's command).
SKILL_MENTION = (
    "The user's message names /{name}, a skill. If they want it run, call the Skill tool with "
    "skill \"{name}\" and any arguments they gave as args; if they only mention it, do not "
    "run it.")
#: A ``/word`` after the start, whitespace or a CJK sentence mark, ending at
#: whitespace or the end (Claude Code's mention rule).
_MENTION = re.compile(r"(^|[\s。、？！])/([a-zA-Z][a-zA-Z0-9.:_-]*)")  # noqa: RUF001


def mentioned_skills(text: str) -> list[str]:
    """The ``/<name>`` words of ``text`` that are not its leading command,
    in order, each once."""
    out: list[str] = []
    leading = text.startswith("/")
    after = 0
    for m in _MENTION.finditer(text):
        name = m.group(2).rstrip(".")
        start, end = m.start(2) - 1, m.start(2) + len(name)
        if leading and not text[after:start].strip():
            after = end  # the command itself, or its leading run
            continue
        leading = False
        if end < len(text) and not text[end].isspace():
            continue
        if name not in out:
            out.append(name)
    return out


def _typed_command(text: str) -> tuple[str, str] | None:
    """``(name, args)`` of a slash command the person typed: the command's
    tags a front end sends, or the bare ``/<name> <args>``."""
    tagged = parse_command_message(text)
    if tagged is not None:
        return tagged[0], tagged[1].strip()
    # A scoped name's directory may hold a "/" (``/apps/web:deploy``).
    m = re.match(r"/([\w.-]+(?::[\w.-]+)*|[\w][\w./-]*:[\w.-]+)"
                 r"(?:\s+|$)(.*)", text.lstrip(), re.DOTALL)
    return (m.group(1), m.group(2).strip()) if m else None


def _scopes(cwd: str, nested: Sequence[str]) -> dict[str, str]:
    """Each skill directory found below ``cwd`` -> the directory it serves
    (the one holding its ``.noeta/skills`` or ``.agents/skills``), relative
    to ``cwd``."""
    return {root: os.path.relpath(os.path.dirname(os.path.dirname(root)), cwd)
            for root in nested}


def _noeta_first(nested: Sequence[str]) -> list[str]:
    """``nested`` with each directory's ``.noeta/skills`` ahead of its
    ``.agents/skills``: the first wins a name the two share."""
    out = list(nested)
    for d in nested:
        twin = os.path.join(os.path.dirname(os.path.dirname(d)), WORKSPACE_SKILLS_DIR)
        if d.endswith(WORKSPACE_AGENTS_SKILLS_DIR) and twin in out \
                and out.index(twin) > out.index(d):
            out.remove(twin)
            out.insert(out.index(d), twin)
    return out


def nested_dirs(state: Any) -> tuple[str, ...]:
    """The skill directories below the working directory a task has found."""
    return tuple((state or {}).get("nested") or ())


def _digests(registry: SkillRegistry, lines: Sequence[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, _line in lines:
        desc = registry.get(name)
        if desc is not None:
            out[name] = skill_digest(desc)
    return out


class Skills(Extension):
    """Skills from SKILL.md directories.

    Constructor arguments (0.6 ``HostConfig`` / skills plugin config):

    * ``dirs`` — the lowest tier: built-in, plugin and borrowed directories
      (``builtin_skills_dirs`` + ``extra_skill_dirs``).
    * ``plugin_dirs`` — directory -> plugin name: a Claude Code plugin's
      skill directories, in the lowest tier; their skills are named
      ``<plugin>:<skill directory>``, as Claude Code names them.
    * ``global_agents_dir`` / ``global_dir`` — the user's ``~/.agents/skills`` /
      ``~/.noeta/skills`` tiers (``None`` = none, the 0.6 default).
    * ``global_commands_dir`` — the user's commands directory
      (``~/.noeta/commands``): each ``*.md`` is a skill (Claude Code's
      ``~/.claude/commands``).
    * ``managed_dir`` — the machine's managed directory: its
      ``.noeta/skills`` and ``.noeta/commands`` go first (Claude Code's
      managed skills).
    * ``workspace`` — mount the workspace tiers ``.noeta/skills``,
      ``.agents/skills`` and ``.noeta/commands`` of the working directory and
      of each directory above it up to the repository's root (not the home
      directory), nearest first (repo-derived, so they pass ``trusted``).
      Claude Code's ``.claude/skills`` is never read.
    * ``additional_dirs`` — the task's other directories (``--add-dir``,
      ``additionalDirectories``): their ``.noeta/skills``, ``.agents/skills``
      and ``.noeta/commands`` come after the workspace's (trust-gated too).
    * ``plugin_vars`` — plugin skill directory -> ``{variable: value}`` filled
      into its skills' bodies (``${CLAUDE_PLUGIN_ROOT}``, its options).
    * ``plugin_commands`` — a Claude Code plugin's commands (``name``,
      ``path``, ``text``: the file with its variables filled in): skills the
      model can call, as in Claude Code; typing them stays the
      ``markdown_commands`` plugin's.
    * ``skills_dir`` — replaces the workspace tiers with this one directory
      (relative to the workspace), not trust-gated (0.6 ``skills_dir``).
    * ``trusted`` — ``trusted(workspace_path) -> bool`` gate for the
      repo-derived tiers; ``None`` = open (0.6 ``workspace_skills_trust``).
    * ``budget_chars`` — listing budget in characters; ``None`` =
      ``budget_fraction`` (default 1 %) of the task model's window times its
      characters per token (Claude Code's ``skillListingBudgetFraction``).
    * ``max_desc_chars`` — each listed description's cap (Claude Code's
      ``skillListingMaxDescChars``, default 1,536).
    * ``rank`` — ``rank(view) -> {skill: score}`` (sync or async), asked once
      when the listing is recorded; higher keeps more of its summary over
      budget (0.6 ``skill_menu_rank_resolver``). ``noeta.plugins.skills.usage``
      has a store-wide usage ranker a host can pass.
    * ``usage_from`` — a client whose tasks rank the listing by use
      (Claude Code's ranking across sessions; ``UsageRank``) when ``rank``
      is not given. The ``noeta`` command passes its store's.
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
        plugin_dirs: Mapping[str, str] | None = None,
        global_agents_dir: str | Path | None = None,
        global_dir: str | Path | None = None,
        global_commands_dir: str | Path | None = None,
        managed_dir: str | Path | None = None,
        workspace: bool = True,
        additional_dirs: Sequence[str | Path] = (),
        plugin_vars: Mapping[str, Mapping[str, str]] | None = None,
        plugin_commands: Sequence[Mapping[str, str]] = (),
        skills_dir: str | Path | None = None,
        trusted: Callable[[str], bool] | None = None,
        budget_chars: int | None = None,
        budget_fraction: float | None = None,
        max_desc_chars: int | None = None,
        rank: Rank | None = None,
        usage_from: ClientAPI | None = None,
        preload: Sequence[str] = (),
        scripts: bool = False,
        tool_enforcement: Enforcement = "off",
        rehang: bool = True,
        origin: Literal["system"] | None = "system",
        shell_commands: bool = True,
    ) -> None:
        if budget_chars is not None and budget_chars <= 0:
            raise ValueError("Skills: budget_chars must be a positive int")
        if budget_fraction is not None and not 0 < budget_fraction <= 1:
            raise ValueError("Skills: budget_fraction must be in (0, 1]")
        if max_desc_chars is not None and max_desc_chars <= 0:
            raise ValueError("Skills: max_desc_chars must be a positive int")
        if tool_enforcement not in ("off", "approval", "deny"):
            raise ValueError(f"Skills: unknown tool_enforcement {tool_enforcement!r}")
        self.dirs = [os.path.expanduser(str(d)) for d in dirs]
        self.plugin_dirs = {os.path.expanduser(str(d)): str(name)
                            for d, name in (plugin_dirs or {}).items()}
        self.global_agents_dir = os.path.expanduser(str(global_agents_dir)) \
            if global_agents_dir is not None else None
        self.global_dir = os.path.expanduser(str(global_dir)) if global_dir is not None else None
        self.global_commands_dir = os.path.expanduser(str(global_commands_dir)) \
            if global_commands_dir is not None else None
        self.managed_dir = os.path.expanduser(str(managed_dir)) \
            if managed_dir is not None else None
        self.workspace = workspace
        self.additional_dirs = [os.path.expanduser(str(d)) for d in additional_dirs]
        self.plugin_vars = {os.path.expanduser(str(d)): tuple(sorted(v.items()))
                            for d, v in (plugin_vars or {}).items()}
        self.plugin_commands = tuple(_plugin_command(c) for c in plugin_commands)
        self.skills_dir = str(skills_dir) if skills_dir is not None else None
        self.trusted = trusted
        self.budget_chars = budget_chars
        self.budget_fraction = budget_fraction
        self.max_desc_chars = max_desc_chars or MENU_DESCRIPTION_MAX_CHARS
        self.rank = rank if rank is not None or usage_from is None else UsageRank(usage_from)
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
        #: What the task's ``SKILL.md`` files held at its last model call.
        self._fingerprints: dict[str, str] = {}
        #: Fork skills whose commands wait until every check passed the
        #: ``Skill`` call: ``(task, call id)`` -> ``(Agent input, activation)``.
        self._forks_due: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        #: The directories from a working directory up to its repository's
        #: root, by working directory (each worker finds them for itself).
        self._ancestors: dict[str, tuple[str, ...]] = {}

    # ------------------------------------------------------------ discovery

    def tiers(self, cwd: str, nested: Sequence[str] = (),
              ancestors: Sequence[str] | None = None) -> list[tuple[str, int]]:
        """``(directory, keep-order tier)``, highest precedence first (the
        first skill of a name wins, as in Claude Code: the machine's, the
        user's, the workspace's from the working directory up, the
        additional directories', then commands directories, then the host's
        and plugins'); ``ancestors``: the working directory and those above
        it to scan (default: the working directory). ``nested`` are skill
        directories found below the working directory (deepest last): they
        never shadow, and come last."""
        managed = [(os.path.join(self.managed_dir, WORKSPACE_SKILLS_DIR), 2)] \
            if self.managed_dir is not None else []
        user: list[tuple[str, int]] = []
        if self.global_dir is not None:
            user.append((self.global_dir, 2))
        if self.global_agents_dir is not None:
            user.append((self.global_agents_dir, 1))
        workspace: list[tuple[str, int]] = []
        commands: list[tuple[str, int]] = []
        if self.managed_dir is not None:
            commands.append((os.path.join(self.managed_dir, WORKSPACE_COMMANDS_DIR), 2))
        if self.global_commands_dir is not None:
            commands.append((self.global_commands_dir, 2))
        below: list[tuple[str, int]] = []
        if self.skills_dir is not None:
            workspace.append((os.path.join(cwd, self.skills_dir), 4))
        elif self.workspace:
            if self.trusted is None or self.trusted(cwd):
                for d in [*(ancestors or [cwd]), *self.additional_dirs]:
                    workspace += [(os.path.join(d, WORKSPACE_SKILLS_DIR), 4),
                                  (os.path.join(d, WORKSPACE_AGENTS_SKILLS_DIR), 3)]
                    commands.append((os.path.join(d, WORKSPACE_COMMANDS_DIR), 4))
                below = [(d, NESTED_TIER) for d in _noeta_first(nested)]
            elif cwd not in _warned_untrusted:
                gated = [os.path.join(cwd, WORKSPACE_AGENTS_SKILLS_DIR),
                         os.path.join(cwd, WORKSPACE_SKILLS_DIR)]
                skipped = [d for d in gated if os.path.isdir(d)]
                if skipped:  # nothing is skipped where the directories do not exist
                    if len(_warned_untrusted) >= WARNED_KEEP:
                        del _warned_untrusted[next(iter(_warned_untrusted))]
                    _warned_untrusted[cwd] = None
                    _log.warning("workspace %s is not trusted; skipping its skill "
                                 "directories: %s", cwd, ", ".join(skipped))
        host = [(d, 0) for d in [*self.dirs, *self.plugin_dirs]]
        return managed + user + workspace + commands + host + below

    def commands_dirs(self, tiers: Sequence[tuple[str, int]]) -> frozenset[str]:
        """Which of ``tiers``' directories are commands directories."""
        own = {self.global_commands_dir}
        return frozenset(d for d, _ in tiers
                         if d in own or d.endswith(os.sep + WORKSPACE_COMMANDS_DIR))

    async def ancestors(self, env: ExecEnv) -> tuple[str, ...]:
        """The working directory and each directory above it, up to the
        repository's root (the first with a ``.git``) or the filesystem's,
        never the home directory (Claude Code's project directories), as
        ``env`` sees them."""
        cwd = env.cwd
        found = self._ancestors.get(cwd)
        if found is not None:
            return found
        home = os.path.expanduser("~")
        out: list[str] = []
        d = cwd
        while d != home:
            out.append(d)
            try:
                if await env.stat(os.path.join(d, ".git")) is not None:
                    break
            except OSError:
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        if len(self._ancestors) >= ANCESTORS_KEEP:
            del self._ancestors[next(iter(self._ancestors))]
        self._ancestors[cwd] = found = tuple(out) or (cwd,)
        return found

    async def _tiers(self, env: ExecEnv, state: Any) -> list[tuple[str, int]]:
        return self.tiers(env.cwd, nested_dirs(state), await self.ancestors(env))

    async def registry(self, env: ExecEnv, state: Any = None) -> SkillRegistry:
        """The skills ``env`` sees, with the nested directories ``state``
        (this plugin's) recorded."""
        return await self._load(env, await self._tiers(env, state), nested_dirs(state))

    async def _load(self, env: ExecEnv, tiers: Sequence[tuple[str, int]],
                    nested: Sequence[str]) -> SkillRegistry:
        return await load_registry(env, tiers, plugins=self.plugin_dirs,
                                   scopes=_scopes(env.cwd, nested),
                                   commands=self.commands_dirs(tiers),
                                   variables=self.plugin_vars,
                                   extra=[d for d in self.plugin_commands if d is not None])

    async def turn_registry(self, view: TaskView) -> SkillRegistry:
        """The skills as found for the task: loaded again when new input was
        taken, when the skill directories changed (a nested one found) or
        when :meth:`_refresh` saw their ``SKILL.md`` files change before a
        model call, not after every file a call touched."""
        env = task_env(view.env, view.state.config)
        nested = nested_dirs(view.ext_state(self.name))
        tiers = await self._tiers(env, view.ext_state(self.name))
        key = (view.state.inbox_read, env.cwd, tuple(tiers),
               self._fingerprints.get(view.task_id))
        cached = self._turn_registries.pop(view.task_id, None)
        if cached is not None and cached[0] == key:
            registry = cached[1]
        else:
            registry = await self._load(env, tiers, nested)
        self._turn_registries[view.task_id] = (key, registry)  # most recent last
        while len(self._turn_registries) > TURN_REGISTRIES_KEEP:
            del self._turn_registries[next(iter(self._turn_registries))]
        return registry

    async def _refresh(self, view: TaskView) -> None:
        """Note what the task's ``SKILL.md`` files hold now (one command over
        every skill directory), so a skill installed or changed mid-turn is
        found before the next model call (Claude Code watches the
        directories)."""
        env = task_env(view.env, view.state.config)
        roots = [root for root, _tier in await self._tiers(env, view.ext_state(self.name))]
        try:
            out = await env.run(["sh", "-c", _FINGERPRINT, "sh", *roots], timeout=10,
                                output_cap=256)
        except Exception:  # noqa: BLE001 — any exec env failure skips the look, not the call
            return
        now = out.stdout.decode("ascii", errors="replace")
        cached = self._turn_registries.get(view.task_id)
        if view.task_id not in self._fingerprints and cached is not None:
            # The first look: the registry loaded this turn stands for it.
            self._turn_registries[view.task_id] = ((*cached[0][:-1], now), cached[1])
        self._fingerprints[view.task_id] = now
        while len(self._fingerprints) > TURN_REGISTRIES_KEEP:
            del self._fingerprints[next(iter(self._fingerprints))]

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
        if kind == "listing_rebuilt":
            kept = data.get("kept") or {}
            out = [{"names": kept["names"], "lines": [], "digests": kept.get("digests") or {}}] \
                if kept.get("names") else []
            if data.get("names"):
                out.append({k: v for k, v in data.items() if k != "kept"} | {"held": True})
            return {**state, "listings": out}
        if kind == "turn":
            return {**state, "turn": data}
        if kind == "turn_done":
            return {**state, "turn": None}
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
            # Newest invocation last: the re-attach after a compaction reads
            # them newest first.
            rest = {k: v for k, v in active.items() if k != name}
            return {**state, "active": {**rest, name: entry}}
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
        if kind == "typed_fork":
            return {**state, "typed_fork": data["input"]}
        if kind == "typed_fork_done":
            return {**state, "typed_fork": None}
        if kind == "nested":
            return {**state, "nested": [*(state.get("nested") or []), data["dir"]]}
        if kind == "unlocked":
            have = list(state.get("unlocked") or [])
            return {**state, "unlocked": have + [n for n in data["names"] if n not in have]}
        return state

    @staticmethod
    def announced(state: Any) -> set[str]:
        return {n for listing in (state or {}).get("listings", []) for n in listing["names"]}

    @staticmethod
    def digests(state: Any) -> dict[str, str]:
        """The digest each skill was last listed with (a listing recorded
        before digests were kept has none)."""
        out: dict[str, str] = {}
        for listing in (state or {}).get("listings", []):
            out.update(listing.get("digests") or {})
        return out

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
        tiers = self.tiers(cwd, (), _local_ancestors(cwd))
        commands = self.commands_dirs(tiers)
        found: dict[str, str] = {}
        taken: set[str] = set()
        for root, _tier in tiers:
            base = Path(root)
            if not base.is_dir():
                continue
            plugin = self.plugin_dirs.get(root)
            if root in commands:
                paths = sorted(base.rglob("*.md"))
            else:
                paths = sorted(base.glob("*/SKILL.md"))
                if plugin is not None and (base / "SKILL.md").is_file():
                    paths.insert(0, base / "SKILL.md")
            for path in paths:
                rel = path.relative_to(base).as_posix()
                try:
                    raw = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                name: str | None
                if root in commands:
                    name = command_name(rel)
                elif plugin is not None:
                    try:
                        front = _frontmatter.parse(raw)[0].get("name")
                    except _frontmatter.FrontmatterError:
                        front = None
                    name = plugin_skill_name(plugin, rel, root, front)
                else:
                    name = rel.split("/")[0]
                    name = name if skill_dir_name(name) else None
                if name is None or name in taken:
                    continue
                desc = parse_skill(str(path), raw, name=name,
                                   own_dir=root not in commands or rel.endswith("SKILL.md"))
                if desc is None:
                    continue
                taken.add(name)
                if desc.user_invocable:
                    found[name] = desc.description
        return [Command(name=n, description=d, template=command_message(n, "{args}"))
                for n, d in sorted(found.items())]

    async def on_start(self, view: TaskView) -> None:
        state = view.ext_state(self.name) or {}
        env = task_env(view.env, view.state.config)
        registry: SkillRegistry | None = None
        if not state.get("listings"):
            registry = await self.registry(env)
            lines = listing_lines(registry, budget_chars=self._budget(view),
                                  rank=await self._rank(view), unlocked=unlocked_skills(state),
                                  max_desc_chars=self.max_desc_chars)
            if lines:
                # Held for the first request's system turn, after the prompt.
                view.record(self.name, "listing", {"names": [n for n, _ in lines],
                                                   "lines": [t for _, t in lines],
                                                   "digests": _digests(registry, lines),
                                                   "held": True})
        pending = [n for n in self.preload if n not in state.get("active", {})]
        if pending:
            registry = registry or await self.registry(env)
            for name in pending:
                desc = registry.get(name)
                if desc is None:
                    _log.warning("skills: preload %r is not an indexed skill; skipped", name)
                    continue
                data = activation_data(desc, "preload", cwd=view.cwd,
                                       root_id=_root(view), effort=_effort(view))
                try:
                    data = await self._expand(view, data, f"skill-preload-{name}")
                except InlineCommandError as exc:
                    _log.warning("skills: preload %r skipped: %s", name, exc)
                    continue
                self._record_invocation(view, data, None, self.origin)

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """The listing taken at start, in the environment's system turn
        (Claude Code's place for it); before every later model call, the
        skills not listed yet or whose listed text changed (Claude Code's
        skill listing attachment): installed since, found in a nested
        directory, or edited."""
        if view.env is not None:
            await self._refresh(view)
            await self._list_new(view)
        state = view.ext_state(self.name) or {}
        out: list[Reminder] = []
        if state.get("invoked"):
            out.append(Reminder(text=state["invoked"], kind="invoked_sent", data={},
                                role="system", order=INVOKED_ORDER))
        held = [line for listing in state.get("listings", []) if listing.get("held")
                for line in listing["lines"]]
        if held:
            out.append(Reminder(text=render_listing(held), kind="listing_sent", data={},
                                role="system", order=LISTING_ORDER))
        return out

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        messages = new_user_messages(view.state, recorded.seq)
        if not messages:
            return
        state = view.ext_state(self.name) or {}
        if state.get("turn") and not view.state.steps_in_turn:
            # A new turn: a skill's model and effort were for the last one.
            view.record(self.name, "turn_done", {})
        registry = await self.turn_registry(view)
        for message in messages:
            self._note_mentions(view, registry, state, message.text)
            typed = _typed_command(message.text)
            desc = registry.get(typed[0]) if typed else None
            if desc is not None and (not desc.user_invocable or (
                    desc.paths and desc.name not in unlocked_skills(state))):
                desc = None
            if typed and desc is not None and desc.fork \
                    and view.find_tool(AGENT_TOOL) is not None:
                await self._fork_typed(view, desc, typed[1], recorded.seq)
                continue
            if typed and desc is not None:
                prior = (state.get("active") or {}).get(desc.name)
                data = activation_data(desc, "user", typed[1], view.cwd, _root(view),
                                       _effort(view))
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
                self._note_turn(view, desc)
                registry = await self._changed_files_skills(view, desc, registry)
                self._note_variants(view, registry, desc, "inline")

    async def _fork_typed(self, view: TaskView, desc: SkillDescription, args: str,
                          seq: int) -> None:
        """A typed ``/<name>`` of a ``context: fork`` skill: its agent starts
        at once, in the background, and the message gets no model call; the
        agent's report arrives later as any agent's does (Claude Code
        forks it; noeta's agents always run in the background)."""
        call_id = f"skill-{seq}"
        data = activation_data(desc, "user", args, view.cwd)
        data["text"] = render_body(desc, args, view.cwd, _root(view), _effort(view))
        try:
            prompt = (await self._expand(view, data, call_id))["text"]
        except InlineCommandError as exc:
            view.notify(str(exc))
            view.record(self.name, "load_failed", {"name": desc.name},
                        message=user(f"The skill could not load: {exc}", origin="inline"))
            return
        tool = view.find_tool(AGENT_TOOL)
        assert tool is not None and view.env is not None

        def spawn(agent: str, prompt: Any, *, config: Mapping[str, Any] | None = None,
                  background: bool = True, queue: str | None = None,
                  history_from: str | None = None, history_through: int | None = None) -> str:
            return view.spawn(agent, prompt, config=config, queue=queue, key=call_id)

        ctx = ToolContext(task_id=view.task_id, call_id=call_id, env=view.env,
                          state=view.state.ext, record=view.record, spill_dir=view.spill_dir,
                          spawn=spawn, root_id=view.state.root_id or view.task_id,
                          model=view.model, prompt=view.prompt)
        output = await tool.run({"description": FORK_DESCRIPTION.format(name=desc.name),
                                 "prompt": prompt,
                                 "subagent_type": desc.agent or DEFAULT_FORK_AGENT,
                                 **_fork_options(tool, desc)}, ctx)
        text = "\n".join(getattr(c, "text", "") for c in output.content).strip()
        if output.is_error:
            view.notify(text)
            view.record(self.name, "load_failed", {"name": desc.name}, message=user(
                f"The skill could not start: {text}", origin="inline"))
            return
        view.record(self.name, "typed_fork", {"input": seq, "name": desc.name},
                    message=user(TYPED_FORK.format(name=desc.name, id=output.meta["child_id"]),
                                 origin="inline"))

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        """No model call answers a typed fork skill's message (its agent's
        report opens a turn later). The prompt's skill line goes only with
        skills and the ``Skill`` tool (:func:`drop_skill_guidance`). A skill
        loaded this turn with its own ``model`` or ``effort`` sets the rest of
        the turn's (Claude Code's)."""
        request = await self._skill_guidance(view, request)
        state = view.ext_state(self.name) or {}
        turn = state.get("turn") or {}
        changes = {k: turn[k] for k in ("model", "effort") if turn.get(k)}
        if changes:
            request = msgspec.structs.replace(request, **changes)
        at = state.get("typed_fork")
        if at is None:
            return request
        view.record(self.name, "typed_fork_done", {})
        if view.state.steps_in_turn:
            return request
        newest = max((seq for seq, m in zip(view.state.message_seqs, view.state.messages,
                                            strict=False)
                      if m.from_person and m.text), default=None)
        if newest == at:
            raise TurnDone()
        return request

    async def _skill_guidance(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        if any(t.name == SKILL_TOOL for t in request.tools) \
                and (await self.turn_registry(view)).names():
            return request
        system = [msgspec.structs.replace(p, text=drop_skill_guidance(p.text))
                  for p in request.system]
        return msgspec.structs.replace(request, system=system)

    def _note_turn(self, view: TaskView, desc: SkillDescription) -> None:
        """Keep a loaded skill's ``model`` and ``effort`` for the rest of
        the turn."""
        effort = desc.effort if desc.effort in EFFORTS else None
        if desc.effort and effort is None:
            _log.warning("skills: %s: effort %r is not one of %s; ignored", desc.name,
                         desc.effort, ", ".join(EFFORTS))
        model = models.resolve_alias(desc.model) if desc.model else None
        if model or effort:
            view.record(self.name, "turn", {"name": desc.name, "model": model,
                                            "effort": effort})

    async def _changed_files_skills(self, view: TaskView, desc: SkillDescription,
                                    registry: SkillRegistry) -> SkillRegistry:
        """Before naming a review skill's scoped variants: the skill folders
        on the way to each file git reports changed (Claude Code's), so a
        variant there is known; the registry as it is then."""
        if desc.unqualified is not None or desc.name not in CHANGED_FILES_SKILLS \
                or view.env is None:
            return registry
        env = task_env(view.env, view.state.config)
        try:
            top = await env.run(["git", "rev-parse", "--show-toplevel"], cwd=env.cwd,
                                timeout=5, output_cap=4096)
            root = top.stdout.decode("utf-8", errors="replace").strip()
            if top.exit_code != 0 or not root:
                return registry
            done = await env.run(["git", "status", "--porcelain", "-z",
                                  "--untracked-files=normal"], cwd=root, timeout=10,
                                 output_cap=1024 * 1024)
        except Exception:  # noqa: BLE001 — no git: nothing to look at
            return registry
        if done.exit_code != 0:
            return registry
        entries = done.stdout.decode("utf-8", errors="replace").split("\0")
        files: dict[str, str] = {}
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            paths = [entry[3:]]
            if "R" in entry[:2] or "C" in entry[:2]:
                paths.append(entries[i] if i < len(entries) else "")
                i += 1
            for rel in paths:
                path = os.path.normpath(os.path.join(root, rel))
                if rel and path != root and len(files) < CHANGED_DIRS_MAX \
                        and path.startswith(root.rstrip("/") + "/"):
                    files.setdefault(os.path.dirname(path), path)
        found: list[str] = []
        for path in files.values():
            await self._discover(view, path, found)
        if not found:
            return registry
        state = view.ext_state(self.name) or {}
        return await self.registry(env, {**state, "nested": [*nested_dirs(state), *found]})

    def _note_variants(self, view: TaskView, registry: SkillRegistry, desc: SkillDescription,
                       origin: Origin | None) -> None:
        """After an unscoped skill loaded: the skills of its name scoped to
        directories, if any (Claude Code's note)."""
        if desc.unqualified is not None:
            return
        variants = [d for d in registry.scoped(desc.name) if d.model_invocable]
        if not variants:
            return
        lines = "\n".join(SCOPED_VARIANT_LINE.format(
            name=d.name, scope=d.name[:-len(desc.name) - 1]) for d in variants)
        text = view.prompt("reminder/skill_scoped_variants", SCOPED_VARIANTS)
        view.record(self.name, "variants", {"name": desc.name}, message=user(
            text.replace("{name}", desc.name).replace("{lines}", lines), origin=origin))

    async def _list_new(self, view: TaskView) -> None:
        """List the skills not listed yet, or whose description changed (the
        whole first listing, ranked, when the task started with none)."""
        state = view.ext_state(self.name) or {}
        if any(listing.get("held") for listing in state.get("listings", [])):
            return  # the first request's listing is still to go out
        registry = await self.turn_registry(view)
        unlocked = unlocked_skills(state)
        only: list[str] | None = None
        if state.get("listings"):
            announced, digests = self.announced(state), self.digests(state)
            only = []
            for name in registry.model_invocable(unlocked):
                desc = registry.get(name)
                now = skill_digest(desc) if desc is not None else ""
                if name not in announced or digests.get(name, now) != now:
                    only.append(name)
            if not only:
                return
        # The new ones fitted to the whole budget by themselves, ranked
        # (Claude Code's).
        lines = listing_lines(registry, budget_chars=self._budget(view),
                              rank=await self._rank(view), unlocked=unlocked, only=only,
                              max_desc_chars=self.max_desc_chars)
        if lines:
            self._record_listing(view, lines, registry)

    def _note_mentions(self, view: TaskView, registry: SkillRegistry, state: Any,
                       text: str) -> None:
        """A note per skill the message names mid-text: whether to run it is
        the model's call (Claude Code's skill mention)."""
        if parse_command_message(text) is not None:
            return
        offered = set(registry.model_invocable(unlocked_skills(state)))
        for name in mentioned_skills(text):
            if name in offered:
                note = view.prompt("reminder/skill_mention", SKILL_MENTION)
                view.record(self.name, "mentioned", {"name": name},
                            message=user(note.replace("{name}", name), origin=self.origin))

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        path = _file_of(call)
        if call.name in FILE_TOOLS and not output.is_error and path is not None:
            await self._discover(view, path)
            await self._unlock(view, path)
        data = output.meta.get("skill_activation")
        if call.name != SKILL_TOOL or output.is_error or not isinstance(data, dict):
            return output
        prior = ((view.ext_state(self.name) or {}).get("active") or {}).get(data["name"])
        registry = await self.turn_registry(view)
        desc = registry.get(data["name"])
        if desc is not None:
            data = {**data, "text": render_command_body(desc, data.get("args"), view.cwd,
                                                        _root(view), _effort(view))}
        try:
            data = await self._expand(view, data, call.id)
        except InlineCommandError as exc:
            return text_output(str(exc), is_error=True)
        self._record_invocation(view, data, prior, self.origin)
        if desc is not None:
            self._note_turn(view, desc)
            registry = await self._changed_files_skills(view, desc, registry)
            self._note_variants(view, registry, desc, self.origin)
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
        if self.budget_chars is not None:
            return self.budget_chars
        budget = listing_budget_chars(str(view.state.config.get("model") or ""))
        if self.budget_fraction is not None:
            budget = max(1, int(budget * self.budget_fraction / LISTING_BUDGET_FRACTION))
        return budget

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
            data["text"] = prompt = render_body(desc, args, view.cwd, _root(view),
                                                _effort(view))
            if has_commands(prompt) and self.shell_commands:
                # Run once the rewritten call is gated: a deny of this
                # ``Skill`` call (any plugin's) has stopped it by then.
                due = data
            else:
                prompt = (await self._expand(view, data, call.id))["text"]
                view.record(self.name, "forked", {"call_id": call.id, "name": name,
                                                  "prompt": prompt})
        agent_input = {"description": FORK_DESCRIPTION.format(name=name), "prompt": prompt,
                       "subagent_type": desc.agent or DEFAULT_FORK_AGENT,
                       **_fork_options(view.find_tool(AGENT_TOOL), desc)}
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

    async def _discover(self, view: TaskView, path: str,
                        recorded: list[str] | None = None) -> None:
        """Skill directories (``.noeta/skills``, ``.agents/skills``) of the
        directories between the working directory and the file this call
        touched join the task, announced with their skills' names (Claude
        Code's nested skill discovery). A directory git ignores (a
        dependency's, a build's) is skipped, as Claude Code skips it.
        ``recorded``: directories this hook found already (its records are
        not folded yet); the ones found here are added."""
        recorded = recorded if recorded is not None else []
        if self.skills_dir is not None or not self.workspace:
            return
        env = task_env(view.env, view.state.config)
        cwd = env.cwd
        if self.trusted is not None and not self.trusted(cwd):
            return
        target = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
        rel = os.path.relpath(os.path.dirname(target), cwd)
        if rel == "." or rel.startswith(".."):
            return
        state = view.ext_state(self.name) or {}
        state = {**state, "nested": [*nested_dirs(state), *recorded]}
        known = set(nested_dirs(state))
        d = cwd
        for part in rel.split(os.sep):
            d = os.path.join(d, part)
            # ``.noeta/skills`` first: it wins a name the two share.
            for sub in (WORKSPACE_SKILLS_DIR, WORKSPACE_AGENTS_SKILLS_DIR):
                root = os.path.join(d, sub)
                if root in known:
                    continue
                found = await index_dir(env, root)
                if not found or await _git_ignored(env, root, cwd):
                    continue
                known.add(root)
                recorded.append(root)
                view.record(self.name, "nested", {"dir": root})
                state = {**state, "nested": [*nested_dirs(state), root]}
                registry = await self.registry(env, state)
                # Under the names they are offered by (a scoped one when
                # another skill has its name).
                paths = {desc.source_path for desc in found.values()
                         if desc.model_invocable and not desc.paths}
                names = sorted(n for n in registry.names()
                               if (won := registry.get(n)) is not None
                               and won.source_path in paths)
                if not names:
                    continue
                # Names only here; the next model call lists them with their
                # descriptions (Claude Code's dynamic_skill, then its listing).
                text = NESTED_SKILLS.format(dir=os.path.relpath(root, cwd)) + "\n" + "\n".join(
                    f"- {n}" for n in names)
                view.record(self.name, "nested_found", {"dir": root, "names": names},
                            message=user(text, origin=self.origin))

    async def _unlock(self, view: TaskView, path: str) -> None:
        """Offer the skills whose ``paths`` match the file the model touched,
        announced like a newly installed skill."""
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
        fresh = [n for n in names if n not in self.announced(state)]
        lines = listing_lines(registry, budget_chars=self._budget(view),
                              rank=await self._rank(view), unlocked={*unlocked, *names},
                              only=fresh, max_desc_chars=self.max_desc_chars) if fresh else ()
        if lines:
            self._record_listing(view, lines, registry)

    async def _expand(self, view: TaskView, data: dict[str, Any],
                      call_id: str) -> dict[str, Any]:
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
                        registry: SkillRegistry) -> None:
        names = [n for n, _ in lines]
        texts = [line for _, line in lines]
        view.record(self.name, "listing", {"names": names, "lines": texts,
                                           "digests": _digests(registry, lines)},
                    message=user(render_listing(texts), origin=self.origin))

    def _record_invocation(self, view: TaskView, data: dict[str, Any],
                           prior: Mapping[str, Any] | None, origin: Origin | None) -> None:
        """A skill invoked, by the model or a typed ``/name``: its text the
        first time; a short note when the same text is already in the
        conversation; a note and the whole text when its arguments or
        command output changed (Claude Code's). The text is kept as it was
        rendered: a compaction gives that again, not the file as it is then."""
        data = {**data, "rendered": data.get("rendered") or data["text"]}
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
        registry = await self.registry(task_env(view.env, view.state.config), state)
        if active:
            parts = [INVOKED_SKILLS_HEADER]
            used = 0
            for name, entry in reversed(active.items()):
                desc = registry.recorded(name)
                if desc is not None:
                    text = _cut_skill(entry.get("rendered") or strip_commands(
                        render_command_body(desc, entry.get("args"), view.cwd,
                                            _root(view), _effort(view)), ""))
                    size = estimate_tokens(text)
                    if used + size > INVOKED_SKILLS_BUDGET_TOKENS:
                        continue
                    used += size
                    parts.append(f"### Skill: {desc.name}\n"
                                 f"Path: {self._source(desc, registry)}:{desc.name}\n\n{text}")
            if len(parts) > 1 and view.keeps_tail is not None:
                view.record(self.name, "invoked_rehold", {"text": "\n\n".join(parts)})
        if view.keeps_tail is not None:
            # The listing again, built from the skills there are now (Claude
            # Code's): an edited skill once, a removed one gone. A listing
            # the kept messages still hold counts as sent: only the skills
            # it lacks, or whose text changed since, are listed.
            kept_texts = {m.text for m in view.kept}
            sent: dict[str, str] = {}
            for listing in state.get("listings") or []:
                if listing.get("lines") and render_listing(listing["lines"]) in kept_texts:
                    sent.update(dict.fromkeys(listing["names"], ""))
                    sent.update(listing.get("digests") or {})
            only = []
            for name in registry.model_invocable(unlocked_skills(state)):
                desc = registry.get(name)
                if desc is not None and sent.get(name, None) not in ("", skill_digest(desc)):
                    only.append(name)
            lines = listing_lines(registry, budget_chars=self._budget(view),
                                  rank=await self._rank(view), unlocked=unlocked_skills(state),
                                  only=only, max_desc_chars=self.max_desc_chars) if only else ()
            if lines or state.get("listings"):
                view.record(self.name, "listing_rebuilt", {
                    "names": [n for n, _ in lines], "lines": [t for _, t in lines],
                    "digests": _digests(registry, lines),
                    "kept": {"names": sorted(sent), "digests": {
                        n: d for n, d in sent.items() if d}}})
        return out

    def _source(self, desc: SkillDescription, registry: SkillRegistry) -> str:
        """Where a skill comes from, the label ahead of its name when it is
        given again after a compaction (Claude Code's ``<source>:<name>``):
        ``workspace`` (the workspace's own directories and those below the
        working directory), ``user`` (the user's directories), ``plugin``
        or ``host`` (the directories the host passed)."""
        tier = registry.tier_of(desc.name)
        if tier >= 3:
            return "workspace"
        if tier >= 1:
            return "user"
        source = desc.source_path or ""
        if any(source.startswith(d.rstrip("/") + "/") for d in self.plugin_dirs) \
                or any(d is not None and d.name == desc.name for d in self.plugin_commands):
            return "plugin"
        return "host"


def _cut_skill(text: str) -> str:
    """A skill body within ``INVOKED_SKILL_MAX_TOKENS``, cut with a note
    when longer (Claude Code's re-attach after a compaction)."""
    if estimate_tokens(text) <= INVOKED_SKILL_MAX_TOKENS:
        return text
    return text[:prefix_within(text, INVOKED_SKILL_MAX_TOKENS, INVOKED_SKILL_CUT)] \
        + INVOKED_SKILL_CUT


async def _git_ignored(env: ExecEnv, path: str, cwd: str) -> bool:
    """Whether git ignores ``path`` in the repository at ``cwd`` (``False``
    outside a repository or without git)."""
    try:
        done = await env.run(["git", "check-ignore", "-q", "--", path], cwd=cwd, timeout=5,
                             output_cap=4096)
    except Exception:  # noqa: BLE001 — no git: nothing is ignored
        return False
    return done.exit_code == 0 and not done.timed_out


def _local_ancestors(cwd: str) -> tuple[str, ...]:
    """:meth:`Skills.ancestors` on this machine (a front end's own
    directory)."""
    home = os.path.expanduser("~")
    out: list[str] = []
    d = cwd
    while d != home:
        out.append(d)
        if os.path.exists(os.path.join(d, ".git")):
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return tuple(out) or (cwd,)


def _root(view: TaskView) -> str:
    """What a skill's conversation-id variable says: the root task's id."""
    return view.state.root_id or view.task_id


def _effort(view: TaskView) -> str:
    """``${CLAUDE_EFFORT}`` when the skill sets none: the task's effort, else
    the model's default."""
    return view.effort or models.default_effort(view.model)


def _file_of(call: ToolUse) -> str | None:
    return next((call.input[k] for k in ("file_path", "notebook_path")
                 if isinstance(call.input.get(k), str) and call.input[k]), None)


def _fork_options(agent_tool: Any, desc: SkillDescription) -> dict[str, str]:
    """A fork skill's ``model`` and ``effort`` as its agent's, where the
    ``Agent`` tool takes that value."""
    props = ((getattr(agent_tool, "input_schema", None) or {}).get("properties") or {})
    out: dict[str, str] = {}
    for key, value in (("model", desc.model), ("effort", desc.effort)):
        allowed = (props.get(key) or {}).get("enum")
        if value and allowed and value in allowed:
            out[key] = value
    return out


def _plugin_command(entry: Mapping[str, str]) -> SkillDescription | None:
    """A plugin's command as a skill: the model's to call; the person types
    it through the ``markdown_commands`` plugin."""
    path = str(entry.get("path") or "")
    desc = parse_skill(path or str(entry["name"]), str(entry.get("text") or ""),
                       name=str(entry["name"]), own_dir=path.endswith("/SKILL.md"))
    return replace(desc, user_invocable=False) if desc is not None else None
