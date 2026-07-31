import tests._builtin_skills as _skills
from tests._builtin_skills import BUILTIN_SKILLS_DIR
from noeta.agent.spec import agent_activates
from noeta.client.parts import (
    default_control_tools,
    default_guards_factory,
    default_policy_factory,
    default_reminder_specs,
    default_session_packs,
    derive_compaction_config,
)
from noeta.execution.builder import build_session_inputs
from noeta.policies.control_semantics import WORKFLOW_AGENT_NAME
from noeta.presets import official_specs

from tests._sdk_session import default_coding_budget

_ALIASES = {"default": "main"}

#: The per-feature ``build_session_inputs`` kwargs, grouped by the
#: plugin whose ``plugin_config`` entry carries them. ``memory_enabled`` is
#: handled separately (it becomes a ``capability_flags`` entry, not a
#: plugin_config key).
_LEGACY_PLUGIN_CONFIG_KEYS = {
    "fs": (
        "write_mode",
        "shell_mode",
        "shell_allowlist",
        "write_path_globs",
        "write_roots",
    ),
    "skills": (
        "skills_dir",
        "builtin_skills_dirs",
        "global_skills_dir",
        "allow_skill_scripts",
    ),
    "memory": ("memory_dir", "global_memory_dir"),
    "workspace": (
        "instructions_enabled",
        "instructions_file",
        "instructions_discovery",
    ),
}


def fold_legacy_capability_kwargs(kwargs: dict) -> None:
    """Translate the per-feature ``build_session_inputs`` kwargs into
    the generic ``capability_flags`` / ``plugin_config`` bags, in place.

    The builder takes two generic bags the capability packs self-gate and
    configure on. Direct callers keep passing the per-feature names; this
    helper pops each one actually present and folds it under the right key.
    Only keys the caller passed are folded, so any pack whose knob is omitted
    applies its own default. Caller-supplied ``capability_flags`` /
    ``plugin_config`` entries win over the translated knobs.
    """
    _missing = object()
    # capability_flags — the boolean kwargs fold in under their capability
    # names, merged with any caller-supplied flags (e.g. ``browser``); the
    # caller's entries win.
    _LEGACY_FLAG_KWARGS = {
        "memory_enabled": "memory",
        "delegation_enabled": "delegation",
        "todo_write_enabled": "todo_write",
        "ask_user_question_enabled": "ask_user_question",
        "skill_invocation_enabled": "skill_invocation",
        "workflow_enabled": "workflow",
    }
    folded_flags = {}
    for legacy, flag_name in _LEGACY_FLAG_KWARGS.items():
        value = kwargs.pop(legacy, _missing)
        if value is not _missing:
            folded_flags[flag_name] = value
    if folded_flags:
        capability_flags = dict(kwargs.get("capability_flags") or {})
        for flag_name, value in folded_flags.items():
            capability_flags.setdefault(flag_name, value)
        kwargs["capability_flags"] = capability_flags
    # plugin_config — per-plugin config bags built from the per-feature knobs,
    # with any caller-supplied plugin_config entries taking priority key-by-key.
    caller_config = {
        name: dict(cfg) for name, cfg in (kwargs.get("plugin_config") or {}).items()
    }
    plugin_config: dict[str, dict[str, object]] = {}
    for plugin, keys in _LEGACY_PLUGIN_CONFIG_KEYS.items():
        folded: dict[str, object] = {}
        for key in keys:
            if key in kwargs:
                folded[key] = kwargs.pop(key)
        if folded or plugin in caller_config:
            folded.update(caller_config.get(plugin, {}))
            plugin_config[plugin] = folded
    # Preserve any caller plugin_config entries for plugins with no legacy knob.
    for plugin, cfg in caller_config.items():
        plugin_config.setdefault(plugin, cfg)
    if plugin_config:
        kwargs["plugin_config"] = plugin_config


def default_factory_kwargs():
    """Loader-resolved injection kwargs required by ``build_session_inputs``
    (the manifest-resolved ``session_packs``) — tests that call the builder
    directly splat these in."""
    return {
        "session_packs": default_session_packs(),
        "control_tools": default_control_tools(),
        "base_reminders": default_reminder_specs(),
        "guards_factory": default_guards_factory(),
        "default_policy_factory": default_policy_factory(),
    }


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
    # build_session_inputs does not take the per-feature low-level kwargs —
    # they are folded into the generic ``capability_flags`` / ``plugin_config``
    # bags the capability packs self-gate + configure on. This funnel
    # TRANSLATES the per-feature names so its ~20 callers stay unchanged.
    # Pre-seed the always-defaulted knobs so
    # ``fold_legacy_capability_kwargs`` folds them in:
    #
    # * the three skill tiers (builtin < global < workspace) — wire the same
    #   low-level dirs the product live path does so the composed View matches.
    #   ``builtin_skills_dirs`` is the packaged tier (``BUILTIN_SKILLS_DIR``);
    #   the global tier is read from ``noeta.agent.skills.DEFAULT_GLOBAL_SKILLS_DIR``
    #   at call time (conftest redirects that module attribute to a tmp dir,
    #   keeping the suite hermetic).
    # * the memory switch is sourced from the agent's ``"memory"`` activation
    #   (the sole truth source for the memory flag; SdkHost + product resolve
    #   effective flags under the same discipline) — main on, the three sub-agents
    #   off. When replaying a recording that explicitly overrode
    #   cfg.memory_enabled, the caller passes the same value explicitly.
    kwargs.setdefault("builtin_skills_dirs", (BUILTIN_SKILLS_DIR,))
    kwargs.setdefault("global_skills_dir", _skills.DEFAULT_GLOBAL_SKILLS_DIR)
    kwargs.setdefault("memory_enabled", agent_activates(agent, "memory"))
    # plan's restricted-write path whitelist is host-injected
    # from the spec metadata at LIVE time, so the replay rebuild must derive the
    # SAME globs (otherwise plan's ``write`` tool schema → composed View → bytes
    # diverge). Mirrors noeta.agent.host.session._spec_write_path_globs. Derive
    # it BEFORE the fold so it lands in ``plugin_config["fs"]`` (the fs knobs are
    # no longer kernel-signature kwargs, spec §4.2).
    _raw_globs = agent.metadata.get("write_path_globs")
    if _raw_globs:
        kwargs.setdefault(
            "write_path_globs",
            tuple(p.strip() for p in _raw_globs.split(",") if p.strip()),
        )
    fold_legacy_capability_kwargs(kwargs)
    # build_session_inputs takes the manifest-resolved
    # session packs; default to the SDK's builtin set unless the caller
    # injects its own.
    kwargs.setdefault("session_packs", default_session_packs())
    # Every control tool (todo_write / ask_user_question / delegation / skill /
    # run_workflow / structured_output) arrives as a manifest-resolved
    # control_tool entry; the builder keeps no internal control-tool table.
    # Wire the SDK built-in set so a directly-built session mounts the same
    # control schemas the SdkHost live path does.
    kwargs.setdefault("control_tools", default_control_tools())
    kwargs.setdefault("base_reminders", default_reminder_specs())
    kwargs.setdefault("guards_factory", default_guards_factory())
    kwargs.setdefault("default_policy_factory", default_policy_factory())
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
