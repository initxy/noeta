"""The control-tool mount contract: what one control tool contributes to a
session build — a provider-facing ``schema``, a response→neutral-``Decision``
``translate`` closure over its own build inputs, and two separate priorities,
because the schema render order and the decision routing order genuinely differ.

The builder drives every :class:`ControlToolEntry` through one priority-ordered
loop and enumerates no control tool by name; a factory whose tool does not apply
self-gates on the build context and returns ``None``. The routing-spec type the
decision dispatcher consumes lives in ``noeta.policies``, never here, so
``policies`` need not import ``execution``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from noeta.policies.control_semantics import ControlTranslateContext
from noeta.protocols.decisions import Decision


__all__ = [
    "ControlToolBuildContext",
    "ControlToolMount",
    "ControlToolFactory",
    "ControlToolEntry",
    "AskAnswerCodec",
]


@dataclass(frozen=True, slots=True)
class AskAnswerCodec:
    """The ``ask_user_question`` answer codec the driver's ``answer`` path reads.

    The three answer-side functions live in the ``ask_user_question`` built-in
    next to its schema and translate; the built-in fills this struct so the
    kernel can call them WITHOUT a static ``noeta.builtins`` import.

    * ``question_handle(question_id) -> handle`` — the wake handle a
      suspended-on-question task waits on.
    * ``question_id_from_handle(handle) -> question_id | None`` — the inverse,
      so the driver's ``interrupt`` can recognise a question suspend from its
      wake handle (returns ``None`` for any non-question handle).
    * ``load_questions_body(content_store, ref) -> questions`` — decode the
      spilled pending-question body.
    * ``normalize_answer_document(raw, questions) -> answers`` — validate a
      submitted answer body against the pending questions.
    """

    question_handle: Callable[[str], str]
    question_id_from_handle: Callable[[str], Optional[str]]
    load_questions_body: Callable[[Any, Any], list[dict[str, Any]]]
    normalize_answer_document: Callable[
        [Any, list[dict[str, Any]]], dict[str, dict[str, Any]]
    ]


@dataclass(frozen=True, slots=True)
class ControlToolBuildContext:
    """What one session build offers every control-tool mount factory.

    Assembled after the tools, because every field here is a function of session
    state that only exists once the session packs have run. Frozen so no factory
    can perturb a later factory's inputs.

    ``capability_flags`` carries the already-ANDed effective values (agent
    activation × host kill-switch) by capability name; this stage does not
    re-derive them. It is the same generic bag session packs read, so a
    third-party control tool gates on its own activation name with no kernel
    signature change.
    """

    #: The effective capability flags by name — the generic bag a mount factory
    #: self-gates on via :meth:`flag`.
    capability_flags: Mapping[str, bool]
    #: The sorted ``(name, description)`` spawn directory the ``spawn_subagent``
    #: schema renders into its ``agent`` enum.
    subtask_agent_directory: tuple[tuple[str, str], ...]
    #: The per-helper structured-output JSON Schema; its gate is data-driven
    #: rather than an activation.
    structured_output_schema: Optional[dict[str, Any]]

    def flag(self, name: str) -> bool:
        """The effective flag for capability ``name`` (absent ⇒ ``False``)."""
        return bool(self.capability_flags.get(name, False))


@dataclass(frozen=True, slots=True)
class ControlToolMount:
    """One control tool's materialized contribution to a session build.

    ``schema`` is the provider-facing dict the composer appends to
    ``View.provider_tool_schemas``, so it folds into the stable-prefix hash.
    ``translate`` closes over this mount's own build inputs, which is what keeps
    feature-named fields out of the decision-time ``ControlTranslateContext``;
    it is ``None`` for a tool intercepted outside the dispatch loop, and such a
    mount contributes a schema but no routing spec.
    """

    name: str
    schema: dict[str, Any]
    translate: Optional[Callable[[ControlTranslateContext], Optional[Decision]]]
    routing_priority: int
    schema_priority: int
    #: Filled only by the ``ask_user_question`` mount, so the driver's ``answer``
    #: path can decode a submitted reply without importing the built-in.
    answer_codec: Optional[AskAnswerCodec] = None


#: Read the build context, self-gate, and return a mount or ``None``.
ControlToolFactory = Callable[[ControlToolBuildContext], Optional[ControlToolMount]]


@dataclass(frozen=True, slots=True)
class ControlToolEntry:
    """One resolved control tool in the builder's loop.

    The output byte orders come from each mount's own ``schema_priority`` /
    ``routing_priority``, so ``priority`` here is not load-bearing: it only fixes
    a stable, sorted iteration for the collision check, with ``name`` as the
    collision key and tie-breaker.
    """

    name: str
    priority: int
    factory: ControlToolFactory
