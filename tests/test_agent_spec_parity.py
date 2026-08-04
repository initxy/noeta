"""Byte-identity parity goldens for compiled ``AgentSpec`` s.

A bare ``Options()`` and every official preset must compile to a
**byte-identical ``AgentSpec``**. This module is the characterization net that
pins that contract: for a bare ``Options()`` and for every official recipe in
:mod:`noeta.presets`, it compiles via
:func:`noeta.client.options.compile_options` and serializes the resulting
``AgentSpec`` (root + descendants) deterministically into a golden file. The
golden IS the byte-identity contract: the same recipes must serialize to the
same bytes or this test fails with a readable diff.

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
# redesign must preserve (all 10 built-in fs/web tools on, memory/browser off,
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


def test_agent_spec_to_from_dict_roundtrips() -> None:
    """``AgentSpec.to_dict`` / ``from_dict`` round-trips every compiled recipe.

    The dict form emits ``plugins`` + ``spawnable`` (the activation-tuple
    identity, D6) — no ``capabilities`` key — and rebuilds byte-for-byte equal.
    """
    for factory in _RECIPES.values():
        root, descendants = compile_options(factory())
        for spec in (root, *descendants):
            raw = spec.to_dict()
            assert "capabilities" not in raw
            assert "plugins" in raw and "spawnable" in raw
            assert AgentSpec.from_dict(raw) == spec


def test_from_dict_hard_break_on_stale_capabilities() -> None:
    """D7 hard break, as a *tested contract*: a recording made before the
    activation-tuple identity swap (a ``capabilities`` key, no ``plugins``) no
    longer decodes — the failure is loud, never a silent plugin-less default.

    This is the decision record: the break is deliberate (nothing published, all
    recordings local), so it is pinned here rather than allowed to surface as an
    accident.
    """
    root, _ = compile_options(main_options())
    fresh = root.to_dict()

    # (1) a stale recording still carrying ``capabilities`` (and no ``plugins``)
    stale = {k: v for k, v in fresh.items() if k not in ("plugins", "spawnable")}
    stale["capabilities"] = {
        "todo_write": True,
        "ask_user_question": True,
        "delegation": True,
        "skill_invocation": True,
        "memory": True,
        "mcp": True,
        "browser": False,
        "spawnable": ["explore", "general-purpose", "plan"],
    }
    with pytest.raises(ValueError, match="capabilities"):
        AgentSpec.from_dict(stale)

    # (2) even without the stale key, a missing ``plugins`` must fail loudly —
    # identity is the tuple now, so a plugin-less agent has to say so explicitly.
    no_plugins = {k: v for k, v in fresh.items() if k != "plugins"}
    with pytest.raises(ValueError, match="plugins"):
        AgentSpec.from_dict(no_plugins)
