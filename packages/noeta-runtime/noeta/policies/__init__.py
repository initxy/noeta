"""The control band (microkernel phase 2b).

The decision-mapping policy implementations — ``ReActPolicy`` and the
``run_workflow`` interpreter ``OrchestrationPolicy`` /
``StructuredOutputPolicy`` — live in the ``react`` built-in plugin
(``noeta.builtins.react.impl``); the kernel builder receives the default
through its ``default_policy_factory`` injection. What stays here is the
control vocabulary the kernel itself owns (phase-1 D3: control tools are
renderings of kernel Decision variants, not contributions): the per-tool
schema + validator + response→Decision translation in ``control_semantics``
(re-exported via ``control_tools`` / ``_control_translate``), the
workflow-script validation sandbox in ``_workflow_sandbox``, and the
deterministic test doubles in ``stub`` (``StubFinishPolicy`` /
``StubScriptedPolicy``).
"""

from __future__ import annotations

__all__: list[str] = []
