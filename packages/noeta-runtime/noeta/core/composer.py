"""Pass-through ContextComposer (D3).

The kernel holds **zero** opinion on how a ``View`` is assembled — that is
material, not mechanism, and lives in ``noeta.context`` (the SDK) per
D2. But the Engine's compose→decide loop still needs *a* ``ContextComposer``
to call. D3 severs the old kernel→material edge (the Engine used to
lazily import ``noeta.context.ThreeSegmentComposer`` as a default) by making
``composer`` a required injection and providing this in-kernel pass-through as
the documented zero-opinion fallback.

:class:`PassthroughComposer` composes every ``Task`` to the empty ``View()``:
no system prompt, no skills, no tool schema, and ``plan_ref=None`` — the
Engine still emits its per-step ``ContextPlanComposed`` (with a ``None``
``plan_ref``) so ``governance.iterations`` / ``max_iterations`` keep
counting (:func:`noeta.core.engine._emit_context_plan`, core #2). It is
deterministic — no LLM,
clock, randomness, network, or ContentStore write — and depends only on
``noeta.protocols``. Production hosts wire a real Composer
(e.g. ``noeta.context.ThreeSegmentComposer``) explicitly; this in-kernel
pass-through is the fallback when no composer is injected.
"""

from __future__ import annotations

from noeta.protocols.task import Task
from noeta.protocols.view import View


class PassthroughComposer:
    """Zero-opinion Composer: every Task composes to the empty ``View``.

    Satisfies the :class:`noeta.protocols.composer.ContextComposer` protocol
    while expressing no view-assembly policy. ``View()`` carries
    ``plan_ref=None``; the Engine still records the step boundary as a
    ``ContextPlanComposed`` with a ``None`` ``plan_ref`` (core #2 — the
    iteration counter must not depend on a composer storing a plan).
    """

    def compose(self, task: Task) -> View:  # noqa: ARG002 - protocol shape
        return View()
