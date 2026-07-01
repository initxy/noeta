"""Content-channel registry — the SDK's swappable kind table.

The resident-content channel is the runtime-generic
mechanism issue 02 built: activations live in the generic map
``TaskState.active_content`` (kind → name tuple), content hashes travel on
``ContextContentRecorded`` events, and the ``ContentHashesFn`` seam
resolves ``(kind, name) → (version, hash)``. What a kind *means* —
how its content is loaded, what shape it renders to, which drift policy
its recordings carry — is deliberately NOT runtime knowledge. This
module is where that knowledge lives: one :class:`ContentKindSpec` per
kind, collected into a :class:`ContentChannelRegistry` that
:class:`noeta.context.composer.ThreeSegmentComposer` consults when it
renders the ``semi_stable`` segment.

Adding a new content kind = registering one spec (a renderer + a hash
resolver + a policy). The runtime needs zero changes — that is the D2
acceptance line.

Red line: a renderer is a pure function of the *names* fold
derived from the ledger — it may close over preloaded state (a
``SkillRegistry``, an in-memory index) but must NOT fetch from external
sources at compose time. Same ledger ⇒ same bytes keeps the
``semi_stable`` segment cache-friendly (a stable rendering does not bust
the provider prompt cache between steps).

The renderer output reuses :class:`noeta.context.composer.RenderedSkills`
(``messages`` + post-resolve ``selected_skills`` + optional
``retrieved_resources``) — the shape predates the generic channel and is
field-named after its first resident; renaming it is deferred to the
issue-07 generation switch so this batch stays purely additive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from noeta.context.composer import RenderedSkills, SkillRenderer
from noeta.protocols.events import CONTENT_DRIFT_POLICIES


__all__ = [
    "ContentChannelRegistry",
    "ContentHashes",
    "ContentKindSpec",
    "ContentRenderer",
]


#: A kind renderer: post-fold active names → rendered semi_stable
#: content. Alias of the historical ``SkillRenderer`` shape — the skill
#: renderer was the first resident and the generic channel adopts its
#: contract unchanged (the type only gets renamed, not reshaped, until issue 07).
ContentRenderer = SkillRenderer

#: A kind's hash resolver: name → ``(declared_version, content_hash)``,
#: ``None`` for names the host does not know (no ``content_hash`` provenance
#: is then recorded for that name).
ContentHashes = Callable[[str], Optional[tuple[str, str]]]


@dataclass(frozen=True, slots=True)
class ContentKindSpec:
    """One registered content kind: how to render + how to hash.

    * ``kind`` — the channel key, matching ``TaskState.active_content``
      and ``ContextContentRecorded.kind``.
    * ``renderer`` — the render rule for the ``semi_stable`` segment.
    * ``hashes`` — optional ``content_hash`` resolver; ``None`` means the
      kind records no provenance for that name.
    * ``policy`` — the drift policy this kind's recordings carry
      (``pinned`` vs ``evolving``), recorded as descriptive provenance
      on each event. It travels WITH the recording rather than in a kind
      table of its own; the drift-comparison consumer that once read it
      has been retired, so the policy is recorded but no longer enforced.
    """

    kind: str
    renderer: ContentRenderer
    hashes: Optional[ContentHashes] = None
    policy: str = "pinned"

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("ContentKindSpec.kind must be non-empty")
        if self.policy not in CONTENT_DRIFT_POLICIES:
            raise ValueError(
                f"ContentKindSpec.policy {self.policy!r} unknown; expected "
                f"one of {CONTENT_DRIFT_POLICIES}"
            )


class ContentChannelRegistry:
    """Immutable kind → :class:`ContentKindSpec` table.

    Satisfies the :class:`noeta.context.composer.ContentRenderers`
    protocol so it plugs straight into ``ThreeSegmentComposer``.
    ``kinds()`` preserves registration order — that order IS the
    ``semi_stable`` layout, so hosts construct the registry
    deterministically (a stable layout keeps the segment cache-friendly,
    exactly like the system prompt or the tool set).
    """

    def __init__(self, items: Iterable[ContentKindSpec]) -> None:
        table: dict[str, ContentKindSpec] = {}
        for item in items:
            if item.kind in table:
                raise ValueError(
                    f"duplicate content kind {item.kind!r} in registry"
                )
            table[item.kind] = item
        self._items = table

    def kinds(self) -> tuple[str, ...]:
        return tuple(self._items)

    def get(self, kind: str) -> Optional[ContentKindSpec]:
        return self._items.get(kind)

    def render(self, kind: str, names: list[str]) -> RenderedSkills:
        item = self._items.get(kind)
        if item is None:
            raise KeyError(f"content kind {kind!r} is not registered")
        return item.renderer(names)

    def content_hashes(self) -> Callable[[str, str], Optional[tuple[str, str]]]:
        """Build the generic ``(kind, name) → (version, hash)`` resolver
        (the ``ContentHashesFn`` seam shape) over the registered specs.

        Unknown kinds and items without a ``hashes`` fn resolve to
        ``None`` — the no-provenance path (no ``content_hash`` recorded,
        never a crash).
        """
        items = self._items

        def _resolve(kind: str, name: str) -> Optional[tuple[str, str]]:
            item = items.get(kind)
            if item is None or item.hashes is None:
                return None
            return item.hashes(name)

        return _resolve
