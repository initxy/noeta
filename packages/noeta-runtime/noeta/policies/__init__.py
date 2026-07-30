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
workflow-script validation sandbox in ``workflow_sandbox``, and the
deterministic test doubles in ``stub`` (``StubFinishPolicy`` /
``StubScriptedPolicy``).

Everything the ``react`` built-in imports from this band is **public-named**
(``control_semantics``, ``control_tools``, ``workflow_sandbox``,
:func:`~noeta.policies.control_semantics.concurrent_fanout_enabled`): that
import crosses the wheel boundary now, so an underscore here would be a
contract the kernel does not actually offer. ``_control_translate`` stays
private — it is a pure compatibility shim for pre-merge call sites, and
nothing outside this wheel may depend on it.
"""

from __future__ import annotations

__all__: list[str] = []
