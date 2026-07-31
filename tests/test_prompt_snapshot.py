"""Golden snapshot of the four official preset agents' model-visible identity.

For each of ``main`` / ``explore`` / ``plan`` / ``general-purpose`` this pins:

* ``system_prompt`` — the verbatim instructions the model is given;
* ``tools`` — the allowed tool set (name + version + risk_level), the surface
  advertised to the model;
* ``plugins`` / ``spawnable`` — the activation tuple (control surfaces /
  delegation rights) that shapes the agent's behaviour and is its identity.

An edit that silently re-words a prompt, drops a tool, or flips an activation
fails the matching golden with a human-readable text diff, so a preset's
model-visible bytes cannot drift unreviewed.

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


# Pulled from ``official_specs()`` once, so any change to the preset set
# surfaces here as a missing or orphaned golden rather than being skipped.
_SPECS = official_specs()
_PRESET_NAMES = sorted(_SPECS)


def test_preset_set_is_the_canonical_four() -> None:
    """The snapshot suite covers exactly the four official presets, so a
    change to the preset set forces a matching change to the goldens instead
    of leaving an agent's bytes uncovered."""
    assert set(_PRESET_NAMES) == {"main", "explore", "plan", "general-purpose"}


def _preset_view(spec: AgentSpec) -> dict[str, object]:
    """Build the stable, model-visible snapshot payload for one preset.

    ``tools`` and the activation tuple are already sorted by
    ``AgentSpec.__post_init__``, which is what makes the payload stable across
    runs; each tool is rendered as ``{name, version, risk_level}`` — the
    identity surface the model sees.
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
