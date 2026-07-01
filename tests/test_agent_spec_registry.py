"""Generic ``AgentSpec`` / ``AgentRegistry`` identity.

``AgentSpec`` identity is plain frozen-dataclass structural equality (``==``):
deterministic across constructions, order-independent (component lists normalise
to sorted tuples), and sensitive to every behaviour-bearing field. The
verify-era fingerprint digest was retired with verify/replay.
"""

from __future__ import annotations

import pytest

from noeta.agent.registry import AgentRegistry, UnknownAgentError
from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    Capabilities,
    ComponentRef,
    ToolRef,
)


def _bug_fixer(*, metadata: dict[str, str] | None = None) -> AgentSpec:
    return AgentSpec(
        name="bug-fixer",
        instructions="Fix the failing test.",
        policy=ComponentRef("react", "2"),
        composer=ComponentRef("three_segment", "2"),
        tools=(
            ToolRef("read_file"),
            ToolRef("shell_run", risk_level="high"),
        ),
        skills=(ComponentRef("fix-python-test"),),
        default_budget=BudgetSpec(max_iterations=20),
        metadata=metadata if metadata is not None else {"owner": "noeta-code"},
    )


def test_identity_is_deterministic_across_constructions() -> None:
    assert _bug_fixer() == _bug_fixer()


def test_identity_is_order_independent() -> None:
    reordered = AgentSpec(
        name="bug-fixer",
        instructions="Fix the failing test.",
        policy=ComponentRef("react", "2"),
        composer=ComponentRef("three_segment", "2"),
        # tools listed in the opposite order
        tools=(
            ToolRef("shell_run", risk_level="high"),
            ToolRef("read_file"),
        ),
        skills=(ComponentRef("fix-python-test"),),
        default_budget=BudgetSpec(max_iterations=20),
        metadata={"owner": "noeta-code"},
    )
    # normalisation makes the stored tuple canonical, so reordering tools does
    # not change identity.
    assert reordered.tools == _bug_fixer().tools
    assert reordered == _bug_fixer()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: AgentSpec(**{**_kw(s), "name": "other-name"}),
        lambda s: AgentSpec(**{**_kw(s), "instructions": "Different prompt."}),
        lambda s: AgentSpec(**{**_kw(s), "policy": ComponentRef("react", "3")}),
        lambda s: AgentSpec(**{**_kw(s), "composer": ComponentRef("three_segment", "3")}),
        lambda s: AgentSpec(**{**_kw(s), "tools": (ToolRef("read_file"),)}),
        lambda s: AgentSpec(**{**_kw(s), "skills": ()}),
        lambda s: AgentSpec(**{**_kw(s), "guards": (ComponentRef("permission"),)}),
        lambda s: AgentSpec(**{**_kw(s), "observers": (ComponentRef("audit"),)}),
        lambda s: AgentSpec(**{**_kw(s), "default_budget": BudgetSpec(max_iterations=99)}),
        lambda s: AgentSpec(**{**_kw(s), "capabilities": Capabilities(todo_write=True)}),
    ],
)
def test_every_identity_field_changes_identity(mutate) -> None:
    base = _bug_fixer()
    # Rebuild the unmutated spec the same way `mutate` does (via _kw, which
    # carries no metadata) so the only difference is the mutated field.
    unmutated = AgentSpec(**_kw(base))
    assert mutate(base) != unmutated


def _kw(s: AgentSpec) -> dict:
    return {
        "name": s.name,
        "instructions": s.instructions,
        "policy": s.policy,
        "composer": s.composer,
        "tools": s.tools,
        "skills": s.skills,
        "guards": s.guards,
        "observers": s.observers,
        "default_budget": s.default_budget,
        "capabilities": s.capabilities,
    }


def test_toolref_risk_level_is_identity_bearing() -> None:
    """``risk_level`` gates approval, so it is part of identity: two specs whose
    only difference is a tool's risk_level are not ``==``."""
    base = AgentSpec(
        name="a", instructions="x", policy=ComponentRef("react"),
        tools=(ToolRef("t", risk_level="low"),),
    )
    changed = AgentSpec(
        name="a", instructions="x", policy=ComponentRef("react"),
        tools=(ToolRef("t", risk_level="high"),),
    )
    assert base != changed


# --- registry ---


def test_registry_add_resolve_roundtrip() -> None:
    reg = AgentRegistry()
    spec = _bug_fixer()
    reg.add(spec)
    assert reg.resolve("bug-fixer") is spec
    assert "bug-fixer" in reg
    assert reg.names() == ["bug-fixer"]


def test_registry_rejects_duplicate_names() -> None:
    reg = AgentRegistry()
    reg.add(_bug_fixer())
    with pytest.raises(ValueError, match="already registered"):
        reg.add(_bug_fixer())


def test_registry_unknown_is_hard_error() -> None:
    reg = AgentRegistry()
    reg.add(_bug_fixer())
    with pytest.raises(UnknownAgentError) as ei:
        reg.resolve("does-not-exist")
    assert ei.value.agent_name == "does-not-exist"
    assert ei.value.available == ["bug-fixer"]
    assert ei.value.task_id is None


def test_registry_membership_is_insertion_order_independent() -> None:
    a = AgentSpec(name="a", instructions="x", policy=ComponentRef("react"))
    b = AgentSpec(name="b", instructions="y", policy=ComponentRef("react"))
    r1 = AgentRegistry(); r1.add(a); r1.add(b)
    r2 = AgentRegistry(); r2.add(b); r2.add(a)
    # Same membership regardless of insertion order: resolved specs are equal.
    assert sorted(r1.names()) == sorted(r2.names())
    for name in r1.names():
        assert r1.resolve(name) == r2.resolve(name)


def test_registry_member_drift_is_observable() -> None:
    r1 = AgentRegistry(); r1.add(_bug_fixer())
    drifted = AgentSpec(
        name="bug-fixer", instructions="DRIFTED", policy=ComponentRef("react", "2"),
    )
    r2 = AgentRegistry(); r2.add(drifted)
    # A member behaviour change is a real identity change on the resolved spec.
    assert r1.resolve("bug-fixer") != r2.resolve("bug-fixer")
