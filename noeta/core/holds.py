"""A worker's hold on a task tree, as what holds it sees it
(``Runtime.hold``, ``ToolContext.hold``)."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger("noeta.core.runner")


@dataclass(eq=False)
class TreeHold:
    """This worker's hold on one task tree (``Store.hold``), shared by
    everything in the process that holds the tree."""

    root_id: str
    worker: str
    epoch: int
    #: How long the hold lasts without a renewal before it may be reaped.
    seconds: float
    #: Reaped, or given up on the way out: what it held must stop.
    lost: bool = False
    renewed: float = field(default_factory=time.monotonic)
    count: int = 0
    _on_lost: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def fresh(self) -> bool:
        """Still this worker's as far as it can tell: not lost, and renewed
        recently enough that no other worker can have reaped it yet."""
        return not self.lost and time.monotonic() - self.renewed < self.seconds

    def on_lost(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` once the hold is lost (at once if it is)."""
        if self.lost:
            callback()
        else:
            self._on_lost.append(callback)

    def mark_lost(self) -> None:
        if self.lost:
            return
        self.lost = True
        callbacks, self._on_lost = self._on_lost, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.exception("tree %s: a lost-hold callback failed", self.root_id)


class Release:
    """What ``Runtime.hold`` returns: awaiting it gives this holder's share
    of the hold back (once); :attr:`tree` is the hold itself."""

    def __init__(self, tree: TreeHold, release: Callable[[], Awaitable[None]]) -> None:
        self.tree = tree
        self._release = release

    async def __call__(self) -> None:
        await self._release()
