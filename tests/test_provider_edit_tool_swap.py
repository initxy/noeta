"""Provider-mutex edit tool selection at the assembly layer.

The edit↔apply_patch difference is absorbed in ``noeta.execution.builder`` by the
bound model's vendor family — NOT in any tool field, NOT in the prompt, NOT in
the AgentSpec whitelist. An Anthropic model's live tool set carries ``edit``
(no ``apply_patch``); an OpenAI/GPT model's carries ``apply_patch`` (no
``edit``); an unrecognised test/stub model keeps both (byte-equal legacy).

Microkernel M2: the model→family judgment lives in the providers built-in's
catalog and reaches the kernel pre-resolved (``provider_family=`` — mirrored
here by ``_tool_names``); phase 2c: the FAMILY→drop-set table is the fs
built-in's ``PROVIDER_EDIT_TOOL_MUTEX``, injected as ``edit_tool_mutex=``.

Switching the model must change only the tool set, never the agent definition
or the system prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._session_inputs import default_factory_kwargs
from noeta.client.options import AgentDefinition, Options, compile_options
from noeta.builtins.fs.impl import PROVIDER_EDIT_TOOL_MUTEX
from noeta.builtins.providers.impl.catalog import provider_family
from noeta.execution.builder import (
    COMPACTION_OFF,
    build_session_inputs,
)
from noeta.runtime.governance import Budget
from noeta.storage.memory import InMemoryContentStore


# A whitelist carrying BOTH edit candidates (edit + apply_patch) so the
# assembly-layer mutex actually has both to choose between. main carries the
# full built-in set (incl. apply_patch); general-purpose was narrowed to
# read/write/edit + shell (no apply_patch), so this fixture
# is the catch-all main shape, not gp's.
_FULL_EDIT_TOOLS = frozenset(
    {"read", "glob", "grep", "edit", "write", "apply_patch", "shell_run"}
)


def _tool_names(*, model: str, allowed: frozenset[str] = _FULL_EDIT_TOOLS) -> set[str]:
    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=Path("/"),  # never written (DRY_RUN default)
        system_prompt="p",
        allowed_tools=allowed,
        content_store=InMemoryContentStore(),
        model=model,
        # The SDK host resolves the family from the catalog and injects it
        # (microkernel M2) — mirror that wiring here.
        provider_family=provider_family(model),
        edit_tool_mutex=PROVIDER_EDIT_TOOL_MUTEX,
        compaction=COMPACTION_OFF,
        budget=Budget(),
    )
    return set(inputs.tools)


# ---------------------------------------------------------------------------
# 1. provider_family classification (catalog-membership gated)
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
# 2. PROVIDER_EDIT_TOOL_MUTEX — which name(s) to DROP (fs builtin's table,
#    phase 2c: the kernel builder consumes it only as an injection)
# ---------------------------------------------------------------------------


def test_mutex_drops_apply_patch_for_anthropic() -> None:
    assert PROVIDER_EDIT_TOOL_MUTEX["anthropic"] == ("apply_patch",)
    assert PROVIDER_EDIT_TOOL_MUTEX[provider_family("opus")] == ("apply_patch",)


def test_mutex_drops_edit_for_openai() -> None:
    assert PROVIDER_EDIT_TOOL_MUTEX["openai"] == ("edit",)
    assert PROVIDER_EDIT_TOOL_MUTEX[
        provider_family("gpt-5.4-2026-03-05")
    ] == ("edit",)


def test_mutex_knows_nothing_for_unknown_family() -> None:
    # ``None`` family = uncatalogued model / kernel-alone build; the table
    # only knows the two families, and the builder skips the drop entirely
    # for a ``None`` family.
    for model in ("gpt-test", "stub-model", "test-model", "claude-sonnet-4-5"):
        assert provider_family(model) not in PROVIDER_EDIT_TOOL_MUTEX
    assert set(PROVIDER_EDIT_TOOL_MUTEX) == {"anthropic", "openai"}


# ---------------------------------------------------------------------------
# 3. Assembly layer — the live tool set is provider-mutex
# ---------------------------------------------------------------------------


def test_anthropic_model_tool_set_has_edit_not_apply_patch() -> None:
    names = _tool_names(model="claude-opus-4-8")
    assert "edit" in names
    assert "apply_patch" not in names


def test_openai_model_tool_set_has_apply_patch_not_edit() -> None:
    names = _tool_names(model="gpt-4o")
    assert "apply_patch" in names
    assert "edit" not in names


def test_alias_resolves_to_anthropic_edit() -> None:
    names = _tool_names(model="sonnet")
    assert "edit" in names
    assert "apply_patch" not in names


def test_unknown_model_keeps_both_edit_variants() -> None:
    # The test/stub sentinel path must keep BOTH so existing recordings and
    # the apply_patch session tests (which run on ``gpt-test``) stay green.
    names = _tool_names(model="gpt-test")
    assert "edit" in names
    assert "apply_patch" in names


def test_mutex_filter_only_touches_edit_pair() -> None:
    # The read-only + write + shell tools are unaffected by the family swap.
    a = _tool_names(model="claude-opus-4-8")
    o = _tool_names(model="gpt-4o")
    unaffected = {"read", "glob", "grep", "write", "shell_run"}
    assert unaffected <= a
    assert unaffected <= o


def test_readonly_whitelist_never_grows_an_edit_tool() -> None:
    # An explore-style whitelist (read/grep/glob) gains no edit tool from the
    # filter on EITHER family — the mutex only removes, never adds.
    readonly = frozenset({"read", "glob", "grep"})
    for model in ("claude-opus-4-8", "gpt-4o", "gpt-test"):
        names = _tool_names(model=model, allowed=readonly)
        assert names == {"read", "glob", "grep"}


# ---------------------------------------------------------------------------
# 4. Switching the model changes the tool set but NOT the agent / prompt
# ---------------------------------------------------------------------------


def test_model_swap_does_not_touch_agent_definition_or_prompt() -> None:
    # One agent definition with both edit tools whitelisted; compile it once.
    opts = Options(
        system_prompt="You are a careful coding assistant.",
        name="main",
        agents={
            "worker": AgentDefinition(
                description="worker",
                prompt="do the task",
                tools=("read", "edit", "apply_patch"),
            )
        },
    )
    main, kids = compile_options(opts)
    worker = next(k for k in kids if k.name == "worker")
    # The compiled AgentSpec whitelist carries BOTH edit tools regardless of
    # any model — provider selection happens at assembly, not compile.
    whitelist = {r.name for r in worker.tools}
    assert {"edit", "apply_patch"} <= whitelist

    prompt = "You are a careful coding assistant."
    allowed = frozenset(whitelist)

    anth = _tool_names(model="claude-opus-4-8", allowed=allowed)
    oai = _tool_names(model="gpt-4o", allowed=allowed)

    # Same agent, same prompt, same whitelist — only the live tool set differs.
    assert "edit" in anth and "apply_patch" not in anth
    assert "apply_patch" in oai and "edit" not in oai
    # Agent identity is model-independent (provider selection happens at
    # assembly, not compile): recompiling yields an equal worker spec.
    assert worker == next(
        k for k in compile_options(opts)[1] if k.name == "worker"
    )
    # The prompt the assembly layer feeds the composer is the agent's, verbatim,
    # for both families (no "if you are GPT use apply_patch" prompt steering).
    for model in ("claude-opus-4-8", "gpt-4o"):
        inputs = build_session_inputs(
            **default_factory_kwargs(),
            workspace_dir=Path("/"),
            system_prompt=prompt,
            allowed_tools=allowed,
            content_store=InMemoryContentStore(),
            model=model,
            provider_family=provider_family(model),
            edit_tool_mutex=PROVIDER_EDIT_TOOL_MUTEX,
            compaction=COMPACTION_OFF,
            budget=Budget(),
        )
        assert inputs.composer._system_prompt == prompt


# ---------------------------------------------------------------------------
# 5. apply_patch description lives in an independent text resource
# ---------------------------------------------------------------------------


def test_apply_patch_description_loads_from_resource() -> None:
    from noeta.protocols.resources import load_markdown
    from noeta.builtins.fs.impl import ApplyPatchTool

    assert ApplyPatchTool.description == load_markdown(
        "noeta.builtins.fs.impl", "apply_patch"
    )


def test_apply_patch_description_is_cc_short_form() -> None:
    # the four-section template was dropped for Claude Code's terse
    # short-form (a one-line summary + bullets).
    from noeta.protocols.resources import load_markdown

    text = load_markdown("noeta.builtins.fs.impl", "apply_patch")
    first_line = text.strip().splitlines()[0]
    assert first_line and not first_line.startswith("#")
    assert "## What it does" not in text
