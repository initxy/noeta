"""The skill listing: the roster the model may load, recorded as residents.

The ``skill`` tool's schema carries no roster (it is the same bytes for every
task and turn); the names and summaries reach the model here instead, as
``skill_listing`` content-channel residents written once and never rewritten —
Claude Code's ``skill_listing`` attachment, carried on the content channel so
compaction and resume need no repair code:

* ``roster`` — the whole fitted roster, recorded by the skills pack's ``init``
  hook, activate-once (``refresh=False``). Pre-loop it renders in
  ``semi_stable``; a task that predates the listing records it at its next
  goal, anchored there. The init hook runs on every new goal, so a roster empty
  at seed is recorded by the first goal that finds skills.
* ``added-<k>`` — the skills that joined the roster since every listing the
  task already carries, recorded by the ``turn_intake`` provider
  (:func:`skill_listing_provider`) and anchored at that goal, so nothing
  written before it moves.

What was announced is read off recorded state — the ``names`` of every active
``skill_listing`` resident, resolved through the content store — never off
process memory, so a restart or another replica announces exactly what this
process would. A removed skill and an edited summary are not announced: the
listing keeps its bytes, and calling a removed skill gets the translate's
"unknown skill" answer. Residents re-hang after a compaction summary
(``docs/adr/036-anchored-content-placement.md``).

The fitted roster of the current build reaches the provider through the task's
local slot (:data:`ROSTER_SLOT`, ``noeta.runtime.task_local``): the pack writes
it at every build and the intake seam runs after the build in the same woken
window. The slot only carries "what the roster is now"; losing it on a restart
costs nothing, because the next build writes it again before intake reads it.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional, Sequence

from noeta.context.composer import ContentResolve, RenderedContent
from noeta.context.content_channel import ContentKindSpec
from noeta.execution.reminders import RecallView, ResidentActivation
from noeta.execution.session_pack import InitHook, SessionRecorder
from noeta.protocols.content_store import ContentStore
from noeta.protocols.errors import ContentNotFound
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.values import ContentRef


__all__ = [
    "ROSTER_SLOT",
    "SKILL_LISTING_KIND",
    "SkillRoster",
    "announced_skill_names",
    "build_listing_init_hook",
    "decode_listing",
    "encode_listing",
    "render_listing",
    "skill_listing_content_kind",
    "skill_listing_provider",
]


#: The content kind the listing residents are recorded under.
SKILL_LISTING_KIND = "skill_listing"
#: The opening resident's name: the whole roster at the task's first sight.
ROSTER_NAME = "roster"
#: Later residents are ``added-0``, ``added-1``, … in recording order.
ADDED_PREFIX = "added-"
SKILL_LISTING_VERSION = "1"
#: The recorded bytes never change for a resident's life (activate-once).
SKILL_LISTING_POLICY = "pinned"
_MEDIA_TYPE = "application/json"

#: The task-local slot name the pack and the provider meet at.
ROSTER_SLOT = "skills.roster"

_ROSTER_HEADER = "The following skills are available via the `skill` tool:"
_ADDED_HEADER = "New skills are available via the `skill` tool:"


class SkillRoster:
    """One task's current fitted roster, as the latest build composed it —
    ``(name, line)`` pairs. Touched by the task's one driving thread at a time
    (the pack on the build, the provider on the intake that follows), so
    unlocked."""

    def __init__(self) -> None:
        self._lines: Optional[tuple[tuple[str, str], ...]] = None

    def note_built(self, lines: Sequence[tuple[str, str]]) -> None:
        """Record the roster the pack just composed."""
        self._lines = tuple(lines)

    @property
    def lines(self) -> Optional[tuple[tuple[str, str], ...]]:
        """The latest build's roster, or ``None`` before the first build."""
        return self._lines


