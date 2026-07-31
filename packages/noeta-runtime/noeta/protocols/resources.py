"""Shared loader for long text externalized into in-package ``<name>.md`` files.

Agent prompts (``noeta.presets.prompts``) and every builtin tool's description
(beside its impl in ``noeta.builtins.<name>.impl``) load through here. It sits
at L0 and imports only stdlib so any layer may depend on it, since
``noeta.protocols`` itself depends on nothing.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


__all__ = ["load_markdown"]


@lru_cache(maxsize=None)
def load_markdown(anchor_package: str, name: str, *, strip: bool = True) -> str:
    """Read the text of the ``<name>.md`` resource inside ``anchor_package``.

    Reads via :mod:`importlib.resources` rather than ``__file__``-relative
    paths so it works inside a wheel, and caches by
    ``(anchor_package, name, strip)`` — one read per resource per process. A
    missing file raises ``FileNotFoundError``: a typo must never silently mint
    empty text, which would strip the model's only source of tool/role
    semantics.

    ``strip=True`` (the default) suits description files, written as Markdown
    with a trailing blank line and consumed as schema strings. Pass
    ``strip=False`` for prompts: ``AgentSpec`` hashes the content of its
    instructions, so one extra newline changes the hash.
    """
    resource = files(anchor_package).joinpath(f"{name}.md")
    text = resource.read_text(encoding="utf-8")
    return text.rstrip("\n") if strip else text
