"""The ``env`` field of a hook in Claude Code's ``hooks`` format is noeta's
own, for a Claude Code plugin's options: the places a person or an agent
file writes hooks in never set it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["without_env"]


def without_env(hooks: Mapping[str, Any]) -> dict[str, Any]:
    """A ``hooks`` object with no hook's ``env``."""
    return {event: [{**g, "hooks": [{k: v for k, v in h.items() if k != "env"}
                                    if isinstance(h, dict) else h for h in g["hooks"]]}
                    if isinstance(g, dict) and isinstance(g.get("hooks"), list) else g
                    for g in groups] if isinstance(groups, list) else groups
            for event, groups in hooks.items()}
