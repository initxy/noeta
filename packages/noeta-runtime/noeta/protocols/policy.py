"""Policy Protocol — the function "given current View, decide what's next".

Phase 0 only ships StubFinishPolicy; ReActPolicy lands in Phase 1.

The ``decide`` signature carries an explicit
:class:`noeta.protocols.step_context.StepContext` so downstream callees
(notably the runtime LLM client) can stamp the right ``task_id`` /
``lease_id`` / ``trace_id`` onto recorded events without reading
process-globals.
"""

from __future__ import annotations

from typing import Protocol

from noeta.protocols.decisions import Decision
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View


class Policy(Protocol):
    """Decides the next Decision given the composed View.

    Implementations may be pure LLM, pure FSM, or hybrid. They must not
    read the EventLog directly — the ``ctx`` argument plus ``view`` are
    the only sanctioned inputs.
    """

    def decide(self, ctx: StepContext, view: View) -> Decision: ...
