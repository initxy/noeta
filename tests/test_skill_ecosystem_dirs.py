"""Skill-ecosystem directory tiers — `.agents/skills`, borrowed dirs, trust.

The pack assembles the tier list from its config entry; precedence (low→high)
is builtin+plugin+extra < `~/.agents/skills` < `~/.noeta/skills` < workspace
`.agents/skills` < workspace `.noeta/skills`. Two rules produce it: wider
scope below narrower scope, and at equal scope the vendor-neutral directory
below the vendor-specific one — `.noeta/skills` keeps sovereignty. The
workspace tiers (repo content) can be gated on the plugin trust store; an
operator `skills_dir` override is host config and stays outside the gate.

Membership and shadowing are asserted through the contributed content kind's
fingerprint lookup — `(version, sha256(SKILL.md bytes))` — the same identity
the composer pins for resume, so a tier win is observed exactly where it
matters.
"""

from __future__ import annotations

import hashlib
import warnings
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from tests._skill_fixtures import write_skill_raw

from noeta.builtins.skills.impl import (
    UntrustedWorkspaceSkillsWarning,
    build_skills_session_pack,
)
from noeta.client.plugins import grant_trust
from noeta.execution.session_pack import PackContribution, SessionBuildContext
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore


def _skill_file(tier_dir: Path, name: str, marker: str) -> Path:
    """One skill under ``tier_dir``, body tagged with ``marker``; returns the
    SKILL.md path so a test can pin the winning bytes."""
    write_skill_raw(
        tier_dir,
        name,
        f"---\nname: {name}\ndescription: {marker}\n---\n\n{marker}\n",
    )
    return tier_dir / name / "SKILL.md"


def _pack(ws: Path, cfg: Mapping[str, Any]) -> PackContribution:
    ws.mkdir(parents=True, exist_ok=True)
    ctx = SessionBuildContext(
        workspace=WorkspaceRoot.from_path(ws),
        workspace_dir=ws,
        content_store=InMemoryContentStore(),
        exec_env=None,
        model="test-model",
        provider_family=None,
        allowed_tools=frozenset(),
        backends={},
        capability_flags={},
        plugin_config={"skills": cfg},
    )
    return build_skills_session_pack(ctx)


def _digest(contribution: PackContribution, name: str) -> Optional[str]:
    """The indexed skill's SKILL.md sha256, or None when it did not load."""
    hashes = contribution.content_kinds[0].spec.hashes
    assert hashes is not None
    got = hashes(name)
    return None if got is None else got[1]


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Tier precedence
# ---------------------------------------------------------------------------


