"""Shared Markdown resource loader (generalized).

Three places externalize long text into in-package ``<name>.md`` resources:
execution-tool descriptions, control-tool descriptions, and agent prompts.
The loading mechanism is identical (``importlib.resources`` + cache + optional
trailing-newline strip). They live in ``noeta.tools`` / ``noeta.policies`` /
``noeta.presets`` respectively, and import-linter forbids tools and policies (same
layer) from depending on each other, so the shared implementation sinks to this
L0 layer: anyone may depend on ``noeta.protocols``, which depends on nothing.

Pure stdlib (imports no in-project module), satisfying this layer's "Import-only
dependencies are stdlib" constraint.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


__all__ = ["load_markdown"]


@lru_cache(maxsize=None)
def load_markdown(anchor_package: str, name: str, *, strip: bool = True) -> str:
    """Read the text of the ``<name>.md`` resource inside ``anchor_package``.

    ``anchor_package`` is the dotted path of the package holding the resource
    (e.g. ``"noeta.tools.descriptions"``); ``name`` is the bare name (e.g.
    ``"read"``). Read via :mod:`importlib.resources` so it works inside a wheel
    too (no reliance on ``__file__``-relative paths). The result is cached by
    ``(anchor_package, name, strip)`` — one read per resource per process. A
    missing file raises ``FileNotFoundError`` so a typo never silently mints
    empty text (which would quietly strip the model's only source of tool/role
    semantics).

    ``strip`` controls trailing-newline handling:

    * **Description** files are written as Markdown (a trailing blank line by
      convention); strip the trailing newline before consuming them as schema
      strings, hence the ``True`` default.
    * **Prompt** files must be byte-for-byte equal to the Python constant they
      replace — ``AgentSpec`` hashes the *content* of its instructions, so
      one extra/missing newline changes the hash — hence pass ``strip=False`` for
      exact fidelity.
    """
    resource = files(anchor_package).joinpath(f"{name}.md")
    text = resource.read_text(encoding="utf-8")
    return text.rstrip("\n") if strip else text