def encode_listing(lines: Sequence[tuple[str, str]]) -> bytes:
    """One resident's body: ``{"names": [...], "lines": [...]}``, canonical
    JSON so the same roster always hashes the same."""
    body = {
        "names": [name for name, _ in lines],
        "lines": [line for _, line in lines],
    }
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def decode_listing(raw: bytes) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(names, lines)`` from a resident body written by
    :func:`encode_listing`."""
    body = json.loads(raw.decode("utf-8"))
    return tuple(body.get("names", ())), tuple(body.get("lines", ()))


def render_listing(name: str, lines: Sequence[str]) -> str:
    """The text one resident renders: a header, then one line per skill."""
    header = _ROSTER_HEADER if name == ROSTER_NAME else _ADDED_HEADER
    return "\n".join((header, *lines))


def _order(name: str) -> tuple[int, int, str]:
    """``roster`` first, then ``added-<k>`` by ``k`` — not by string order,
    which would put ``added-10`` before ``added-2``."""
    if name == ROSTER_NAME:
        return (0, 0, name)
    suffix = name[len(ADDED_PREFIX):]
    if name.startswith(ADDED_PREFIX) and suffix.isdigit():
        return (1, int(suffix), name)
    return (2, 0, name)


def skill_listing_content_kind() -> ContentKindSpec:
    """The listing's content-channel registry item.

    One resident renders as one user message, resolved from the store at the
    recorded hash, so the composed listing is a pure function of (folded
    state, store). A resident whose bytes are missing renders nothing. No
    ``hashes`` seam: the bytes are the recording, not a fingerprint of a
    source.
    """

    def _render(names: list[str], resolve: ContentResolve) -> RenderedContent:
        messages: list[Message] = []
        for name in sorted(names, key=_order):
            try:
                _, lines = decode_listing(resolve(SKILL_LISTING_KIND, name))
            except ContentNotFound:
                continue
            if not lines:
                continue
            messages.append(
                Message(
                    role="user",
                    content=[TextBlock(text=render_listing(name, lines))],
                )
            )
        return RenderedContent(messages=messages, selected_skills=[])

    return ContentKindSpec(
        kind=SKILL_LISTING_KIND,
        renderer=_render,
        policy=SKILL_LISTING_POLICY,
    )


def build_listing_init_hook(
    lines: Sequence[tuple[str, str]], content_store: ContentStore
) -> InitHook:
    """The pack's ``init`` hook: record ``roster`` once, when there is one.

    ``refresh=False``: once a task carries its roster, a rerun at a new goal or
    a subtask drain appends nothing even when the roster changed — the
    provider announces additions instead.
    """
    roster = tuple(lines)

    def _init(rec: SessionRecorder) -> None:
        if not roster:
            return
        ref = content_store.put(encode_listing(roster), media_type=_MEDIA_TYPE)
        rec.record_content(
            kind=SKILL_LISTING_KIND,
            name=ROSTER_NAME,
            version=SKILL_LISTING_VERSION,
            ref=ref,
            policy=SKILL_LISTING_POLICY,
            refresh=False,
        )

    return _init


def announced_skill_names(
    active: Mapping[str, Any], content_store: ContentStore
) -> frozenset[str]:
    """Every skill name the task's active listing residents carry.

    ``active`` is ``TaskState.active_content[SKILL_LISTING_KIND]`` (``name →
    hash``). A body missing from the store contributes nothing.
    """
    out: set[str] = set()
    for content_hash in active.values():
        if not content_hash:
            continue
        try:
            raw = content_store.get(
                ContentRef(hash=str(content_hash), size=0, media_type=_MEDIA_TYPE)
            )
        except ContentNotFound:
            continue
        names, _ = decode_listing(raw)
        out.update(names)
    return frozenset(out)


def skill_listing_provider(
    peek: Callable[[str, str], Optional[Any]], content_store: ContentStore
) -> Callable[[RecallView], tuple[ResidentActivation, ...]]:
    """The ``turn_intake`` provider recording ``added-<k>`` for the skills
    that joined the roster since every listing the task carries.

    Silent while the task has no ``roster`` resident (the init hook records
    that), when the host bound no roster for the task, and when nothing
    joined. Bound to the host's task-local ``peek`` (the current roster) and
    its ``content_store`` (to read the recorded listings back).
    """

    def provider(view: RecallView) -> tuple[ResidentActivation, ...]:
        task_id = view.task_id
        if not task_id:
            return ()
        roster = peek(str(task_id), ROSTER_SLOT)
        lines = getattr(roster, "lines", None)
        if not lines:
            return ()
        state: Any = view.task_state
        all_active = getattr(state, "active_content", None) or {}
        active = all_active.get(SKILL_LISTING_KIND, {})
        if ROSTER_NAME not in active:
            return ()
        announced = announced_skill_names(active, content_store)
        added = tuple((name, line) for name, line in lines if name not in announced)
        if not added:
            return ()
        k = sum(1 for name in active if name.startswith(ADDED_PREFIX))
        return (
            ResidentActivation(
                kind=SKILL_LISTING_KIND,
                name=f"{ADDED_PREFIX}{k}",
                body=encode_listing(added),
                version=SKILL_LISTING_VERSION,
                policy=SKILL_LISTING_POLICY,
                media_type=_MEDIA_TYPE,
                refresh=False,
            ),
        )

    return provider
