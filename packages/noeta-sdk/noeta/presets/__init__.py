"""The official factory agents: ``main`` and its three subagents, plus two
identities a host registers explicitly (``web``, ``__consolidation__``).

This is top-of-stack content: no other SDK module may import it (import-linter
``sdk-core-not-presets``), only consumers do.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from noeta.client.consolidation import CONSOLIDATION_AGENT_NAME
from noeta.client.options import (
    DEFAULT_PLUGINS,
    AgentDefinition,
    Options,
    compile_options,
    register_preset_prompt,
)
from noeta.protocols.resources import load_markdown

if TYPE_CHECKING:
    from noeta.agent.spec import AgentSpec


__all__ = [
    "CONSOLIDATION_AGENT",
    "CONSOLIDATION_AGENT_NAME",
    "MAIN_SYSTEM_PROMPT",
    "MAIN_WEB_SYSTEM_PROMPT",
    "MEMORY_POLICY_PROMPT",
    "OFFICIAL_SUBAGENTS",
    "WEB_SUBAGENT",
    "main_options",
    "official_specs",
    "sandbox_browser_options",
    "with_consolidation_agent",
]


# ---------------------------------------------------------------------------
# Prompt text lives in ``prompts/<name>.md`` resources, not inline, and is
# loaded with ``strip=False`` because the bytes feed the composer's stable
# prefix and therefore ``AgentSpec`` identity. A prompt carries role plus
# cross-tool workflow policy only — never the tool catalog, since each tool's
# semantics ride on its own structured description. The one-line roster
# descriptions stay inline below: they are part of the spawn schema, not prose.
# ---------------------------------------------------------------------------


def _load_prompt(name: str) -> str:
    """Load a factory agent's system prompt (``presets/prompts/<name>.md``, byte-faithful)."""
    return load_markdown("noeta.presets.prompts", name, strip=False)


#: The memory-policy prompt fragment: what earns a memory, what never does, and
#: the write hygiene (dedupe before writing / stable slugs / archive the stale).
#: It rides the preset prompt layer rather than the memory-index resident because
#: the index renders zero bytes on an empty store, yet the policy must be in place
#: before the first memory is ever written. :func:`_with_memory_policy` appends it
#: to every preset that activates ``memory``; exported so custom-spec authors who
#: set ``plugins=["memory"]`` can do the same.
MEMORY_POLICY_PROMPT = _load_prompt("memory-policy")


def _with_memory_policy(prompt: str) -> str:
    """Append the memory-policy fragment to a memory-enabled preset's prompt.

    The prompt files end with a newline, so adding one more yields exactly one
    blank line between the role prompt and the fragment — a deterministic
    separator (the result is part of the stable prefix).
    """
    return prompt + "\n" + MEMORY_POLICY_PROMPT


#: ``main``'s full system prompt: ``main.md`` plus the memory-policy fragment
#: (``main`` is the one official preset that activates ``memory``; the
#: three subagents are memory-free and get no fragment).
MAIN_SYSTEM_PROMPT = _with_memory_policy(_load_prompt("main"))
#: The sandbox-browser variant of ``main``'s prompt: identical to ``main.md``
#: except the delegation bullet also names the ``web`` specialist. A separate file
#: so ``main.md`` — every non-sandbox deployment's stable prefix — is never touched
#: by the browser variant, and a test pins the two to differ ONLY in that bullet.
#: Used by :func:`sandbox_browser_options`, because the prompt must not name
#: ``web`` unless ``web`` is actually in the roster. It inherits ``main``'s
#: activation, memory included, hence the same policy fragment.
MAIN_WEB_SYSTEM_PROMPT = _with_memory_policy(_load_prompt("main-web"))
_GENERAL_PURPOSE_PROMPT = _load_prompt("general-purpose")
_EXPLORE_PROMPT = _load_prompt("explore")
_PLAN_PROMPT = _load_prompt("plan")
_WEB_PROMPT = _load_prompt("web")


#: The read-mostly tool set shared by explore and plan: every built-in tool
#: except the write family (Edit / Write). ``Bash`` is in the
#: allowlist because read-only investigation needs it (ls / git log / git diff /
#: find / cat …); "no writes" is enforced by the prompt, with the approval gate
#: on ``high``-risk shell as the backstop.
_SCOUT_TOOLS = (
    "Glob",
    "Grep",
    "Read",
    "Bash",
    "BashOutput",
    "KillShell",
    "webfetch",
)


