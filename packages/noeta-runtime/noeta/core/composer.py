"""Zero-opinion ``ContextComposer`` for the kernel.

How a ``View`` is assembled is material, not mechanism, so the kernel holds no
opinion on it and ``composer`` is a required Engine injection. This
pass-through is what a caller wires when it wants none: deterministic (no LLM,
clock, randomness, network or ContentStore write) and dependent only on
``noeta.protocols``.
"""

from __future__ import annotations

from noeta.protocols.task import Task
from noeta.protocols.view import View


class PassthroughComposer:
    """Every Task composes to the empty ``View``.

    ``View()`` carries ``plan_ref=None``; the Engine still records the step
    boundary as a ``ContextPlanComposed``, because the iteration counter must
    not depend on a composer having stored a plan.
    """

    def compose(self, task: Task) -> View:  # noqa: ARG002 - protocol shape
        return View()
