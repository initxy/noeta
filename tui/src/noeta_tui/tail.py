"""The transcript's scroll: it lays out only its last items, so a long
conversation costs what a short one does. Textual lays out the whole screen
whenever anything changes size (a key typed, a line streamed), and that
costs per widget laid out: every message kept as live widgets made each
step of a long task slower than the last.

While the person follows the end, items past the last :data:`SHOWN` are
hidden (still mounted, never laid out); a log shown at once mounts only its
last ones and keeps the rest unmounted. Scrolling up near the top brings
back :data:`REVEAL` more at a time, the view kept where it was."""

from __future__ import annotations

from collections.abc import Sequence

from textual.containers import VerticalScroll
from textual.widget import Widget

#: Items laid out at most while the end is followed.
SHOWN = 60
#: Items a log shown at once mounts: enough to fill a tall screen (more
#: come as the person scrolls up), few enough to show at once.
FIRST = 10
#: Items beyond :data:`SHOWN` before the oldest are hidden (hidden in steps,
#: not one per item added).
SLACK = 20
#: Items brought back at a time when the person scrolls up to them.
REVEAL = 12
#: Class of a mounted item hidden for being old.
OLDER = "-older"


class TailScroll(VerticalScroll):
    """A scroll of items, the newest last: items are added at the end
    (``mount``), or many at once (:meth:`show_all`)."""

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        #: Items never mounted, oldest first: all older than every mounted one.
        self.older: list[Widget] = []
        #: The mounted items at the front hidden for being old.
        self._hidden = 0
        self._revealing = False

    @property
    def has_older(self) -> bool:
        """There are items above the first one laid out."""
        return bool(self.older) or self._hidden_count() > 0

    @property
    def following(self) -> bool:
        """The person is at the end: what is added shows there. Scrolling
        up leaves it; scrolling back to the end follows again."""
        if self.is_anchored:
            return not self._anchor_released
        return self.scroll_y >= self.max_scroll_y - 1

    def stick(self) -> None:
        """After an item was added or grew: keep the end in view while the
        person follows it (never pull them back from what they scrolled
        up to), and hide what is now old. Anchored once the transcript is
        taller than the screen (Textual's anchor sinks a shorter one to
        the bottom of the screen)."""
        if self.is_anchored:
            if not self._anchor_released:
                self.trim()
            return
        if self.max_scroll_y > 0:
            self.anchor()
        else:
            self.scroll_end(animate=False)
        self.trim()

    async def clear(self) -> None:
        """Nothing shown, nothing older; not anchored."""
        self.anchor(False)
        self.older = []
        self._hidden = 0
        await self.remove_children()

    async def show_all(self, items: Sequence[Widget]) -> None:
        """``items`` after what is shown: the last :data:`FIRST` mounted,
        the rest kept to mount when scrolled up to (or all mounted when the
        transcript has hidden or unmounted items: they stay in order)."""
        items = list(items)
        if len(self.children) == 0 and not self.older and len(items) > FIRST:
            self.older = items[:-FIRST]
            items = items[-FIRST:]
        if items:
            await self.mount_all(items)
        self.call_after_refresh(self._fill)

    def trim(self) -> None:
        """Hide the oldest items laid out beyond :data:`SHOWN`, those two
        screens or more above the view (what is near it stays, so the view
        never runs short of what is above it)."""
        children = self.children
        hidden = self._hidden_count()
        if len(children) - hidden <= SHOWN + SLACK:
            return
        # Where each item ends, from the last layout (margins as the
        # vertical layout keeps them: the larger of two neighbours').
        far = self.max_scroll_y - 2 * self.size.height
        n, bottom, below = hidden, 0, 0
        while n < len(children) - SHOWN:
            child = children[n]
            bottom += max(below, child.styles.margin.top) + child.outer_size.height
            below = child.styles.margin.bottom
            if bottom >= far:
                break
            child.add_class(OLDER, update=False)  # a marker: no style of its own
            child.display = False
            n += 1
        self._hidden = n

    def _hidden_count(self) -> int:
        """The mounted items hidden at the front (counted again when one of
        them went away)."""
        children = self.children
        n = min(self._hidden, len(children))
        if (n and not children[n - 1].has_class(OLDER)) or \
                (n < len(children) and children[n].has_class(OLDER)):
            n = 0
            while n < len(children) and children[n].has_class(OLDER):
                n += 1
        self._hidden = n
        return n

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value < old_value:
            self._fill()

    def _fill(self) -> None:
        """Older items brought back while the view is within two screens
        of the top of what is laid out."""
        if not self._revealing and self.has_older and self.scroll_y <= 2 * self.size.height:
            self._revealing = True
            self.call_later(self.reveal)

    async def reveal(self) -> None:
        """Bring back :data:`REVEAL` older items above the first shown, the
        view kept on what it showed."""
        self._revealing = True
        try:
            before = self.virtual_size.height
            hidden = self._hidden_count()
            take = REVEAL
            with self.app.batch_update():
                n = min(take, hidden)
                for child in self.children[hidden - n:hidden]:
                    child.remove_class(OLDER, update=False)
                    child.display = True
                self._hidden = hidden - n
                take -= n
                if take and self.older and self._hidden == 0:
                    batch, self.older = self.older[-take:], self.older[:-take]
                    if self.children:
                        await self.mount_all(batch, before=0)
                    else:
                        await self.mount_all(batch)
        except BaseException:
            self._revealing = False
            raise

        def keep_place(last: int, passes: int) -> None:
            # What came in grows over a refresh or two (a reply draws its
            # pieces once mounted): the view moves down by each growth.
            grown = self.virtual_size.height - last
            # Followed, the end stays in view by itself (the anchor).
            if grown > 0 and not (self.is_anchored and not self._anchor_released):
                self.set_scroll(None, self.scroll_y + grown)
            if grown and passes:
                self.call_after_refresh(keep_place, self.virtual_size.height, passes - 1)
                return
            self._revealing = False
            self._fill()  # still near the top: more

        self.call_after_refresh(keep_place, before, 3)
