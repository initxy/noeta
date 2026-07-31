"""``AgentSpec.default_model`` is an observational routing hint.

Like :attr:`AgentSpec.metadata` it is host configuration rather than part of
what the model sees, but it is a dataclass field all the same: two specs
differing only in ``default_model`` must compare unequal, or a registry would
treat two differently-routed agents as the same spec.
"""

from __future__ import annotations

from noeta.agent.spec import AgentSpec, ComponentRef


def _spec(*, default_model: str | None = None) -> AgentSpec:
    return AgentSpec(
        name="bug-fixer",
        instructions="Fix the failing test.",
        policy=ComponentRef("react", "2"),
        default_model=default_model,
    )


def test_default_model_participates_in_equality() -> None:
    a = _spec(default_model="claude-opus-4-8")
    b = _spec(default_model="claude-haiku-4-5")
    assert a != b


def test_default_model_defaults_to_none_and_round_trips() -> None:
    assert _spec().default_model is None
    assert _spec(default_model="claude-sonnet-4-5").default_model == "claude-sonnet-4-5"