def test_workspace_noeta_shadows_every_other_tier(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _skill_file(tmp_path / "extra", "edit", "EXTRA")
    _skill_file(tmp_path / "agents-global", "edit", "AGENTS-GLOBAL")
    _skill_file(tmp_path / "noeta-global", "edit", "NOETA-GLOBAL")
    _skill_file(ws / ".agents" / "skills", "edit", "WS-AGENTS")
    winner = _skill_file(ws / ".noeta" / "skills", "edit", "WS-NOETA")

    contribution = _pack(
        ws,
        {
            "extra_skill_dirs": (tmp_path / "extra",),
            "global_agents_skills_dir": tmp_path / "agents-global",
            "global_skills_dir": tmp_path / "noeta-global",
            "workspace_agents_tier": True,
        },
    )
    assert _digest(contribution, "edit") == _file_digest(winner)


def test_workspace_agents_beats_home_tiers_yields_to_workspace_noeta(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    _skill_file(tmp_path / "agents-global", "edit", "AGENTS-GLOBAL")
    _skill_file(tmp_path / "noeta-global", "edit", "NOETA-GLOBAL")
    winner = _skill_file(ws / ".agents" / "skills", "edit", "WS-AGENTS")

    contribution = _pack(
        ws,
        {
            "global_agents_skills_dir": tmp_path / "agents-global",
            "global_skills_dir": tmp_path / "noeta-global",
            "workspace_agents_tier": True,
        },
    )
    assert _digest(contribution, "edit") == _file_digest(winner)


def test_lowest_tier_order_builtin_then_extra_then_agents_global(
    tmp_path: Path,
) -> None:
    """Within the home/lowest band: builtin < extra < `~/.agents/skills` <
    `~/.noeta/skills` — each step shadows the previous."""
    ws = tmp_path / "ws"
    _skill_file(tmp_path / "builtin", "edit", "BUILTIN")
    extra = _skill_file(tmp_path / "extra", "edit", "EXTRA")
    contribution = _pack(
        ws,
        {
            "builtin_skills_dirs": (tmp_path / "builtin",),
            "extra_skill_dirs": (tmp_path / "extra",),
        },
    )
    assert _digest(contribution, "edit") == _file_digest(extra)

    agents_global = _skill_file(tmp_path / "agents-global", "edit", "AGENTS-GLOBAL")
    contribution = _pack(
        ws,
        {
            "builtin_skills_dirs": (tmp_path / "builtin",),
            "extra_skill_dirs": (tmp_path / "extra",),
            "global_agents_skills_dir": tmp_path / "agents-global",
        },
    )
    assert _digest(contribution, "edit") == _file_digest(agents_global)

    noeta_global = _skill_file(tmp_path / "noeta-global", "edit", "NOETA-GLOBAL")
    contribution = _pack(
        ws,
        {
            "builtin_skills_dirs": (tmp_path / "builtin",),
            "extra_skill_dirs": (tmp_path / "extra",),
            "global_agents_skills_dir": tmp_path / "agents-global",
            "global_skills_dir": tmp_path / "noeta-global",
        },
    )
    assert _digest(contribution, "edit") == _file_digest(noeta_global)


def test_workspace_agents_tier_off_without_switch(tmp_path: Path) -> None:
    """No ``workspace_agents_tier`` key (the reduced orchestration shape) ⇒
    the `.agents/skills` tier does not mount; the `.noeta/skills` pack does."""
    ws = tmp_path / "ws"
    _skill_file(ws / ".agents" / "skills", "borrowed", "WS-AGENTS")
    _skill_file(ws / ".noeta" / "skills", "own", "WS-NOETA")

    contribution = _pack(ws, {})
    assert _digest(contribution, "borrowed") is None
    assert _digest(contribution, "own") is not None


# ---------------------------------------------------------------------------
# Borrowed ecosystems — extra_skill_dirs
# ---------------------------------------------------------------------------


def test_extra_dir_indexes_claude_code_shaped_skill(tmp_path: Path) -> None:
    """A Claude Code-flavoured skill directory loads verbatim through
    ``extra_skill_dirs`` — CC frontmatter keys survive as opaque metadata."""
    ws = tmp_path / "ws"
    claude_dir = tmp_path / "dot-claude-skills"
    write_skill_raw(
        claude_dir,
        "commit-helper",
        "---\n"
        "name: commit-helper\n"
        "description: Draft a commit message\n"
        "allowed-tools: Read, Bash(git diff:*)\n"
        "disable-model-invocation: false\n"
        "---\n\nLook at the staged diff and draft a message.\n",
    )
    contribution = _pack(ws, {"extra_skill_dirs": (claude_dir,)})
    assert _digest(contribution, "commit-helper") is not None


# ---------------------------------------------------------------------------
# Workspace trust gate
# ---------------------------------------------------------------------------


def test_trust_store_untrusted_drops_both_workspace_tiers(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _skill_file(ws / ".agents" / "skills", "ws-agents-skill", "WS-AGENTS")
    _skill_file(ws / ".noeta" / "skills", "ws-noeta-skill", "WS-NOETA")
    _skill_file(tmp_path / "noeta-global", "global-skill", "GLOBAL")

    with pytest.warns(UntrustedWorkspaceSkillsWarning) as recorded:
        contribution = _pack(
            ws,
            {
                "global_skills_dir": tmp_path / "noeta-global",
                "workspace_agents_tier": True,
                "workspace_skills_trust": "trust-store",
                "trust_store": tmp_path / "trust.json",
            },
        )
    assert _digest(contribution, "ws-agents-skill") is None
    assert _digest(contribution, "ws-noeta-skill") is None
    # Lower tiers are not repo content and stay up.
    assert _digest(contribution, "global-skill") is not None
    # The warning carries the trust subject and the dropped tiers as
    # attributes — consumable without parsing the message.
    warning = recorded.list[0].message
    assert isinstance(warning, UntrustedWorkspaceSkillsWarning)
    assert warning.workspace_dir == ws
    assert warning.skipped_dirs == (
        ws / ".agents" / "skills",
        ws / ".noeta" / "skills",
    )
    assert str(ws) in str(warning)


def test_unknown_trust_mode_fails_loud(tmp_path: Path) -> None:
    """A typo on a security knob must not silently read as 'open'."""
    ws = tmp_path / "ws"
    with pytest.raises(ValueError, match="workspace_skills_trust"):
        _pack(ws, {"workspace_skills_trust": "trust_store"})


def test_trust_subject_keys_the_gate_not_workspace_dir(tmp_path: Path) -> None:
    """Sandbox shape: the pack's workspace_dir is a container path the operator
    never granted; the host-supplied ``trust_subject`` (the host repo path) is
    what the gate must key on."""
    container_ws = tmp_path / "container-ws"
    _skill_file(container_ws / ".noeta" / "skills", "ws-skill", "WS")
    host_repo = tmp_path / "host-repo"
    host_repo.mkdir()
    store = tmp_path / "trust.json"
    grant_trust(host_repo, store)

    with warnings.catch_warnings():
        warnings.simplefilter("error", UntrustedWorkspaceSkillsWarning)
        contribution = _pack(
            container_ws,
            {
                "workspace_skills_trust": "trust-store",
                "trust_store": store,
                "trust_subject": host_repo,
            },
        )
    assert _digest(contribution, "ws-skill") is not None


def test_trust_store_granted_workspace_loads_without_warning(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _skill_file(ws / ".agents" / "skills", "ws-agents-skill", "WS-AGENTS")
    _skill_file(ws / ".noeta" / "skills", "ws-noeta-skill", "WS-NOETA")
    store = tmp_path / "trust.json"
    grant_trust(ws, store)

    with warnings.catch_warnings():
        warnings.simplefilter("error", UntrustedWorkspaceSkillsWarning)
        contribution = _pack(
            ws,
            {
                "workspace_agents_tier": True,
                "workspace_skills_trust": "trust-store",
                "trust_store": store,
            },
        )
    assert _digest(contribution, "ws-agents-skill") is not None
    assert _digest(contribution, "ws-noeta-skill") is not None


def test_operator_skills_dir_override_survives_untrusted_workspace(
    tmp_path: Path,
) -> None:
    """The override is host config, not repo content: it loads even when the
    workspace is untrusted — and since the override PINS the workspace set,
    nothing repo-derived is at stake and no warning fires."""
    ws = tmp_path / "ws"
    _skill_file(ws / ".agents" / "skills", "ws-agents-skill", "WS-AGENTS")
    operator_dir = tmp_path / "operator-skills"
    _skill_file(operator_dir, "operator-skill", "OPERATOR")

    with warnings.catch_warnings():
        warnings.simplefilter("error", UntrustedWorkspaceSkillsWarning)
        contribution = _pack(
            ws,
            {
                "skills_dir": operator_dir,
                "workspace_agents_tier": True,
                "workspace_skills_trust": "trust-store",
                "trust_store": tmp_path / "trust.json",
            },
        )
    assert _digest(contribution, "operator-skill") is not None
    assert _digest(contribution, "ws-agents-skill") is None


def test_skills_dir_override_pins_out_the_agents_tier(tmp_path: Path) -> None:
    """An operator ``skills_dir`` pins the workspace-scoped skill set: the
    repo's `.agents/skills` does not mount beneath it, even fully trusted —
    a `skills_dir`-pinning host ingests no new repo content from this feature."""
    ws = tmp_path / "ws"
    _skill_file(ws / ".agents" / "skills", "repo-skill", "REPO")
    operator_dir = tmp_path / "operator-skills"
    _skill_file(operator_dir, "operator-skill", "OPERATOR")

    contribution = _pack(
        ws,
        {"skills_dir": operator_dir, "workspace_agents_tier": True},
    )
    assert _digest(contribution, "operator-skill") is not None
    assert _digest(contribution, "repo-skill") is None


# ---------------------------------------------------------------------------
# Identity — frontmatter name, never the directory name
# ---------------------------------------------------------------------------


def test_directory_name_mismatch_indexes_under_frontmatter_name(
    tmp_path: Path,
) -> None:
    """Cross-ecosystem directories are where dir/frontmatter mismatches occur;
    identity stays the frontmatter ``name`` (more permissive than the
    agent-skills standard, same posture as Pi)."""
    ws = tmp_path / "ws"
    write_skill_raw(
        ws / ".noeta" / "skills",
        "some-directory",
        "---\nname: actual-name\ndescription: mismatch fixture\n---\n\nBody.\n",
    )
    contribution = _pack(ws, {})
    assert _digest(contribution, "actual-name") is not None
    assert _digest(contribution, "some-directory") is None
