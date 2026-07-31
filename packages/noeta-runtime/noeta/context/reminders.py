"""Compose-time reminder registry — the composer's swappable reminder table.

A **reminder** is a compose-time, *pure* contribution rendered at the **tail of
the dynamic suffix**: ``(name, priority, render)`` where ``render`` is a pure
function of a narrow folded-state projection (:class:`ReminderView`) returning
``str | None``. A non-``None`` string is wrapped by the composer in one
``Message(role="user", origin="system")`` (the adapter turns ``origin="system"``
into ``<system-reminder>``); ``None`` renders nothing.

This mirrors :mod:`noeta.context.content_channel`: one :class:`ReminderSpec` per
reminder, collected into an immutable :class:`ReminderRegistry` the
:class:`~noeta.context.composer.ThreeSegmentComposer` consults — the composer
stays locked (registry hook only), and adding a reminder is registering one spec,
not editing the composer. Ordering is integer ``priority`` ascending, ties broken
by ``name`` (cross-plugin ``(plugin, name)`` ties are resolved by the plugin
merge before specs reach the registry).

Red line (same trust class as ``ContentKindSpec`` renderers): a ``render`` must
be a pure function of the projection — no clock, no randomness, no external
fetch — so the same folded state always composes the same dynamic-suffix bytes.
The stable prefix is untouched by construction: reminders only ever append to the
volatile dynamic suffix. This module is the registry **mechanism only**; the
built-in reminders live in the ``reminders`` built-in plugin and reach the
composer through the kernel builder's injected ``base_reminders`` tuple. A
composer built without a registry has **no** reminders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional


__all__ = [
    "ReminderView",
    "ReminderRender",
    "ReminderSpec",
    "ReminderRegistry",
]


@dataclass(frozen=True, slots=True)
class ReminderView:
    """Narrow, read-only projection of folded state a reminder renders from.

    Built by the composer once per ``compose`` from the task's folded state (and
    the composer's own compose-time facts — whether the ``spawn_subagent``
    control schema is offered, whether a spawn already landed in history). A
    reminder ``render`` sees only this projection, never the raw ``Task`` — the
    same narrowing ``ContentKindSpec`` renderers get (post-fold names only), so a
    render cannot reach past its inputs and break compose purity.

    Fields cover exactly the three built-in reminders; a new reminder that needs
    another folded fact widens this projection (a deliberate, reviewed change to
    the locked composer's contract, not an open-ended ``Task`` handle).
    """

    #: ``TaskState.todos`` — the replace-all checklist of ``{id, content, status}``.
    todos: tuple[Mapping[str, object], ...] = ()
    #: Whether the ``spawn_subagent`` control schema is offered this compose.
    delegation_enabled: bool = False
    #: Whether a ``spawn_subagent`` call already landed in the rolling history.
    already_spawned: bool = False
    #: ``ContextState.compaction_thrashing`` — the latched thrash flag.
    compaction_thrashing: bool = False


#: A reminder render: the folded-state projection -> reminder text (or ``None``
#: to render nothing this compose). Must be pure (see the module red line).
ReminderRender = Callable[[ReminderView], Optional[str]]


@dataclass(frozen=True, slots=True)
class ReminderSpec:
    """One registered compose-time reminder: ``(name, priority, render)``.

    * ``name`` — the collision / ordering key **and** the listing label.
    * ``priority`` — integer sort key (ascending); ties broken by ``name``.
    * ``render`` — the pure projection -> ``str | None`` function.
    """

    name: str
    priority: int
    render: ReminderRender

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ReminderSpec.name must be non-empty")
        if not callable(self.render):
            raise ValueError("ReminderSpec.render must be callable")


class ReminderRegistry:
    """Immutable, priority-ordered table of :class:`ReminderSpec`.

    The composer consults it at the dynamic-suffix tail:
    :meth:`render_all` runs every reminder in ``(priority, name)`` order and
    returns the non-``None`` texts, which the composer wraps one message each.
    Duplicate names raise (the same loud-collision stance as
    :class:`~noeta.context.content_channel.ContentChannelRegistry`).
    """

    def __init__(self, items: Iterable[ReminderSpec]) -> None:
        seen: set[str] = set()
        specs: list[ReminderSpec] = []
        for item in items:
            if item.name in seen:
                raise ValueError(f"duplicate reminder {item.name!r} in registry")
            seen.add(item.name)
            specs.append(item)
        # Deterministic order: priority ascending, then name. Cross-plugin
        # ``(plugin, name)`` ties are already resolved by the plugin merge
        # before specs arrive here, so name alone is a sufficient local tie-break.
        specs.sort(key=lambda s: (s.priority, s.name))
        self._specs: tuple[ReminderSpec, ...] = tuple(specs)

    def specs(self) -> tuple[ReminderSpec, ...]:
        """Every spec, in composed order — for inspection / testing."""
        return self._specs

    def render_all(self, view: ReminderView) -> list[str]:
        """Every reminder's non-``None`` text, in composed order.

        Pure over ``view`` (each ``render`` is pure by contract). A render that
        raises propagates loudly — a compose-time reminder is not allowed to
        degrade silently (same stance as a content renderer).
        """
        out: list[str] = []
        for spec in self._specs:
            text = spec.render(view)
            if text is not None:
                out.append(text)
        return out

