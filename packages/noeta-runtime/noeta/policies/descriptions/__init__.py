"""Control-tool descriptions as independent text resources.

The **control** layer's description resources: a control tool's LLM-facing
description lives in a sibling ``<name>.md`` file in this package, not in a
Python string literal (execution-tool descriptions follow the same shape,
shipped beside each tool's builtin impl since phase 2c). This
lets the descriptions iterate like documentation (clean ``git diff``,
editable by non-engineers) while staying the single canonical source the
composer renders into the provider tool schema.

Loading delegates to the shared :func:`noeta.protocols.resources.load_markdown`
(generalized): the loader lives in the L0 ``noeta.protocols`` layer — which
every higher layer may import and which depends on nothing — so every
description/prompt resource consumer (this package, the builtin tool impls,
``noeta.presets``) shares one canonical loader instead of each copying it.

Every tool-level description file follows the same four-section shape so
the model gets symmetric guidance per control tool:

* **What it does** — the action.
* **When to use** — the trigger.
* **When NOT to use** — the anti-trigger.
* **Preconditions** — what must already hold before the call.

Property-level prose long enough to iterate like documentation (e.g. the
``spawn_subagent`` ``spawns`` / ``background`` argument texts) lives here
too, under ``<tool>_<property>.md`` — short schema-shape one-liners stay
inline in ``control_semantics``.
"""

from __future__ import annotations

from noeta.protocols.resources import load_markdown


__all__ = ["load_control_tool_description"]


def load_control_tool_description(name: str) -> str:
    """Return the text of the ``<name>.md`` control-tool description.

    ``name`` is the control tool's neutral name (e.g. ``"run_workflow"``).
    The file must exist in this package; a missing resource raises
    ``FileNotFoundError`` loudly so a typo never mints an empty tool
    description (which would silently strip the model's only source of
    tool semantics).

    The returned string is the file content with a trailing newline
    stripped — descriptions are authored as Markdown files (ending in a
    newline) but consumed as schema description strings.
    """
    return load_markdown(__package__, name)
