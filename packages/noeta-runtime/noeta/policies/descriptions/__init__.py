"""Control-tool descriptions as independent text resources.

Mirrors the :mod:`noeta.tools.descriptions` mechanism for the **control**
layer: a control tool's LLM-facing description lives in a sibling
``<name>.md`` file in this package, not in a Python string literal. This
lets the descriptions iterate like documentation (clean ``git diff``,
editable by non-engineers) while staying the single canonical source the
composer renders into the provider tool schema.

Loading delegates to the shared :func:`noeta.protocols.resources.load_markdown`
(generalized). Previously this loader was duplicated verbatim
from ``noeta.tools.descriptions`` because ``noeta.policies`` and ``noeta.tools``
are independent siblings on the same import layer (a ``policies → tools`` edge
is forbidden). With agent prompts becoming a third consumer of the same
mechanism, the shared implementation now lives in the L0 ``noeta.protocols``
layer — which every higher layer may import and which depends on nothing — so
all three call sites share one canonical loader instead of each copying it.

Every description file follows the same four-section shape so the model
gets symmetric guidance per control tool:

* **What it does** — the action.
* **When to use** — the trigger.
* **When NOT to use** — the anti-trigger.
* **Preconditions** — what must already hold before the call.
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
