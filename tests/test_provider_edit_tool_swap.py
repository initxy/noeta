"""Edit-tool uniformity across provider families at the assembly layer.

The old edit↔apply_patch provider mutex is gone: ``apply_patch`` no longer
exists and ``Edit`` is the single precise-replace tool every vendor family
receives. What stays pinned here is the part that must not regress: the
model→family judgment still lives in the providers built-in's catalog and
reaches the kernel pre-resolved (``provider_family=``), switching the model
changes neither the agent definition nor the system prompt, and a read-only
whitelist never grows a mutating tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._session_inputs import default_factory_kwargs
from noeta.client.options import AgentDefinition, Options, compile_options
from noeta.builtins.providers.impl.catalog import provider_family
from noeta.execution.builder import (
    COMPACTION_OFF,
    build_session_inputs,
)
from noeta.runtime.governance import Budget
from noeta.storage.memory import InMemoryContentStore


_FULL_TOOLS = frozenset({"Read", "Glob", "Grep", "Edit", "Write", "shell_run"})


def _tool_names(*, model: str, allowed: frozenset[str] = _FULL_TOOLS) -> set[str]:
    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=Path("/"),  # never written (DRY_RUN default)
        system_prompt="p",
        allowed_tools=allowed,
        content_store=InMemoryContentStore(),
        model=model,
        # The SDK host resolves the family from the catalog and injects it;
        # mirror that wiring here.
        provider_family=provider_family(model),
        compaction=COMPACTION_OFF,
        budget=Budget(),
    )
    return set(inputs.tools)


# ---------------------------------------------------------------------------
# provider_family classification (catalog-membership gated)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,family",
    [
        # Catalogued Anthropic models + friendly alias.
        ("claude-opus-4-8", "anthropic"),
        ("claude-haiku-4-5", "anthropic"),
        ("opus", "anthropic"),
        ("sonnet", "anthropic"),
        ("haiku", "anthropic"),
        # Catalogued OpenAI / GPT models.
        ("gpt-4o", "openai"),
        ("gpt-4o-mini", "openai"),
        ("gpt-5.4-2026-03-05", "openai"),
        # Uncatalogued / sentinel selectors → None (no filtering).
        ("gpt-test", None),
        ("stub-model", None),
        ("test-model", None),
        ("claude-sonnet-4-5", None),  # not in catalog
        ("m", None),
    ],
)
def test_provider_family_classification(model: str, family: str | None) -> None:
    assert provider_family(model) == family


# ---------------------------------------------------------------------------
# one edit tool set for every family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model", ["claude-opus-4-8", "gpt-4o", "sonnet", "gpt-test", "stub-model"]
)
def test_every_family_gets_the_same_edit_tools(model: str) -> None:
    names = _tool_names(model=model)
    assert "Edit" in names
    assert "Write" in names
    assert "apply_patch" not in names


def test_family_swap_leaves_the_tool_set_identical() -> None:
    assert _tool_names(model="claude-opus-4-8") == _tool_names(model="gpt-4o")


def test_readonly_whitelist_never_grows_an_edit_tool() -> None:
    # An explore-style whitelist (Read/Grep/Glob) gains no edit tool on any
    # family — assembly filters only remove, never add.
    readonly = frozenset({"Read", "Glob", "Grep"})
    for model in ("claude-opus-4-8", "gpt-4o", "gpt-test"):
        names = _tool_names(model=model, allowed=readonly)
        assert names == {"Read", "Glob", "Grep"}


# ---------------------------------------------------------------------------
# Switching the model changes nothing else
# ---------------------------------------------------------------------------


def test_model_swap_does_not_touch_agent_definition_or_prompt() -> None:
    opts = Options(
        system_prompt="You are a careful coding assistant.",
        name="main",
        agents={
            "worker": AgentDefinition(
                description="worker",
                prompt="do the task",
                tools=("Read", "Edit", "Write"),
            )
        },
    )
    main, kids = compile_options(opts)
    worker = next(k for k in kids if k.name == "worker")
    whitelist = {r.name for r in worker.tools}
    assert {"Edit", "Write"} <= whitelist

    prompt = "You are a careful coding assistant."
    allowed = frozenset(whitelist)

    anth = _tool_names(model="claude-opus-4-8", allowed=allowed)
    oai = _tool_names(model="gpt-4o", allowed=allowed)
    assert anth == oai

    # Agent identity is model-independent: recompiling yields an equal spec.
    assert worker == next(
        k for k in compile_options(opts)[1] if k.name == "worker"
    )
    # The prompt the assembly layer feeds the composer is the agent's, verbatim,
    # for both families (no per-vendor prompt steering).
    for model in ("claude-opus-4-8", "gpt-4o"):
        inputs = build_session_inputs(
            **default_factory_kwargs(),
            workspace_dir=Path("/"),
            system_prompt=prompt,
            allowed_tools=allowed,
            content_store=InMemoryContentStore(),
            model=model,
            provider_family=provider_family(model),
            compaction=COMPACTION_OFF,
            budget=Budget(),
        )
        assert inputs.composer._system_prompt == prompt