#: general-purpose's tool allowlist: the full built-in set, the same surface as
#: main, so it searches with ``Grep`` / ``Glob``, edits with ``Edit``, and
#: fetches with ``webfetch`` instead of being forced back through
#: ``Bash``. Its ``delegation`` capability stays off — a leaf
#: worker that spawns nothing further cannot fan out without bound.
_GENERAL_PURPOSE_TOOLS = (
    "Edit",
    "Glob",
    "Grep",
    "Read",
    "Bash",
    "BashOutput",
    "KillShell",
    "web_search",
    "webfetch",
    "Write",
)


#: The ``web`` subagent's whitelist-filtered base tools — the supporting cast
#: around browsing: read/write to save findings, read-only shell, and
#: ``webfetch`` for a raw fetch when no interaction is needed. The browser pack
#: (``browser_*``) is deliberately absent: it is gated by the ``browser``
#: activation plus a live sandbox backend, not by this whitelist. No ``Edit``
#: either — a browser worker writes fresh notes rather than editing a
#: codebase.
_WEB_TOOLS = (
    "Glob",
    "Grep",
    "Read",
    "Bash",
    "BashOutput",
    "KillShell",
    "webfetch",
    "Write",
)


# ---------------------------------------------------------------------------
# main's delegation roster. Every name here lands in main's ``spawn_subagent``
# schema, so adding one changes main's stable prefix for every deployment.
# ---------------------------------------------------------------------------


OFFICIAL_SUBAGENTS: dict[str, AgentDefinition] = {
    "general-purpose": AgentDefinition(
        description=(
            "General-purpose worker for self-contained coding tasks: "
            "search, read, write, edit, run shell commands, then return "
            "the result."
        ),
        prompt=_GENERAL_PURPOSE_PROMPT,
        tools=_GENERAL_PURPOSE_TOOLS,
        # No ``todo_write``: gp returns a value, it does not narrate progress.
        # ``mcp`` is on because a real working worker inherits the parent task's
        # enabled MCP tool set, connecting its own independent sessions.
        plugins=("skill_invocation", "mcp"),
    ),
    "explore": AgentDefinition(
        description=(
            "Read-only scout: fans out (glob/grep/read + read-only shell) "
            "to investigate the workspace and report facts (never edits)."
        ),
        prompt=_EXPLORE_PROMPT,
        tools=_SCOUT_TOOLS,
        plugins=("skill_invocation",),
    ),
    "plan": AgentDefinition(
        description=(
            "Architect: reads the code and returns a concrete ordered "
            "implementation plan (read-only — never writes any file)."
        ),
        prompt=_PLAN_PROMPT,
        # No write tool at all: the plan is returned as the agent's message,
        # never persisted to a file the caller then has to find and clean up.
        tools=_SCOUT_TOOLS,
        plugins=("ask_user_question",),
    ),
}


#: The browser specialist: a delegatable subagent whose one job is to drive the
#: sandbox container's browser. Its browsing-loop prompt isolates a web task's
#: token churn in its own context and returns a distilled answer to the parent.
#:
#: **Deliberately NOT in ``OFFICIAL_SUBAGENTS``.** Registering it there would put
#: ``web`` in ``main``'s spawnable roster and so change ``main``'s
#: ``spawn_subagent`` schema for EVERY deployment, including non-sandbox ones
#: where the browser cannot work at all. Browsing only makes sense under a
#: sandbox, so wiring ``web`` in is a host-activation decision
#: (:func:`sandbox_browser_options`), never an SDK default. ``web`` is the sole
#: identity that opens ``browser``; ``main`` stays browser-free and delegates.
WEB_SUBAGENT: AgentDefinition = AgentDefinition(
    description=(
        "Web-browsing specialist: drives the sandbox browser (navigate / "
        "click / type / extract) to research or operate live web pages, and "
        "returns a distilled answer."
    ),
    prompt=_WEB_PROMPT,
    tools=_WEB_TOOLS,
    plugins=("browser", "skill_invocation"),
)


