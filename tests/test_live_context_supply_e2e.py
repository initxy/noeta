"""Three real-LLM E2E loops (live marker).

All three loops run through the production SDK assembly (``SdkHost`` +
``InteractionDriver`` + presets main — the real shipping product path):

1. **Skill invocation (generic shape)** — the real model invokes via the `skill`
   control tool; the log shows ``ContextContentRecorded`` (kind=skill,
   policy=pinned) and no longer the old ``SkillContentRecorded``; the skill body
   lands in semi_stable.
2. **Memory write** — the real model calls the plain ``memory_write`` tool; the
   memory file lands on disk.
3. **Auto-recall (origin=memory)** — with a pre-seeded memory, a goal matching
   the memory name injects a recall message recorded with ``origin="memory"`` and
   a resident index (kind=memory, policy=evolving); the model uses the recalled
   content in its reply.

Run (same env vars as examples/real_provider_subtask_demo.py)::

    # OpenAI-compatible:
    NOETA_OPENAI_BASE_URL=... NOETA_OPENAI_API_KEY=... NOETA_OPENAI_MODEL=... \
        uv run pytest -m live tests/test_live_context_supply_e2e.py
    # Anthropic:
    NOETA_PROVIDER=anthropic NOETA_API_KEY=... NOETA_MODEL=claude-... \
        uv run pytest -m live tests/test_live_context_supply_e2e.py

Auto-skips when the key is missing (CI does not run it by default).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import pytest

from noeta.core.fold import fold

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Provider from env — same contract as examples/real_provider_subtask_demo.py
# ---------------------------------------------------------------------------


def _build_provider() -> Optional[Any]:
    provider_kind = os.environ.get("NOETA_PROVIDER", "openai")
    if provider_kind == "openai":
        required = (
            "NOETA_OPENAI_BASE_URL",
            "NOETA_OPENAI_API_KEY",
            "NOETA_OPENAI_MODEL",
        )
        if any(os.environ.get(v) is None for v in required):
            return None
        from noeta.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            base_url=os.environ["NOETA_OPENAI_BASE_URL"],
            api_key=os.environ["NOETA_OPENAI_API_KEY"],
        )
    if provider_kind == "anthropic":
        if os.environ.get("NOETA_API_KEY") is None:
            return None
        from noeta.providers.anthropic import AnthropicProvider

        max_tokens_str = os.environ.get("NOETA_MAX_TOKENS")
        return AnthropicProvider(
            api_key=os.environ["NOETA_API_KEY"],
            default_max_tokens=int(max_tokens_str) if max_tokens_str else 1024,
        )
    return None


def _model() -> str:
    return (
        os.environ.get("NOETA_OPENAI_MODEL")
        or os.environ.get("NOETA_MODEL")
        or "gpt-4o-mini"
    )


_HAS_PROVIDER = _build_provider() is not None

requires_live_llm = pytest.mark.skipif(
    not _HAS_PROVIDER,
    reason=(
        "real-LLM E2E needs provider env (NOETA_OPENAI_BASE_URL/"
        "NOETA_OPENAI_API_KEY/NOETA_OPENAI_MODEL or NOETA_PROVIDER=anthropic"
        "+NOETA_API_KEY)"
    ),
)


# ---------------------------------------------------------------------------
# Product-session helpers
# ---------------------------------------------------------------------------


def _session(workspace: Path, *, max_steps: int = 8):
    from noeta.tools.fs import FsWriteMode, ShellMode

    from tests._sdk_session import (
        make_driver,
        make_host,
        make_registry,
        runner_main_spec,
    )

    provider = _build_provider()
    assert provider is not None
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
    # New recordings always use the generic shape: a
    # ContextContentRecorded(kind=skill), no old event.
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
    assert folded.state.active_content.get("skill") == (
        "release-checklist",
    )
    # Skill body lands in semi_stable.
    engine = host.resolve_engine_for_agent("main", model=_model())
    view = engine._composer.compose(folded)
    semi = next(s for s in view.segments if s.name == "semi_stable")
    joined = "\n".join(
        b.text
        for m in semi.content
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
    # Memory writes land in the
    # global memory dir (conftest pins it to a per-test tmp), not ws/.noeta/memories.
    from noeta.execution.memory import DEFAULT_GLOBAL_MEMORY_DIR

    written = Path(DEFAULT_GLOBAL_MEMORY_DIR) / "team-greeting.md"
    assert written.is_file(), "model never wrote the memory file"
    assert "ahoy" in written.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------------------
# Loop 3 — auto-recall (origin=memory)
# ---------------------------------------------------------------------------


# (T8/③-B resolved): auto-recall (origin=memory injected message + kind=memory
# index event) is the ``driver.seed_start`` recall seam
# (``append_user_message_with_recall`` + ``record_memory_index``, resolved via
# ``SdkHost.memory_recall_context``) — the SDK port of the deleted noeta-agent
# runner's prepare-time wiring.
@requires_live_llm
def test_live_memory_recall_origin(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # Memory is pinned to the global directory (conftest pins it to a
    # per-test tmp), not the runner-era ``ws/.noeta/memories``.
    from noeta.execution.memory import DEFAULT_GLOBAL_MEMORY_DIR

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
    assert len(idx) == 1
    assert idx[0].payload.policy == "evolving"

    folded = fold(host.event_log, host.content_store, out.task_id)
    recall_msgs = [
        m for m in folded.runtime.messages if m.origin == "memory"
    ]
    assert recall_msgs, "a goal matching the memory name must inject an origin=memory recall"
    joined = "".join(
        b.text for b in recall_msgs[0].content if hasattr(b, "text")
    )
    assert "zanzibar" in joined
