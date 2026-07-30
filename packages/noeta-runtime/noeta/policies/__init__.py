"""The control MECHANISM band (microkernel phase 2b; control-tool-surface S2b).

The decision-mapping policy implementations — ``ReActPolicy`` and the
``run_workflow`` interpreter ``OrchestrationPolicy`` /
``StructuredOutputPolicy`` — live in the ``react`` built-in plugin
(``noeta.builtins.react.impl``); the kernel builder receives the default
through its ``default_policy_factory`` injection. What stays here is the neutral
control MECHANISM the kernel itself owns, with zero product schema / description
/ translate body: ``control_semantics`` (the mount / routing types + dispatcher,
the shared neutral helpers, and the reserved recorded-wire vocabulary the drain /
resolver route on) and the deterministic test doubles in ``stub``
(``StubFinishPolicy`` / ``StubScriptedPolicy``). Control-tool-surface S2/S2b moved
every per-tool schema + validator + translate + ``.md`` out into the built-ins
that own them, and deleted the ``control_tools`` / ``_control_translate``
re-export shims, the ``descriptions`` resource package, and the
``workflow_sandbox`` module (which rode ``run_workflow`` into the ``react``
built-in).

Everything the built-ins import from this band is **public-named**
(``control_semantics``,
:func:`~noeta.policies.control_semantics.concurrent_fanout_enabled`): that import
crosses the wheel boundary, so an underscore here would be a contract the kernel
does not actually offer.
"""

from __future__ import annotations

__all__: list[str] = []
