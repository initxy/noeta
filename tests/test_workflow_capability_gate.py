"""``run_workflow`` capability gating.

``run_workflow`` is a **control-layer orchestration tool**: it goes through
``SpawnSubtaskDecision`` → ``OrchestrationPolicy``, not ToolRuntime (unlike
ordinary ``read`` / ``edit`` tools). Two things gate its availability:

1. ``workflow_enabled`` (host-level kill-switch, ``HostConfig.workflow_enabled``);
2. ``delegation`` (whether this agent may spawn sub-agents).

A workflow's ``agent()`` / ``parallel()`` spawns real sub-agents into the same
``allowed_subtask_agents`` allow-list — so an agent that **cannot delegate**
can't run a workflow even when the host enables it. This test pins down: "only a
delegation-enabled agent gets run_workflow" — of the four (workflow, delegation)
combinations, only both-on exposes the tool.

The description source is pinned too: ``run_workflow``'s description loads from
an independent text resource (``noeta/policies/descriptions/run_workflow.md``,
four sections) covering what / when / when-not / preconditions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.guards.budget import Budget
from noeta.policies.control_tools import (
    RUN_WORKFLOW_TOOL,
    WORKFLOW_AGENT_NAME,
    run_workflow_tool_schema,
)
from noeta.policies.descriptions import load_control_tool_description
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.fs import FsWriteMode, ShellMode


def _build_composer_schemas(
    ws: Path, *, workflow_enabled: bool, delegation_enabled: bool
) -> list[dict[str, Any]]:
    """Call ``build_session_inputs`` and return the composer control schemas.

    Mirrors the production wiring's D3 coupling: the caller passes a
    ``workflow_enabled`` that is already ANDed with delegation (the host
    layers — ``SdkHost._build_engine`` and the noeta-agent session — do that
    AND before they reach the builder).
    """
    content_store = InMemoryContentStore()
    inputs = build_session_inputs(
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read"}),
        content_store=content_store,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        # D3 coupling lives at the host layer; the builder receives the
        # already-ANDed effective flag.
        workflow_enabled=workflow_enabled and delegation_enabled,
        delegation_enabled=delegation_enabled,
        allowed_subtask_agents=(
            frozenset({"explore", WORKFLOW_AGENT_NAME})
            if delegation_enabled
            else frozenset()
        ),
        subtask_agent_directory=(
            (("explore", "read-only explorer"),) if delegation_enabled else ()
        ),
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    return list(inputs.composer._control_action_schemas)


def _has_run_workflow(schemas: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(s, dict)
        and s.get("function", {}).get("name") == RUN_WORKFLOW_TOOL
        for s in schemas
    )


def test_workflow_on_delegation_on_offers_run_workflow(tmp_path: Path) -> None:
    """Both on → run_workflow is exposed (the only combination that does)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    schemas = _build_composer_schemas(
        ws, workflow_enabled=True, delegation_enabled=True
    )
    assert _has_run_workflow(schemas)


def test_workflow_on_delegation_off_hides_run_workflow(tmp_path: Path) -> None:
    """Workflow flag on but the agent can't delegate → no
    run_workflow. A non-delegating agent could never run a workflow (its
    spawns would be blocked), so the tool surface stays honest."""
    ws = tmp_path / "ws"
    ws.mkdir()
    schemas = _build_composer_schemas(
        ws, workflow_enabled=True, delegation_enabled=False
    )
    assert not _has_run_workflow(schemas)


def test_workflow_off_delegation_on_hides_run_workflow(tmp_path: Path) -> None:
    """Delegation on but host kill-switch off → still no run_workflow."""
    ws = tmp_path / "ws"
    ws.mkdir()
    schemas = _build_composer_schemas(
        ws, workflow_enabled=False, delegation_enabled=True
    )
    assert not _has_run_workflow(schemas)


def test_workflow_off_delegation_off_hides_run_workflow(tmp_path: Path) -> None:
    """Both off (the default-safe posture) → no run_workflow."""
    ws = tmp_path / "ws"
    ws.mkdir()
    schemas = _build_composer_schemas(
        ws, workflow_enabled=False, delegation_enabled=False
    )
    assert not _has_run_workflow(schemas)


# ---------------------------------------------------------------------------
# description loaded from an independent four-section resource
# ---------------------------------------------------------------------------


def test_run_workflow_description_from_resource() -> None:
    """The schema's description equals the loaded text resource (not an inline
    Python string), so editing the .md edits the model-facing semantics."""
    schema = run_workflow_tool_schema()
    expected = load_control_tool_description("run_workflow")
    assert schema["function"]["description"] == expected
    # Sanity: it is the actual file content, not an empty/placeholder string.
    assert expected.startswith("Run a short Python orchestration script")


def test_run_workflow_description_has_four_sections() -> None:
    """Every tool description carries the symmetric four sections
    (what / when / when-NOT / preconditions)."""
    text = load_control_tool_description("run_workflow")
    for heading in (
        "## What it does",
        "## When to use",
        "## When NOT to use",
        "## Preconditions",
    ):
        assert heading in text, f"missing section: {heading}"
    # The determinism precondition (the load-bearing hard constraint) survives
    # the migration into the resource file.
    assert "deterministic" in text
