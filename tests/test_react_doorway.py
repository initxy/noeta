"""The ``react_impl()`` doorway matches the built-in it resolves.

``noeta.client.parts.react_impl`` is a dynamic import: the SDK reaches the
``react`` built-in's ``OrchestrationPolicy`` / ``StructuredOutputPolicy`` /
``WORKFLOW_SYSTEM_PROMPT`` without a static edge into ``noeta.builtins``.
:class:`~noeta.client.parts.ReactImpl` gives that doorway a typed shape so the
three host call sites are checked again — but a Protocol only constrains the
*callers*. Nothing makes the built-in satisfy it, so renaming a constructor
kwarg over there would keep mypy green and break the workflow path at runtime.

This module is the other half: it exercises the doorway exactly as the host
does, so impl and Protocol cannot drift apart silently.
"""

from __future__ import annotations

from typing import Any

from noeta.client.parts import ReactImpl, react_impl
from noeta.protocols.decisions import Decision, FinishDecision
from noeta.protocols.policy import Policy
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View


class _InnerPolicy:
    """A minimal Policy to wrap — the decorator must accept any Policy."""

    def decide(self, ctx: StepContext, view: View) -> Decision:  # noqa: ARG002
        return FinishDecision(answer="inner")


def test_orchestration_policy_takes_the_declared_kwargs() -> None:
    """``react_impl().OrchestrationPolicy(script=..., args=...)`` — host.py's call."""
    policy: Policy = react_impl().OrchestrationPolicy(script="", args={})
    assert hasattr(policy, "decide")


def test_structured_output_policy_takes_the_declared_kwargs() -> None:
    """``react_impl().StructuredOutputPolicy(inner=..., schema=...)`` — host.py's call."""
    schema: dict[str, Any] = {"type": "object"}
    policy: Policy = react_impl().StructuredOutputPolicy(
        inner=_InnerPolicy(), schema=schema
    )
    assert hasattr(policy, "decide")


def test_workflow_system_prompt_is_a_non_empty_string() -> None:
    """The third doorway attribute, read straight into ``build_session_inputs``."""
    assert isinstance(react_impl().WORKFLOW_SYSTEM_PROMPT, str)
    assert react_impl().WORKFLOW_SYSTEM_PROMPT.strip()


def test_the_doorway_is_memoised() -> None:
    """One dynamic import per process — the module is import-stable."""
    assert react_impl() is react_impl()


def test_protocol_names_exactly_what_the_sdk_reaches_for() -> None:
    """The Protocol stays minimal: only what SDK core actually uses.

    A doorway that grows attributes nobody calls turns back into ``Any`` with
    extra steps — the annotations are the enumeration, so pin them.
    """
    declared = {
        name
        for name in vars(ReactImpl)
        if not name.startswith("_")
    } | set(getattr(ReactImpl, "__annotations__", {}))
    assert declared == {
        "OrchestrationPolicy",
        "StructuredOutputPolicy",
        "WORKFLOW_SYSTEM_PROMPT",
    }
