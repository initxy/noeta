"""A compose-time nudge when the agent's checklist grows too long.

Demonstrated SDK capability: the ``reminder`` surface. A reminder is a pure
``render`` over a narrow folded-state projection, appended to the tail of the
composed request's dynamic suffix. Purity is the contract — no clock, no
randomness, no external fetch — because the same folded state must compose the
same bytes for replay and for the cached stable prefix to hold. A reminder that
needs to reach the outside world belongs on ``reminder_provider`` instead, where
its output is recorded.

The render reads the projection by duck typing so this example depends on
nothing outside ``noeta.sdk``; the projection type itself is a runtime internal.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.sdk import PluginBuilder


#: Unfinished-item count above which the nudge renders. High enough that an
#: ordinary multi-step task never trips it.
THRESHOLD = 5


def long_checklist_reminder(view: Any) -> Optional[str]:
    """Nudge to split a long checklist into sub-agents.

    Returning ``None`` renders nothing, which is what keeps the reminder
    self-limiting: a short or finished list is silent, so the nudge cannot decay
    into noise the model learns to skip.
    """
    todos = getattr(view, "todos", ())
    unfinished = [
        t
        for t in todos
        if isinstance(t, dict) and t.get("status") != "completed"
    ]
    if len(unfinished) <= THRESHOLD:
        return None
    return (
        f"Your checklist has {len(unfinished)} unfinished items. When several "
        "are independent, delegate them to sub-agents (batch the goals into one "
        "spawn_subagent call) instead of working the whole list yourself in one "
        "long context."
    )


#: The builder *is* this plugin's manifest, and its name is the plugin identity
#: — the activation key, not the filename. ``python -m noeta.sdk.plugin_check``
#: derives TOML from it and verifies the shipped ``noeta-plugin.toml`` matches.
#:
#: ``priority=400`` places the nudge after the three built-in reminders
#: (``unfinished-todos`` 100, ``delegation-nudge`` 200, ``read-suggestion`` 300),
#: so an advisory note never displaces the ones the agent acts on.
plugin = PluginBuilder("checklist-reminder", requires_noeta=">=0.4")
plugin.reminder(long_checklist_reminder, name="long-checklist", priority=400)
