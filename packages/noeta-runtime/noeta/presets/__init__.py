"""noeta.presets — the four official factory agents.

No other SDK module may import this one (import-linter sdk-core-not-presets);
only consumers (the product / library users) import it.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from noeta.agent.spec import Capabilities
from noeta.client.options import (
    AgentDefinition,
    Options,
    compile_options,
    register_preset_prompt,
)
from noeta.protocols.resources import load_markdown

if TYPE_CHECKING:
    from noeta.agent.spec import AgentSpec


__all__ = [
    "MAIN_SYSTEM_PROMPT",
    "OFFICIAL_SUBAGENTS",
    "WEB_SUBAGENT",
    "main_options",
    "official_specs",
    "sandbox_browser_options",
]


# ---------------------------------------------------------------------------
# Prompt text: role + cross-tool workflow policy. The tool catalog/contract is
# not restated here — each tool's semantics ride on its structured description,
# which the composer renders into the provider tool schema.
# General rules use per-category
# wording; narrow pairwise choices sink into the relevant tool's description.
# (This module holds its own
# prompts and does not import noeta.agent.roster.)
#
# Prompt text is externalized into
# ``prompts/<name>.md`` resources (like tool descriptions: editable as docs,
# clean git diffs, non-engineers can change them). Loaded with ``strip=False``
# so it is byte-for-byte equal to the original constant and ``AgentSpec`` identity
# is unchanged. One-line roster descriptions are not externalized (they stay in
# OFFICIAL_SUBAGENTS below).
# ---------------------------------------------------------------------------


def _load_prompt(name: str) -> str:
    """Load a factory agent's system prompt (``presets/prompts/<name>.md``, byte-faithful)."""
    return load_markdown("noeta.presets.prompts", name, strip=False)


MAIN_SYSTEM_PROMPT = _load_prompt("main")
_GENERAL_PURPOSE_PROMPT = _load_prompt("general-purpose")
_EXPLORE_PROMPT = _load_prompt("explore")
_PLAN_PROMPT = _load_prompt("plan")
_WEB_PROMPT = _load_prompt("web")


#: The read-mostly tool set shared by explore and plan — aligned with Claude
#: Code's Explore/Plan agents: every built-in tool except the write family
#: (edit / write / apply_patch). ``shell_run`` is in the allowlist, but the
#: prompt restricts it to read-only commands (ls / git log / git diff / find /
#: cat …), matching CC — "no writes" is enforced by the prompt, with noeta's
#: approval gate on ``high`` risk shell as an extra backstop. (Previously
#: explore had only glob/grep/read and plan carried one restricted write; now
#: rebased to match CC: both get read-only shell + webfetch, and plan writes no
#: file at all — the plan is returned as the agent's message, never persisted.)
_SCOUT_TOOLS = (
    "glob",
    "grep",
    "read",
    "shell_kill",
    "shell_poll",
    "shell_run",
    "webfetch",
)


#: general-purpose's tool allowlist — aligned with Claude Code's general-purpose
#: (all tools). It gets the full built-in set (same tool surface as main): it can
#: search with ``grep`` / ``glob``, batch-edit with ``apply_patch``, and fetch
#: pages with ``webfetch`` instead of being forced back to ``shell_run`` for
#: grep / find. (The one spot we don't copy CC is recursive delegation: gp's
#: ``delegation`` capability stays off — it's a leaf worker, spawns nothing
#: further, avoiding unbounded fan-out.)
_GENERAL_PURPOSE_TOOLS = (
    "apply_patch",
    "edit",
    "glob",
    "grep",
    "read",
    "shell_kill",
    "shell_poll",
    "shell_run",
    "web_search",
    "webfetch",
    "write",
)


#: The ``web`` subagent's whitelist-filtered base tools. The browser pack
#: (``browser_*``) is NOT listed here — it is flag-gated by
#: ``Capabilities(browser=True)`` + a live sandbox backend (like memory), not by
#: this whitelist. These are the supporting tools: read/write to save findings,
#: read-only shell + ``webfetch`` (a raw-content fetch when no interaction is
#: needed). No ``edit``/``apply_patch`` — a browser worker writes fresh notes, it
#: does not batch-edit a codebase.
_WEB_TOOLS = (
    "glob",
    "grep",
    "read",
    "shell_kill",
    "shell_poll",
    "shell_run",
    "webfetch",
    "write",
)


# ---------------------------------------------------------------------------
# The three subagents of the four official agents (main + three subs = four).
# ---------------------------------------------------------------------------


OFFICIAL_SUBAGENTS: dict[str, AgentDefinition] = {
    "general-purpose": AgentDefinition(
        description=(
            "General-purpose worker for self-contained coding tasks: "
            "search, read, write, edit, run shell commands, then return "
            "the result."
        ),
        prompt=_GENERAL_PURPOSE_PROMPT,
        # Aligned to Claude Code's general-purpose agent: the full built-in
        # tool surface (same as main), so it searches with grep/glob and
        # batch-edits with apply_patch instead of falling back to shell
        # grep/find.
        tools=_GENERAL_PURPOSE_TOOLS,
        # todo_write disabled (gp returns a value, it does not narrate
        # progress); delegation stays off (no spawnable) — gp is a leaf worker
        # and never spawns further down. This is the one spot we intentionally
        # do NOT mirror CC, which lets general-purpose spawn agents.
        # ``mcp=True`` — the real working worker opts INTO
        # inheriting the parent task's enabled MCP tool set (it connects its
        # own independent sessions, R-1 records its own specs).
        capabilities=Capabilities(skill_invocation=True, mcp=True),
    ),
    "explore": AgentDefinition(
        description=(
            "Read-only scout: fans out (glob/grep/read + read-only shell) "
            "to investigate the workspace and report facts (never edits)."
        ),
        prompt=_EXPLORE_PROMPT,
        # Aligned to Claude Code's Explore: every built-in tool except the
        # write family (edit/write/apply_patch). shell_run is in the set but
        # the prompt restricts it to read-only commands.
        tools=_SCOUT_TOOLS,
        capabilities=Capabilities(skill_invocation=True),
    ),
    "plan": AgentDefinition(
        description=(
            "Architect: reads the code and returns a concrete ordered "
            "implementation plan (read-only — never writes any file)."
        ),
        prompt=_PLAN_PROMPT,
        # Aligned to Claude Code's Plan: same read-mostly surface as explore,
        # and NO write at all — the plan is returned as the agent's message
        # (with a "Critical Files" section), never written to disk. (Dropped
        # the old restricted plans/*.md write and its write_path_globs
        # metadata.)
        tools=_SCOUT_TOOLS,
        # plan opens ONLY ask_user_question (no todo_write, no skill_invocation).
        capabilities=Capabilities(ask_user_question=True),
    ),
}