#: The internal memory-consolidation curator (architecture:
#: ``docs/adr/memory-consolidation.md``). An ordinary agent driven as an
#: ordinary root task by the host's session-stop trigger
#: (``noeta.client.consolidation.run_consolidation``), with a goal carrying a
#: host-built digest of recent session activity. ``tools=()`` empties the
#: whitelist so ONLY the capability-gated memory pack attaches — no fs, no
#: shell, no delegation — which confines every effect on the store to the same
#: slug-checked ``memory_write`` / ``memory_archive`` the interactive agent uses.
#:
#: **Deliberately NOT in ``OFFICIAL_SUBAGENTS``**: it is nobody's delegate. Its
#: reserved ``__``-prefixed name keeps it out of a parent's ``spawnable``
#: auto-union at compile time and out of a host's advertised agent list —
#: resolvable for host-seeded root tasks, never model- or user-selectable, the
#: same treatment ``__workflow__`` gets. Hosts register it via
#: :func:`with_consolidation_agent`.
CONSOLIDATION_AGENT: AgentDefinition = AgentDefinition(
    description=(
        "Internal background curator of the long-term memory store: reads a "
        "host-built digest of recent session activity and merges, archives, "
        "or backfills memories through the memory tools alone."
    ),
    prompt=_with_memory_policy(_load_prompt("consolidation")),
    tools=(),
    plugins=("memory",),
)


def with_consolidation_agent(options: Options) -> Options:
    """Register :data:`CONSOLIDATION_AGENT` into ``options`` (idempotent shape).

    A host applies this when it runs the consolidation trigger, so the
    ``__consolidation__`` identity resolves for the trigger's ``seed_start``.
    Because the name is ``__``-reserved, registration changes NO spawnable
    roster and NO stable prefix — main's compiled spec is unaffected.
    """
    agents = dict(options.agents)
    agents[CONSOLIDATION_AGENT_NAME] = CONSOLIDATION_AGENT
    return dataclasses.replace(options, agents=agents)


def main_options() -> Options:
    """The official main recipe: full tool set, three subagents, all control tools.

    ``memory`` is activated on main alone: recall hooks into the user-message
    ingest seam, and only the top-level conversational agent receives user
    messages. explore and plan are read-only identities and general-purpose
    never converses with the user, so the three subagents leave it off.
    """
    return Options(
        system_prompt=MAIN_SYSTEM_PROMPT,
        name="main",
        agents=dict(OFFICIAL_SUBAGENTS),
        # ``spawnable`` is not listed here: compile derives it structurally from
        # the three subagents above, by additive union.
        plugins=DEFAULT_PLUGINS
        + ("todo_write", "ask_user_question", "skill_invocation", "memory", "mcp"),
    )


def sandbox_browser_options() -> Options:
    """Sandbox-activated variant of :func:`main_options`: the ``web`` subagent
    is registered into main's delegation roster. Main itself stays browser-free.

    A host calls this only once its deployment provisions a per-session sandbox,
    because that is the point at which the browser tool pack can actually work.
    Main deliberately does not open ``browser`` itself: given ``browser_*`` tools
    directly it would shortcut delegation — a one-line ``browser_navigate``
    always beats a ``spawn_subagent`` hop — and lose the context isolation that
    makes ``web`` worth having.

    Never a silent default: a deployment without a sandbox keeps
    :func:`main_options`, whose roster and stable prefix this function does not
    touch.
    """
    base = main_options()
    agents = dict(base.agents)
    agents["web"] = WEB_SUBAGENT
    # The prompt swaps to the web-aware variant in lockstep with the roster: a
    # prompt naming ``web`` without ``web`` being spawnable (or the reverse)
    # sends the model chasing a subagent that is not there.
    return dataclasses.replace(
        base, agents=agents, system_prompt=MAIN_WEB_SYSTEM_PROMPT
    )


def official_specs() -> dict[str, AgentSpec]:
    """Compile main and its subagents into specs keyed by name, ready to register."""
    main, descendants = compile_options(main_options())
    out: dict[str, AgentSpec] = {main.name: main}
    for d in descendants:
        out[d.name] = d
    return out


# Import-time registration is what makes ``SystemPromptPreset(preset="main")``
# and ``(preset="main-web")`` resolvable at all.
register_preset_prompt("main", MAIN_SYSTEM_PROMPT)
register_preset_prompt("main-web", MAIN_WEB_SYSTEM_PROMPT)
