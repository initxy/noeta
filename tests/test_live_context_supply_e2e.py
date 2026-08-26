"""Three real-LLM E2E loops (live marker).

All three loops run through the production SDK assembly (``SdkHost`` +
``InteractionDriver`` + presets main — the real shipping product path):

1. **Skill invocation (generic shape)** — the real model invokes via the `skill`
   control tool; the log shows ``ContextContentRecorded`` (kind=skill,
   policy=pinned), never a bespoke ``SkillContentRecorded``; the skill body
   lands in semi_stable.
2. **Memory write** — the real model calls the plain ``memory_write`` tool; the
   memory file lands on disk.
3. **Auto-recall (origin=memory)** — with a pre-seeded memory, a goal matching
   the memory name injects a recall message recorded with ``origin="memory"`` and
   a resident index (kind=memory, policy=evolving); the model uses the recalled
   content in its reply.

Config comes from a git-ignored ``.env`` via ``tests._live_env`` (copy
``.env.example`` and fill in ``NOETA_LIVE_BASE_URL`` / ``NOETA_LIVE_API_KEY`` /
``NOETA_LIVE_MODEL``)::

    uv run pytest -m live tests/test_live_context_supply_e2e.py

Auto-skips when config is missing (CI does not run it by default).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.core.fold import fold

from tests import _live_env

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Provider from the shared .env loader (Anthropic-shape gateway)
# ---------------------------------------------------------------------------


def _model() -> str:
    return _live_env.live_model() or ""


requires_live_llm = _live_env.requires_live


# ---------------------------------------------------------------------------
# Product-session helpers
# ---------------------------------------------------------------------------


def _session(workspace: Path, *, max_steps: int = 8):
    from noeta.runtime.shell_policy import ShellMode
    from noeta.runtime.workspace import FsWriteMode

    from tests._sdk_session import (
        make_driver,
        make_host,
        make_registry,
        runner_main_spec,
    )

    provider = _live_env.build_anthropic_provider()
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=workspace,
        provider=provider,
        model=_model(),
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        max_steps=max_steps,
    )
    return host, make_driver(host)


def _write_skill(ws: Path, name: str, body: str, description: str) -> None:
    skill_dir = ws / ".noeta" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Loop 1 — skill invocation via the generic shape
# ---------------------------------------------------------------------------


@requires_live_llm
def test_live_skill_invocation_generic_shape(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_skill(
        ws,
        "release-checklist",
        "Release checklist: 1. tag the commit 2. build 3. publish.",
        "the team's release checklist",
    )
    host, driver = _session(ws)
    out = driver.start(
        goal=(
            "Call the `skill` tool with skill='release-checklist' to load the "
            "release checklist skill. After it loads, finish immediately by "
            "replying exactly: loaded."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    types = [e.type for e in events]
    # Skill activation records the generic shape: a
    # ContextContentRecorded(kind=skill), never a bespoke SkillContentRecorded.
    assert "SkillContentRecorded" not in types
    skill_events = [
        e for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == "skill"
    ]
    assert skill_events, f"model never ordered the skill; types={types}"
    assert skill_events[0].payload.name == "release-checklist"
    assert skill_events[0].payload.policy == "pinned"

    folded = fold(host.event_log, host.content_store, out.task_id)
    assert "release-checklist" in folded.state.active_skills
    # active_content[kind] is the generic activation map name -> content_hash;
    # assert on the name set, the hash is content-dependent.
    assert tuple(folded.state.active_content.get("skill", {})) == (
        "release-checklist",
    )
    # Skill body is supplied to the model. A mid-loop activation (the model
    # ordered the skill after it had already started talking) anchors the body
    # into the rolling history — it lands in dynamic_suffix, not semi_stable
    # (anchored-content-placement). Assert on the whole composed view so the
    # test tracks "the body reached the model", not a fixed segment.
    engine = host.resolve_engine_for_agent("main", model=_model())
    view = engine._composer.compose(folded)
    joined = "\n".join(
        b.text
        for seg in view.segments
        for m in seg.content
        for b in m.content
        if hasattr(b, "text")
    )
    assert "tag the commit" in joined


# ---------------------------------------------------------------------------
# Loop 2 — memory write (plain tool)
# ---------------------------------------------------------------------------


@requires_live_llm
def test_live_memory_write_tool(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _session(ws)
    out = driver.start(
        goal=(
            "Use the memory_write tool to store a memory named 'team-greeting' "
            "with the text '# Greeting\\nAlways say ahoy.' Then finish by "
            "replying exactly: stored."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    # Memory writes land in the global memory dir (conftest pins it to a
    # per-test tmp).
    from noeta.builtins.memory.impl.store import DEFAULT_GLOBAL_MEMORY_DIR

    written = Path(DEFAULT_GLOBAL_MEMORY_DIR) / "team-greeting.md"
    assert written.is_file(), "model never wrote the memory file"
    assert "ahoy" in written.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------------------
# Loop 3 — auto-recall (origin=memory)
# ---------------------------------------------------------------------------


# Auto-recall is the ``driver.seed_start`` recall seam: the memory
# ``reminder_provider`` runs before the goal enters the ledger, so a goal
# matching a memory name activates the body as a ``memory``-kind resident —
# a second ``ContextContentRecorded(kind=memory, policy=evolving)`` next to
# the index's — rendered to the model as a host-injected message.
@requires_live_llm
def test_live_memory_recall_origin(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # Memory is pinned to the global directory (conftest pins it to a
    # per-test tmp).
    from noeta.builtins.memory.impl.store import DEFAULT_GLOBAL_MEMORY_DIR

    mem = Path(DEFAULT_GLOBAL_MEMORY_DIR)
    mem.mkdir(parents=True, exist_ok=True)
    memory_file = mem / "deploy-runbook.md"
    memory_file.write_text(
        "# Deploy runbook\nThe magic deploy word is zanzibar.\n",
        encoding="utf-8",
    )
    host, driver = _session(ws)
    out = driver.start(
        goal=(
            "According to the deploy-runbook in your recalled memories, what is "
            "the magic deploy word? Reply with just that word and finish."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    # Resident index: kind=memory, policy evolving.
    idx = [
        e for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == "memory"
    ]
    assert [e.payload.name for e in idx] == ["index", "deploy-runbook"]
    assert {e.payload.policy for e in idx} == {"evolving"}

    folded = fold(host.event_log, host.content_store, out.task_id)
    assert "deploy-runbook" in folded.state.active_content["memory"], (
        "a goal matching the memory name must activate the body resident"
    )
    assert not [m for m in folded.runtime.messages if m.origin == "memory"]
    # The model saw the body: its reply answers from it.
    assistant_texts = [
        "".join(b.text for b in m.content if hasattr(b, "text"))
        for m in folded.runtime.messages
        if m.role == "assistant"
    ]
    assert assistant_texts and "zanzibar" in assistant_texts[-1].lower()
