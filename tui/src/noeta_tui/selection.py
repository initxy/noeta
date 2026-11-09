"""What a mouse selection over the transcript copies. Textual selects the text
of ``Text`` and ``Content``; :class:`HangingContent` is ``render.Hanging``'s
form for it (a tool call's line, its result), whose gutter (``● ``,
``  ⎿  ``) a selection leaves out, as it leaves out :class:`Unselectable`
marks. :func:`selected_text` joins what is selected the way the screen
shows it."""

from __future__ import annotations

from rich.segment import Segment
from textual.content import Content
from textual.css.styles import RulesMap
from textual.screen import Screen
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions
from textual.widget import Widget
from textual.widgets import Static

#: Cells a hanging line's text keeps however narrow the widget (as ``render.Hanging``).
MIN_TEXT_WIDTH = 8


class Unselectable(Static):
    """A mark beside the text (a reply's bullet, the logo): not copied."""

    ALLOW_SELECT = False


class HangingContent(Content):
    """Text whose lines keep their first ``indent`` cells (the gutter) apart:
    a long line wraps under its own text. A selection covers, and copies, the
    text past the gutter (``str()``); ``plain`` keeps the gutter."""

    def __init__(self, text: Content, indent: int) -> None:
        super().__init__(text.plain, list(text.spans), strip_control_codes=False)
        lines = text.split("\n", allow_blank=True)
        self.indent = indent
        self.gutters = [line[:indent] for line in lines]
        self.text = Content("\n").join(line[indent:].expand_tabs() for line in lines)

    def __str__(self) -> str:
        return self.text.plain

    def _text_width(self, width: int) -> int:
        return max(width - self.indent, MIN_TEXT_WIDTH)

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return self.indent + self.text.get_optimal_width(rules, container_width)

    def get_height(self, rules: RulesMap, width: int) -> int:
        return self.text.get_height(rules, self._text_width(width))

    def render_strips(self, width: int, height: int | None, style: Style,
                      options: RenderOptions) -> list[Strip]:
        if not width:
            return []
        out: list[Strip] = []
        line = -1
        for strip in self.text.render_strips(self._text_width(width), None, style, options):
            x, y = _offset(strip) or (0, line + 1)  # an empty line's row has no offsets
            gutter = Content()  # a row the line wrapped onto
            if y != line:  # the line's first row
                line = y
                gutter = self.gutters[line] if line < len(self.gutters) else gutter
            out.append(Strip.join([self._gutter(gutter, style, options, x, line), strip]))
        return out[:height] if height is not None else out

    def _gutter(self, gutter: Content, style: Style, options: RenderOptions, x: int,
                y: int) -> Strip:
        """The gutter's cells, each pointing at the row's first character: a
        selection that starts (or ends) in it starts at the text."""
        cells = [Segment(char, (style + piece).rich_style_with_offset(x, y))
                 for text, piece in gutter.render(style, end="", parse_style=options.get_style)
                 for char in text]
        pad = self.indent - gutter.cell_length
        if pad > 0:
            cells += [Segment(" ", style.rich_style_with_offset(x, y))] * pad
        return Strip(cells)


def _offset(strip: Strip) -> tuple[int, int] | None:
    """Where in the text (column, line) the row starts (``None``: it has no text)."""
    for segment in strip:
        meta = segment.style.meta if segment.style is not None else {}
        x, y = meta.get("offset") or (None, None)
        if x is not None and y is not None:
            return int(x), int(y)
    return None


def selected_text(screen: Screen[object]) -> str:
    """The screen's selection as text: each widget's on lines of its own, a
    blank line where the screen shows one between two, a grid's cells (a
    Markdown table's) one row to a line, tab-separated."""
    out: list[str] = []
    previous: Widget | None = None
    for widget, selection in screen.selections.items():
        if not widget.is_attached:
            continue
        found = widget.get_selection(selection)
        if found is None:
            continue
        text, end = found
        if out and previous is not None and _gap_between(previous, widget):
            out.append("\n")
        if _ends_grid_row(widget) is False:
            end = "\t"
        out += [text, end]
        previous = widget
    return "".join(out).rstrip("\n")


def _ends_grid_row(widget: Widget) -> bool | None:
    """Whether ``widget`` is the last cell of its grid row (``None``: it is not in a grid)."""
    parent = widget.parent
    if not isinstance(parent, Widget) or parent.styles.layout is None \
            or parent.styles.layout.name != "grid":
        return None
    columns = parent.styles.grid_size_columns or 1
    return (parent.children.index(widget) + 1) % columns == 0


def _gap_between(above: Widget, below: Widget) -> bool:
    """The screen shows a blank row between the two (not two rows of one grid)."""
    if _ends_grid_row(below) is not None and below.parent is above.parent:
        return False
    return below.region.y > above.region.bottom
