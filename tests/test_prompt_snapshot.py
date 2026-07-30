"""Golden snapshot of the four official preset agents' model-visible identity.

For each of ``main`` / ``explore`` / ``plan`` / ``general-purpose`` this pins:

* ``system_prompt`` — the verbatim instructions the model is given;
* ``tools`` — the allowed tool set (name + version + risk_level), the surface
  advertised to the model;
* ``plugins`` / ``spawnable`` — the activation tuple (control surfaces /
  delegation rights) that shapes the agent's behaviour and is its identity (D6:
  ``Capabilities`` retired).

A refactor that silently changes any of these (re-words a prompt, drops a tool,
flips an activation) fails the matching golden with a human-readable text diff.
This is the lightweight stand-in for the deleted verify/replay byte-equality
moat.

Re-pin (regenerate goldens) with one command::

    UPDATE_SNAPSHOTS=1 uv run pytest \\
        tests/test_prompt_snapshot.py tests/test_tool_schema_snapshot.py \\
        -q -p no:cacheprovider

Determinism: only plain JSON-able strings/bools/lists are serialized (no object
ids, addresses, timestamps), tool lists come out pre-sorted from
``AgentSpec.__post_init__``, and ``stable_json`` sorts dict keys.
"""

from __future__ import annotations

import pytest

from noeta.agent.spec import AgentSpec
from noeta.presets import official_specs

from tests._snapshot import assert_snapshot, stable_json


# The four official preset names. Pulled from ``official_specs()`` once so a
# new/removed preset surfaces here (missing golden / orphan) rather than being
# silently skipped.
_SPECS = official_specs()
_PRESET_NAMES = sorted(_SPECS)


def test_preset_set_is_the_canonical_four() -> None:
    """Guard: the snapshot suite covers exactly the four official presets.

    If a preset is added or removed this assertion flags it, prompting the
    author to add (or drop) the corresponding golden rather than leave the new
    agent's bytes uncovered.
    """
    assert set(_PRESET_NAMES) == {"main", "explore", "plan", "general-purpose"}


def _preset_view(spec: AgentSpec) -> dict[str, object]:
    """Build the stable, model-visible snapshot payload for one preset.

    ``tools`` are already sorted by ``AgentSpec.__post_init__``; each is
    rendered as ``{name, version, risk_level}`` — the identity surface the model
    sees. ``plugins`` / ``spawnable`` are the activation tuple (already sorted by
    ``__post_init__``) — the identity that shapes behaviour (D6).
    """
    return {
        "name": spec.name,
        "system_prompt": spec.instructions,
        "tools": [
            {"name": t.name, "version": t.version, "risk_level": t.risk_level}
            for t in spec.tools
        ],
        "plugins": list(spec.plugins),
        "spawnable": list(spec.spawnable),
    }


@pytest.mark.parametrize("preset", _PRESET_NAMES)
def test_preset_prompt_tools_capabilities_snapshot(preset: str) -> None:
    """The preset's system prompt + tool set + activation match its golden."""
    spec = _SPECS[preset]
    payload = stable_json(_preset_view(spec))
    assert_snapshot(f"preset_{preset}.txt", payload)
