"""``AgentSpec.default_model`` is an observational routing hint.

Like :attr:`AgentSpec.metadata`, ``default_model`` is host-config / a model
routing preference. It still participates in dataclass equality and round-trips
cleanly: two specs differing only in ``default_model`` are ``!=``.
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
    """Two specs identical except for ``default_model`` are not ``==`` — the
    field participates in dataclass structural equality."""
    a = _spec(default_model="claude-opus-4-8")
    b = _spec(default_model="claude-haiku-4-5")
    assert a != b


def test_default_model_defaults_to_none_and_round_trips() -> None:
    """Defaults to ``None``; an explicit value is stored verbatim."""
    assert _spec().default_model is None
    assert _spec(default_model="claude-sonnet-4-5").default_model == "claude-sonnet-4-5"
