"""L2 policies layer.

Houses the ``Policy`` implementations that turn a composed ``View`` into a
neutral ``Decision``: ``ReActPolicy`` (the ReAct loop bridging LLM round-trips
into Decisions), ``OrchestrationPolicy`` / ``StructuredOutputPolicy`` (the
``run_workflow`` script interpreter and its per-helper structured-return
wrapper), the control-tool vocabulary in ``control_semantics`` (schema +
validator + response→Decision translation, one per-tool section), the skill
``allowed-tools`` alias resolution in ``skill_tools``, and the deterministic
test doubles in ``stub`` (``StubFinishPolicy`` / ``StubScriptedPolicy``).
"""

from __future__ import annotations

__all__: list[str] = []
