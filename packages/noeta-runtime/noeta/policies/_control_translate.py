"""Compatibility re-export shim — see :mod:`noeta.policies.control_semantics`.

ADR deepening (C04 control-semantics): the control-tool response→neutral
Decision translation seam (the ``_maybe_*`` family + :func:`translate_control_tool`
+ :class:`ControlToolSpec` + ``spawn_subagent`` schema) was the *translate*
**half** of each control tool's story; its schema/validator/codec **half**
lived in ``control_tools``. The two were merged into
:mod:`noeta.policies.control_semantics` so each control tool's whole story —
schema + validator + translate — is collocated in one per-tool section
(locality). Byte-for-byte unchanged: same routing priority, same validation
branches, same ack/error strings, same Decisions.

This thin module re-exports the names it always exported so every
``from noeta.policies._control_translate import ...`` call site keeps working
unchanged.
"""

from __future__ import annotations

from noeta.policies.control_semantics import (
    SKILL_TOOL,
    SPAWN_SUBAGENT_TOOL,
    ControlToolSpec,
    translate_control_tool,
)


# ``spawn_subagent_tool_schema`` moved into the ``delegation`` built-in
# (control-tool-surface S2); only the reserved tool NAME + the neutral
# translate mechanism remain kernel-side and re-exportable here.
__all__ = [
    "SKILL_TOOL",
    "SPAWN_SUBAGENT_TOOL",
    "ControlToolSpec",
    "translate_control_tool",
]
