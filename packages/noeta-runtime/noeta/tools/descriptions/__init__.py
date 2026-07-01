"""Tool descriptions as independent text resources.

Each shipping built-in tool's LLM-facing description lives in a sibling
``<name>.md`` file in this package, not in a Python string literal. This
lets the descriptions iterate like documentation (clean ``git diff``,
editable by non-engineers) while staying the single canonical source the
composer renders into the provider tool schema.

Every description file follows the same four-section shape so the model
gets symmetric guidance per tool:

* **What it does** — the action.
* **When to use** — the trigger.
* **When NOT to use** — the anti-trigger (opencode treats this as
  standard; it markedly cuts mis-firing).
* **Preconditions** — what must already hold before the call.

Loading delegates to the shared :func:`noeta.protocols.resources.load_markdown`
(generalized): it reads via :mod:`importlib.resources` so it
works from an installed wheel (no fragile ``__file__``-relative path), caches
the result so a class-level default
(``description: str = field(default=load_tool_description("read"))``) costs
one read per process, and strips the trailing newline so the Markdown text
composes cleanly into a schema.
"""

from __future__ import annotations

from noeta.protocols.resources import load_markdown


__all__ = ["load_tool_description"]


def load_tool_description(name: str) -> str:
    """Return the text of the ``<name>.md`` description resource.

    ``name`` is the tool's neutral Noeta name (e.g. ``"read"``). The file
    must exist in this package; a missing resource raises
    ``FileNotFoundError`` loudly so a typo never mints an empty tool
    description (which would silently strip the model's only source of
    tool semantics).

    The returned string is the file content with a trailing newline
    stripped — descriptions are authored as Markdown files (ending in a
    newline) but consumed as schema description strings.
    """
    return load_markdown(__package__, name)