#: The browser specialist (layer 4). A delegatable subagent whose one job is to
#: drive the sandbox container's browser: it holds the ``browser`` capability
#: (so the noeta-owned browser pack is merged when a live sandbox backend is
#: present) plus a read/write + read-only-shell base, and a browsing-loop prompt
#: that isolates a web task's token churn in its own context and returns a
#: distilled answer to the parent.
#:
#: **Deliberately NOT in ``OFFICIAL_SUBAGENTS``.** Registering it there would add
#: ``web`` to ``main``'s spawnable roster, which changes ``main``'s
#: ``spawn_subagent`` schema — churning ``main``'s stable prefix for EVERY
#: deployment, including non-sandbox ones where the browser cannot even work.
#: Browser only makes sense under a sandbox, so wiring ``web`` into the roster
#: is a **product-activation** concern, gated on ``NOETA_AGENT_SANDBOX`` (S10) —
#: not baked into the SDK presets. This definition is exported ready for that
#: gated registration (see :func:`sandbox_browser_options`). ``web`` is the sole
#: identity that opens ``browser``; ``main`` stays browser-free and delegates.
WEB_SUBAGENT: AgentDefinition = AgentDefinition(
    description=(
        "Web-browsing specialist: drives the sandbox browser (navigate / "
        "click / type / extract) to research or operate live web pages, and "
        "returns a distilled answer."
    ),
    prompt=_WEB_PROMPT,
    tools=_WEB_TOOLS,
    # browser: the noeta-owned browser pack (flag-gated, sandbox-backed).
    # skill_invocation on, matching the other workers.
    capabilities=Capabilities(browser=True, skill_invocation=True),
)


def main_options() -> Options:
    """The official main recipe: full tool set + three subagents + all control-plane capabilities.

    ``memory=True`` is enabled only on
    main — memory recall hooks into the user-message ingest seam, and only the
    top-level conversational agent receives user messages. explore/plan are
    read-only identities and the general-purpose subtask never converses with the
    user, so the three subagents leave it off (zero fingerprint drift).
    """
    return Options(
        system_prompt=MAIN_SYSTEM_PROMPT,
        name="main",
        agents=dict(OFFICIAL_SUBAGENTS),
        capabilities=Capabilities(
            todo_write=True,
            ask_user_question=True,
            delegation=True,
            skill_invocation=True,
            memory=True,
            # main opens MCP inheritance — a worker it delegates to
            # whose own spec also opens ``mcp`` inherits main's enabled servers.
            mcp=True,
        ),  # spawnable is filled in to the three sub-names by compile's additive union
    )


def sandbox_browser_options() -> Options:
    """Sandbox-activated variant of :func:`main_options`: the ``web`` subagent
    is registered into main's delegation roster. Main itself stays browser-free.

    Product-activation helper (the sandbox-browser-subsystem spec, D3 / B6):
    when a deployment provisions a per-session AIO Sandbox (``NOETA_AGENT_SANDBOX``
    on), the browser tool pack can actually work, so the ``web`` browsing
    specialist — the only identity that opens ``Capabilities.browser=True`` —
    is wired into main's delegation roster. Main does NOT open ``browser``: it
    has no ``browser_*`` tools and must delegate every page interaction to
    ``web`` (which isolates browsing token churn in a child context and returns
    a distilled result). Giving main the browser pack directly would let it
    shortcut delegation — a one-line ``browser_navigate`` always beats a
    ``spawn_subagent`` hop — so the browser capability lives on ``web`` alone.

    Off by default — non-sandbox deployments keep :func:`main_options` (no
    ``web`` agent, ``browser=False``) so the roster + stable prefix are
    byte-identical to pre-browser-subsystem. This function is the *explicit*
    opt-in a product uses to activate, never a silent default.
    """
    base = main_options()
    agents = dict(base.agents)
    agents["web"] = WEB_SUBAGENT
    # ``compile_options`` unions ``spawnable`` with the child names and keeps
    # ``delegation`` as-is (already True on main), so ``web`` becomes
    # delegatable. Main's own identity is left untouched — ``browser`` stays
    # ``False`` (it has no browser tools; every page interaction is delegated
    # to ``web``), so activation adds ``web`` to the roster and nothing else.
    return dataclasses.replace(base, agents=agents)


def official_specs() -> dict[str, AgentSpec]:
    """Compile the four agents and return them as a dict keyed by name (for the product registration path)."""
    main, descendants = compile_options(main_options())
    out: dict[str, AgentSpec] = {main.name: main}
    for d in descendants:
        out[d.name] = d
    return out


# Register the main preset prompt so SystemPromptPreset(preset="main") resolves.
register_preset_prompt("main", MAIN_SYSTEM_PROMPT)
