import tests._builtin_skills as _skills
from tests._builtin_skills import BUILTIN_SKILLS_DIR
from noeta.execution.builder import build_session_inputs, derive_compaction_config
from noeta.policies.control_tools import WORKFLOW_AGENT_NAME
from noeta.presets import official_specs

from tests._sdk_session import default_coding_budget

_ALIASES = {"default": "main"}


def known_subtask_agents(names):
    specs = official_specs()
    # __workflow__ is a reserved (non-roster) subtask agent — kept in
    # the guard allow-list (mirrors production ``_root_allowed_subtask_agents``)
    # but never resolved as a roster spec / surfaced in the spawn directory.
    return frozenset(
        n
        for n in names
        if n == WORKFLOW_AGENT_NAME or _ALIASES.get(n, n) in specs
    )


def subtask_directory(allowed):
    """Spawn directory rule, mirroring product
    ``noeta.agent.host.session._subtask_directory``: sorted (name, description)
    pairs; all-empty descriptions → empty tuple (preserves prior bytes)."""
    if not allowed:
        return ()
    specs = official_specs()
    entries = []
    for n in sorted(allowed):
        spec = specs.get(_ALIASES.get(n, n))
        if spec is None:
            continue
        entries.append((n, str(spec.metadata.get("description", ""))))
    if any(d for _, d in entries):
        return tuple(entries)
    return ()


def build_code_replay_inputs(*, workspace_dir, agent, content_store, model, **kwargs):
    # agent: AgentSpec(presets). Remaining kwargs pass through as in the old signature.
    budget = kwargs.pop("budget", None)
    allowed = kwargs.pop("allowed_subtask_agents", frozenset())
    known = known_subtask_agents(allowed)
    delegation = kwargs.get("delegation_enabled", False)
    # Default matches product CodeSessionConfig.skill_invocation_enabled (True),
    # so a session builds the same View product live does. Caller may override.
    kwargs.setdefault("skill_invocation_enabled", True)
    # three skill tiers (builtin < global <
    # workspace) — wire the same low-level dirs the product live path does so the
    # composed View matches. ``builtin_skills_dirs`` is the packaged tier
    # (``BUILTIN_SKILLS_DIR``); the global tier is read from
    # ``noeta.agent.skills.DEFAULT_GLOBAL_SKILLS_DIR`` at call time (conftest
    # redirects that module attribute to a tmp dir, keeping the suite hermetic).
    kwargs.setdefault("builtin_skills_dirs", (BUILTIN_SKILLS_DIR,))
    kwargs.setdefault("global_skills_dir", _skills.DEFAULT_GLOBAL_SKILLS_DIR)
    # the memory switch is sourced from agent
    # capabilities (capabilities is the sole truth source for the memory flag; SdkHost and
    # product resolve effective flags under the same discipline) — main on, the three
    # sub-agents off. When replaying a recording that explicitly overrode cfg.memory_enabled,
    # pass the same value explicitly.
    kwargs.setdefault("memory_enabled", agent.capabilities.memory)
    # plan's restricted-write path whitelist is host-injected
    # from the spec metadata at LIVE time, so the replay rebuild must derive the
    # SAME globs (otherwise plan's ``write`` tool schema → composed View → bytes
    # diverge). Mirrors noeta.agent.host.session._spec_write_path_globs.
    _raw_globs = agent.metadata.get("write_path_globs")
    if _raw_globs:
        kwargs.setdefault(
            "write_path_globs",
            tuple(p.strip() for p in _raw_globs.split(",") if p.strip()),
        )
    return build_session_inputs(
        workspace_dir=workspace_dir,
        system_prompt=agent.instructions,
        allowed_tools=frozenset(r.name for r in agent.tools),
        content_store=content_store,
        model=model,
        compaction=derive_compaction_config(model),
        budget=budget or default_coding_budget(),
        allowed_subtask_agents=known,
        subtask_agent_directory=subtask_directory(known) if delegation else (),
        **kwargs,
    )
