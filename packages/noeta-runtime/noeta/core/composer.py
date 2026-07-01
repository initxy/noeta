"""Pass-through ContextComposer (D3).

The kernel holds **zero** opinion on how a ``View`` is assembled — that is
material, not mechanism, and lives in ``noeta.context`` (the SDK) per
D2. But the Engine's compose→decide loop still needs *a* ``ContextComposer``
to call. D3 severs the old kernel→material edge (the Engine used to
lazily import ``noeta.context.ThreeSegmentComposer`` as a default) by making
``composer`` a required injection and providing this in-kernel pass-through as
the documented zero-opinion fallback.

:class:`PassthroughComposer` composes every ``Task`` to the empty ``View()``:
no system prompt, no skills, no tool schema, and crucially ``plan_ref=None`` so
the Engine skips the ``ContextPlanComposed`` emission
(:func:`noeta.core.engine._emit_context_plan`). It is deterministic — no LLM,
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
    while expressing no view-assembly policy. Because ``View()`` carries
    ``plan_ref=None`` the Engine emits no ``ContextPlanComposed`` event for
    a step — identical to the retired ``MinimalComposer`` migration-window
    behaviour and to a Task that never entered a real compose path.
    """

    def compose(self, task: Task) -> View:  # noqa: ARG002 - protocol shape
        return View()
