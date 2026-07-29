"""Byte-identity parity goldens for compiled ``AgentSpec`` s (M2 safety net).

The SDK-extensibility redesign
(``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``) retires
``Capabilities`` and re-expresses every built-in capability as a built-in
plugin loaded through the new surface registry. Its **acceptance criterion 1**
is a hard promise: a bare ``Options()`` and every official preset must still
compile to a **byte-identical ``AgentSpec``** after the mechanism change.

This module is the characterization net that pins that contract **before** any
mechanism change lands (Plan: "byte-identical parity test"; Risks: "Parity
drift while removing ``Capabilities`` — mitigated by writing the parity test
before the removal"). For a bare ``Options()`` and for every official
recipe in :mod:`noeta.presets`, it compiles via
:func:`noeta.client.options.compile_options` and serializes the resulting
``AgentSpec`` (root + descendants) deterministically into a golden file. The
golden IS the byte-identity contract: after the redesign, the same recipes must
serialize to the same bytes or this test fails with a readable diff.

Serialization is ``dataclasses.asdict`` (full, recursive, every field — not the
narrowed ``test_prompt_snapshot`` view) fed through
:func:`tests._snapshot.stable_json` (``sort_keys`` ⇒ a stable, deterministic
field order; no object ids / addresses / timestamps by construction).

Re-pin (regenerate goldens) with one command::

    UPDATE_SNAPSHOTS=1 uv run pytest \\
        tests/test_agent_spec_parity.py -q -p no:cacheprovider
"""

from __future__ import annotations

import dataclasses
from typing import Callable

import pytest

from noeta.agent.spec import AgentSpec
from noeta.client.options import Options, compile_options
from noeta.presets import (
    main_options,
    sandbox_browser_options,
    with_consolidation_agent,
)

from tests._snapshot import assert_snapshot, stable_json


# A fixed system prompt for the "bare Options()" case: ``Options.system_prompt``
# is required (no default), so the minimal recipe still needs one string. Every
# other field is left at its default — this is the DEFAULT_PLUGINS contract the
# redesign must preserve (all 11 built-in fs/web tools on, memory/browser off,
# no control tools), captured here as concrete bytes.
_BARE_SYSTEM_PROMPT = "You are a helpful assistant."


#: The recipes under contract: label → a zero-arg factory returning ``Options``.
#: Together they surface every official ``AgentSpec`` identity — ``main`` and
#: its three subagents (``main``), the ``main``-web variant plus the ``web``
#: specialist (``sandbox_browser``), and the reserved ``__consolidation__``
#: curator (``consolidation``).
_RECIPES: dict[str, Callable[[], Options]] = {
    "bare_options": lambda: Options(system_prompt=_BARE_SYSTEM_PROMPT),
    "main": main_options,
    "sandbox_browser": sandbox_browser_options,
    "consolidation": lambda: with_consolidation_agent(main_options()),
}


def _spec_payload(spec: AgentSpec) -> dict[str, object]:
    """The full, recursive serialization of one ``AgentSpec`` — every field."""
    return dataclasses.asdict(spec)


def _recipe_payload(options: Options) -> dict[str, object]:
    """Compile ``options`` and serialize root + descendants (sorted by name).

    Descendants are sorted so author/dict ordering can never perturb the bytes
    — the same normalization ``AgentSpec`` applies to its own component lists.
    """
    root, descendants = compile_options(options)
    return {
        "root": _spec_payload(root),
        "descendants": [
            _spec_payload(d)
            for d in sorted(descendants, key=lambda s: s.name)
        ],
    }


@pytest.mark.parametrize("label", sorted(_RECIPES))
def test_agent_spec_parity_golden(label: str) -> None:
    """The recipe's compiled ``AgentSpec`` bytes match its parity golden."""
    payload = stable_json(_recipe_payload(_RECIPES[label]()))
    assert_snapshot(f"agent_spec_parity_{label}.txt", payload)


def test_compile_options_is_deterministic() -> None:
    """A second compile of each recipe yields byte-identical serialization.

    Guards the parity goldens themselves: if compilation were non-deterministic
    the goldens would be meaningless, so pin referential transparency directly
    (the property ``compile_options`` documents) rather than trusting it.
    """
    for factory in _RECIPES.values():
        first = stable_json(_recipe_payload(factory()))
        second = stable_json(_recipe_payload(factory()))
        assert first == second
