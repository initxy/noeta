"""``run_workflow`` capability gating.

``run_workflow`` is a control-layer orchestration tool — it goes through
``SpawnSubtaskDecision`` → ``OrchestrationPolicy``, not ToolRuntime the way
``read`` / ``edit`` do — and two independent flags gate it: the host kill-switch
(``HostConfig.workflow_allowed``) and whether this agent may delegate. Because a
workflow's ``agent()`` / ``parallel()`` spawns real sub-agents through the same
``allowed_subtask_agents`` allow-list, an agent that cannot delegate could never
run a workflow, so of the four flag combinations only both-on may expose the
tool. The description is pinned to its text resource
(``noeta/builtins/react/impl/run_workflow.md``) so editing that file is what
edits the model-facing semantics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests._session_inputs import default_factory_kwargs
from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.runtime.governance import Budget
from noeta.policies.control_semantics import (
    RUN_WORKFLOW_TOOL,
    WORKFLOW_AGENT_NAME,
)
from noeta.builtins.react.impl import run_workflow_tool_schema
from noeta.protocols.resources import load_markdown
from noeta.storage.memory import InMemoryContentStore
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode


def _build_composer_schemas(
    ws: Path, *, workflow_enabled: bool, delegation_enabled: bool
) -> list[dict[str, Any]]:
    """Call ``build_session_inputs`` and return the composer control schemas.

    Mirrors the production wiring: the caller passes a ``workflow_enabled`` that
    is already ANDed with delegation, because the host layer does that AND
    before it reaches the builder.
    """
    content_store = InMemoryContentStore()
    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read"}),
        content_store=content_store,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        # The coupling lives at the host layer; the builder receives the
        # already-ANDed effective flags.
        capability_flags={
            "workflow": workflow_enabled and delegation_enabled,
            "delegation": delegation_enabled,
        },
        allowed_subtask_agents=(
            frozenset({"explore", WORKFLOW_AGENT_NAME})
            if delegation_enabled
            else frozenset()
        ),
        subtask_agent_directory=(
            (("explore", "read-only explorer"),) if delegation_enabled else ()
        ),
        # The fs write/shell knobs ride plugin_config["fs"].
        plugin_config={
            "fs": {"write_mode": FsWriteMode.DRY_RUN, "shell_mode": ShellMode.OFF},
        },
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
    expected = load_markdown("noeta.builtins.react.impl", "run_workflow")
    assert schema["function"]["description"] == expected
    # Sanity: it is the actual file content, not an empty/placeholder string.
    assert expected.startswith("Run a short Python orchestration script")


def test_run_workflow_description_has_four_sections() -> None:
    """Every tool description carries the symmetric four sections
    (what / when / when-NOT / preconditions)."""
    text = load_markdown("noeta.builtins.react.impl", "run_workflow")
    for heading in (
        "## What it does",
        "## When to use",
        "## When NOT to use",
        "## Preconditions",
    ):
        assert heading in text, f"missing section: {heading}"
    # Determinism is the load-bearing precondition — it must reach the model.
    assert "deterministic" in text
