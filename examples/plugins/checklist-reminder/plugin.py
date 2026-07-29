"""First-party example manifest plugin — ``checklist-reminder``: a compose-time
(track B) reminder.

Demonstrated SDK capability
---------------------------
The new ``reminder`` surface — **track B** of the SDK-extensibility redesign
(``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``, D8): a
compose-time, **pure** reminder. A ``reminder`` is ``(name, priority, render)``
where ``render`` is a *pure function of a narrow folded-state projection*
returning ``str | None``, rendered at the **tail of the dynamic suffix** (the
composer wraps a non-``None`` string in one ``<system-reminder>`` message). The
stable prefix is untouched by construction — reminders only append to the
volatile dynamic suffix.

Purity is the contract (the same trust class as a ``ContentKindSpec`` renderer):
no clock, no randomness, no external fetch — so the same folded state always
composes the same bytes, and replay / the KV-cache prefix stay reproducible.

What it renders
---------------
When the agent's checklist grows past :data:`THRESHOLD` unfinished items, it
appends a scope-hygiene nudge — a pure function of the projection's ``todos``.
A short (or finished) list renders nothing, so the reminder is self-limiting and
never nags. ``priority`` places it AFTER the three built-in reminders
(``unfinished-todos`` 100, ``delegation-nudge`` 200, ``read-suggestion`` 300).

The ``render`` takes the composer's narrow ``ReminderView`` projection by
duck typing — it reads only ``view.todos`` (a tuple of ``{id, content, status}``
mappings), never the raw task — so this example stays on the ``noeta.sdk``
public surface (the projection type is a runtime internal).
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.sdk import PluginBuilder


#: Unfinished-item count above which the nudge renders.
THRESHOLD = 5


def long_checklist_reminder(view: Any) -> Optional[str]:
    """Nudge to split a long checklist into sub-agents (pure over ``view.todos``).

    ``view`` is the composer's ``ReminderView`` projection; only its ``todos``
    field is read. Returns ``None`` (render nothing) unless there are more than
    :data:`THRESHOLD` unfinished todos.
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


#: The single-file manifest (decorator sugar *is* the manifest, spec D1). The
#: contributed render is cached for single-file resolution; a distributed install
#: exposes it at ``checklist_reminder:long_checklist_reminder``.
#: ``python -m noeta.sdk.plugin_check`` derives the TOML from this builder.
plugin = PluginBuilder("checklist-reminder", requires_noeta=">=0.4")
plugin.reminder(long_checklist_reminder, name="long-checklist", priority=400)
