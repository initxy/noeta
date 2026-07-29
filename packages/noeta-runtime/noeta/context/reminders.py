"""Compose-time reminder registry — the composer's swappable reminder table (D8).

Track B of the SDK-extensibility redesign
(``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``, D8). A
**reminder** is a compose-time, *pure* contribution rendered at the **tail of
the dynamic suffix**: ``(name, priority, render)`` where ``render`` is a pure
function of a narrow folded-state projection (:class:`ReminderView`) returning
``str | None``. A non-``None`` string is wrapped by the composer in one
``Message(role="user", origin="system")`` (the adapter turns ``origin="system"``
into ``<system-reminder>``); ``None`` renders nothing.

This mirrors :mod:`noeta.context.content_channel` exactly: one
:class:`ReminderSpec` per reminder, collected into an immutable
:class:`ReminderRegistry` the :class:`~noeta.context.composer.ThreeSegmentComposer`
consults — the composer stays locked (registry hook only, like the
``ContentChannelRegistry`` seam), and adding a reminder is registering one spec,
not editing the composer.

Ordering is the guard-observer precedent: integer ``priority`` ascending, ties
broken by ``name`` (cross-plugin ``(plugin, name)`` tie-breaking is resolved
upstream by the plugin merge before specs reach the registry).

Red line (same trust class as ``ContentKindSpec`` renderers): a ``render`` must
be a pure function of the projection — no clock, no randomness, no external
fetch — so the same folded state always composes the same dynamic-suffix bytes.
The stable prefix is untouched by construction: reminders only ever append to
the volatile dynamic suffix.

The three built-in reminders (:func:`default_reminder_registry`) are the
migration of the composer's former ``_append_todo_reminder`` /
``_append_concurrency_reminder`` / ``_append_compaction_thrashing_reminder``
methods; their priorities are chosen to keep the composed output byte-identical
to the pre-migration order (todo -> delegation -> read). They are *declared* by
the ``reminders`` built-in plugin (the reference / listing surface, D11) and
wired here directly, exactly as the ``fs`` / ``web`` built-in tool packs declare
tools that the compile path still sources from ``BUILTIN_TOOL_CLASSES``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional, Sequence


__all__ = [
    "ReminderView",
    "ReminderRender",
    "ReminderSpec",
    "ReminderRegistry",
    "default_reminder_registry",
    "BUILTIN_REMINDER_PRIORITIES",
    "todo_reminder",
    "delegation_reminder",
    "read_suggestion_reminder",
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


# ---------------------------------------------------------------------------
# The three built-in reminders (migrated verbatim from the composer methods).
# Each is a pure function of the projection; the text bytes are copied
# word-for-word so the composed dynamic suffix stays byte-identical (D8 /
# acceptance 9).
# ---------------------------------------------------------------------------


def todo_reminder(view: ReminderView) -> Optional[str]:
    """List the *unfinished* todos so the model does not go blind on its plan.

    ``TaskState.todos`` is folded state the model writes through ``todo_write``
    but otherwise never sees again. Surface only the items not ``completed``; an
    empty or all-completed list renders nothing (a finished checklist must not
    nag). Migrated verbatim from ``ThreeSegmentComposer._append_todo_reminder``.
    """
    unfinished = [
        t
        for t in view.todos
        if isinstance(t, dict) and t.get("status") != "completed"
    ]
    if not unfinished:
        return None
    lines = [
        f"- [{t.get('status', 'pending')}] {t.get('content', '')}"
        for t in unfinished
    ]
    return (
        "Your current todo list (unfinished items only). Keep it updated as "
        "you make progress — mark items in_progress / completed via "
        "todo_write so it stays accurate:\n"
        + "\n".join(lines)
    )


def delegation_reminder(view: ReminderView) -> Optional[str]:
    """Just-in-time fan-out nudge while delegation is offered and unused.

    Live only while ``delegation_enabled`` AND no ``spawn_subagent`` has landed
    yet — self-limiting once the first sub-agent is spawned. Migrated verbatim
    from ``ThreeSegmentComposer._append_concurrency_reminder``.
    """
    if not view.delegation_enabled or view.already_spawned:
        return None
    return (
        "When you delegate independent work to sub-agents, batch ALL the "
        "goals into ONE spawn_subagent call's spawns array so they run "
        "concurrently and the results return together. Spawning one per "
        "turn is sequential, not parallel."
    )


def read_suggestion_reminder(view: ReminderView) -> Optional[str]:
    """Suggest a different read strategy while compaction is thrashing.

    Live only while ``ContextState.compaction_thrashing`` is latched. Migrated
    verbatim from ``ThreeSegmentComposer._append_compaction_thrashing_reminder``.
    """
    if not view.compaction_thrashing:
        return None
    return (
        "The context window keeps getting refilled to the limit by what "
        "looks like a single large file or large tool output, so compaction "
        "is spinning without freeing real headroom. Consider a different "
        "reading strategy: read in chunks, read only the relevant section, "
        "or extract the key points once and re-read on demand instead of "
        "pulling the whole large content back into context each time."
    )


#: The built-in reminders' names -> priority. Chosen so the composed order is
#: todo -> delegation -> read, byte-identical to the pre-migration append order.
#: Spread by 100 to leave room for third-party reminders to interleave.
BUILTIN_REMINDER_PRIORITIES: dict[str, int] = {
    "unfinished-todos": 100,
    "delegation-nudge": 200,
    "read-suggestion": 300,
}

_BUILTIN_REMINDER_SPECS: tuple[ReminderSpec, ...] = (
    ReminderSpec("unfinished-todos", BUILTIN_REMINDER_PRIORITIES["unfinished-todos"], todo_reminder),
    ReminderSpec("delegation-nudge", BUILTIN_REMINDER_PRIORITIES["delegation-nudge"], delegation_reminder),
    ReminderSpec("read-suggestion", BUILTIN_REMINDER_PRIORITIES["read-suggestion"], read_suggestion_reminder),
)


def default_reminder_registry(
    extra: Sequence[ReminderSpec] = (),
) -> ReminderRegistry:
    """The three built-in reminders, plus ``extra`` third-party reminders.

    The default the composer falls back to when constructed without an explicit
    registry (keeping every direct-construction call site byte-identical), and
    the base the builder extends with per-agent-activated plugin reminders
    (``extra``) — mirroring how ``ContentChannelRegistry`` is built from the
    built-in content kinds plus ``Options.content_channels``.
    """
    return ReminderRegistry((*_BUILTIN_REMINDER_SPECS, *extra))
