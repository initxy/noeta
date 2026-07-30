"""The control-tool mount contract — the construction half of a control tool
(control-tool-surface spec, 2026-07-30, stage S1).

A *control-tool mount* is what one control tool contributes to a session build:
a materialized provider-facing ``schema``, a response→neutral-Decision
``translate`` closure (over its own build inputs), and TWO explicit priorities —
one for the schema render order, one for the decision routing order. The builder
runs every :class:`ControlToolEntry` through ONE priority-ordered loop
(:func:`~noeta.execution.builder._run_control_tool_mounts`), mirroring the
session-pack loop; it enumerates no control tool by name.

Two orders, both from the mount's own bands (they genuinely differ — the S0
golden pins ``spawn`` first in the schema list while ``ask`` routes first): the
schema list sorts on ``schema_priority``, the routing specs on
``routing_priority``. A tool that intercepts elsewhere (``structured_output`` →
react's ``StructuredOutputPolicy``) carries ``translate=None`` and is excluded
from the routing order.

Gating is the mount's own business: a factory self-gates on the
:class:`ControlToolBuildContext` and returns ``None`` when its tool does not
apply — the kernel never gates for a mount, exactly as a session pack never
gates for a tool. ``ControlToolBuildContext`` is control-tool-specific by design
(the generic-slots red line protects :class:`~noeta.execution.session_pack.SessionBuildContext`,
not this type).

Layering (import-linter): this module sits in ``noeta.execution`` and reads only
DOWN the stack — the decision-time ``ControlTranslateContext`` from
``noeta.policies`` (the ``execution → policies`` edge the builder already has)
and ``Decision`` from ``noeta.protocols``. The routing-spec TYPE the decision
dispatcher consumes (``ControlToolSpec``) lives in ``noeta.policies``, never
here, so ``policies`` need not import ``execution``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from noeta.policies.control_semantics import ControlTranslateContext
from noeta.protocols.decisions import Decision


__all__ = [
    "ControlToolBuildContext",
    "ControlToolMount",
    "ControlToolFactory",
    "ControlToolEntry",
    "AskAnswerCodec",
    "CONTROL_EXPORT_ASK_ANSWER_CODEC",
]


#: The one admitted control-tool mount-export key (D8). A mount may publish
#: named values under :attr:`ControlToolMount.exports`; the builder collects them
#: into one mapping the host threads to the kernel seams that consume them —
#: mirroring ``PackContribution.exports``. This vocabulary is CLOSED by the same
#: admission rule: a key exists here ONLY because a kernel seam reads it. This
#: one carries the ``ask_user_question`` answer codec (:class:`AskAnswerCodec`)
#: from its built-in mount to the driver's ``answer`` path — the driver can no
#: longer import the codec statically (it moved into the ``ask_user_question``
#: built-in, and the kernel never imports ``noeta.builtins``). A session that
#: never mounts ``ask_user_question`` carries no such key, and an answer arriving
#: for it fails loudly.
CONTROL_EXPORT_ASK_ANSWER_CODEC = "ask_answer_codec"


@dataclass(frozen=True, slots=True)
class AskAnswerCodec:
    """The ``ask_user_question`` answer codec the driver's ``answer`` path reads.

    A kernel-side kit type (the ``EnvironmentKit`` / ``InstructionsKit`` pattern):
    the three answer-side functions live in the ``ask_user_question`` built-in
    (schema + translate + codec are collocated there), and the built-in fills
    this struct so the kernel driver can call them WITHOUT a static
    ``noeta.builtins`` import. Delivered to the driver as the
    :data:`CONTROL_EXPORT_ASK_ANSWER_CODEC` mount export (D8).

    * ``question_handle(question_id) -> handle`` — the ``question-<id>`` wake
      handle a suspended-on-question task waits on.
    * ``load_questions_body(content_store, ref) -> questions`` — decode the
      spilled pending-question body.
    * ``normalize_answer_document(raw, questions) -> answers`` — validate a
      submitted answer body against the pending questions.
    """

    question_handle: Callable[[str], str]
    load_questions_body: Callable[[Any, Any], list[dict[str, Any]]]
    normalize_answer_document: Callable[
        [Any, list[dict[str, Any]]], dict[str, dict[str, Any]]
    ]


@dataclass(frozen=True, slots=True)
class ControlToolBuildContext:
    """What one session build offers every control-tool mount factory.

    Built by :func:`~noeta.execution.builder.build_session_inputs` before the
    mount loop, AFTER tool assembly (the schemas are session-state functions —
    the spawn directory, the per-helper structured-output schema, the packs'
    exports all exist only once the packs have run). Frozen so a factory can
    never perturb a later factory's inputs. A factory that finds its tool
    inapplicable returns ``None`` — self-gating is the factory's own check
    against this context, never a kernel ``if``.

    ``capability_flags`` carries the already-ANDed effective values (agent
    activation × host kill-switch) by capability name, straight off the build
    spec — this stage does not re-derive them (that is the resolver's job). It
    is the same generic bag session packs read
    (:attr:`~noeta.execution.session_pack.SessionBuildContext.capability_flags`),
    so a third-party control tool gates on its own activation name with no
    kernel signature change, and the kernel enumerates no control tool's flag.
    """

    #: The effective capability flags by name (from the build spec) — the
    #: generic bag a mount factory self-gates on via :meth:`flag`.
    capability_flags: Mapping[str, bool]
    #: The sorted ``(name, description)`` spawn directory the ``spawn_subagent``
    #: schema renders into its ``agent`` enum + roster.
    subtask_agent_directory: tuple[tuple[str, str], ...]
    #: The per-helper structured-output JSON Schema, or ``None`` (its gate is
    #: data-driven, not an activation).
    structured_output_schema: Optional[dict[str, Any]]
    #: The session packs' named exports bag (the closed pack vocabulary) — the
    #: doorway a mount reads its own pack's side-state through (the skills
    #: mount derives its menu from ``EXPORT_SKILLS_KIT`` here).
    exports: Mapping[str, object]

    def flag(self, name: str) -> bool:
        """The effective flag for capability ``name`` (absent ⇒ ``False``)."""
        return bool(self.capability_flags.get(name, False))


@dataclass(frozen=True, slots=True)
class ControlToolMount:
    """One control tool's materialized contribution to a session build.

    ``schema`` is the provider-facing dict the composer appends to
    ``View.provider_tool_schemas`` (folded into the stable-prefix hash).
    ``translate`` is the response→neutral-Decision closure over this mount's own
    build inputs (so the skill mount carries its menu, and the decision-time
    ``ControlTranslateContext`` needs no feature-named field). ``translate`` is
    ``None`` for a tool intercepted outside the dispatch loop
    (``structured_output``); such a mount contributes a schema but no routing
    spec. The two priorities pin the two orders — ``schema_priority`` the render
    order (S0 golden), ``routing_priority`` the decision dispatch order.
    """

    name: str
    schema: dict[str, Any]
    translate: Optional[Callable[[ControlTranslateContext], Optional[Decision]]]
    routing_priority: int
    schema_priority: int
    #: Named values this mount publishes to the kernel seams that consume them
    #: (D8), collected by the builder into one mapping — the same closed-
    #: vocabulary pattern as ``PackContribution.exports``. Keys are drawn from the
    #: admitted vocabulary (:data:`CONTROL_EXPORT_ASK_ANSWER_CODEC`). Empty for a
    #: mount whose only contribution is a schema + translate (the common case).
    exports: Mapping[str, Any] = field(default_factory=dict)


#: The uniform factory signature every control-tool entry implements: read the
#: build context, self-gate, and return a mount or ``None``.
ControlToolFactory = Callable[[ControlToolBuildContext], Optional[ControlToolMount]]


@dataclass(frozen=True, slots=True)
class ControlToolEntry:
    """One resolved control tool in the builder's loop: name + priority + factory.

    Mirrors :class:`~noeta.execution.session_pack.SessionPackEntry`: ``name`` is
    the contribution name (collision key + tie-breaker), ``priority`` is the
    declared band the loop orders factory execution by. The output byte orders
    come from each mount's OWN ``schema_priority`` / ``routing_priority``, so the
    entry priority is not itself load-bearing — it only fixes a stable, sorted
    iteration for the collision check.
    """

    name: str
    priority: int
    factory: ControlToolFactory
