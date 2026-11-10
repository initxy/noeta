"""A finished reply drawn as a few pieces of text instead of Textual's
Markdown widget tree. Textual's Markdown is a widget per block (a list a
widget per item and bullet, a table a widget per cell), and Textual lays
out every displayed widget whenever anything on the screen changes size:
the replies above the one being written made every streamed line and
every key slower. :func:`parts` reads the markdown once into ``Content``
(selectable as any text is) and :class:`Table` pieces (drawn as one)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from markdown_it.tree import SyntaxTreeNode
from rich import box
from rich.table import Table as RichTable
from rich.text import Text
from textual.content import Content
from textual.highlight import ANSIDarkHighlightTheme, ANSILightHighlightTheme, highlight
from textual.style import Style

from noeta_tui import render

#: Bullets per level of a nested list (Textual's).
BULLETS = ("• ", "▪ ", "‣ ", "⭑ ", "◦ ")
#: Inline code's colour on a dark and a light terminal (the transcript's CSS).
CODE = {True: "#b1b9f9", False: "#5769f7"}


@dataclass
class Table:
    """A table of a reply: its header cells and rows."""

    headers: list[Content] = field(default_factory=list)
    rows: list[list[Content]] = field(default_factory=list)

    def drawn(self) -> tuple[RichTable, str]:
        """The table to draw, and what it copies as: tab-separated rows."""
        return drawn_table(self.headers, self.rows)


def rich_text(content: Content) -> Text:
    """``content`` as Rich text, its styles kept (a table's cells)."""
    out = Text()
    for segment in content.render_segments(Style(), ""):
        out.append(segment.text, segment.style)
    return out


def drawn_table(headers: list[Content], rows: list[list[Content]]) -> tuple[RichTable, str]:
    table = RichTable(box=box.SQUARE, show_lines=True, border_style="bright_black",
                      header_style="bold", expand=False, highlight=False)
    for header in headers:
        table.add_column(rich_text(header), overflow="fold")
    for row in rows:
        table.add_row(*(rich_text(c) for c in row))
    tsv = "\n".join("\t".join(c.plain for c in row) for row in [headers, *rows])
    return table, tsv


def parts(markdown: str, *, dark: bool = True) -> list[Content | Table]:
    """The reply's blocks, a blank line apart, as text pieces; each table a
    piece of its own."""
    from noeta_tui.widgets import _markdown_parser

    root = SyntaxTreeNode(_markdown_parser().parse(render.clean(markdown)))
    out: list[Content | Table] = []
    texts: list[Content] = []
    for node in root.children:
        if node.type == "table":
            if texts:
                out.append(Content("\n\n").join(texts))
                texts = []
            out.append(_table(node, dark))
        else:
            block = _block(node, dark, 0)
            if block is not None:
                texts.append(block)
    if texts:
        out.append(Content("\n\n").join(texts))
    return out


def _block(node: SyntaxTreeNode, dark: bool, depth: int) -> Content | None:
    kind = node.type
    if kind == "paragraph":
        return _inline(node.children[0], dark) if node.children else Content("")
    if kind == "heading":
        return _inline(node.children[0], dark).stylize("bold") if node.children else None
    if kind in ("bullet_list", "ordered_list"):
        return _list(node, dark, depth)
    if kind in ("fence", "code_block"):
        theme = ANSIDarkHighlightTheme if dark else ANSILightHighlightTheme
        code = highlight(node.content.rstrip("\n"), language=node.info or None, theme=theme)
        return Content("\n").join(Content("  ") + line for line in code.split("\n"))
    if kind == "blockquote":
        inner = [b for child in node.children if (b := _block(child, dark, depth)) is not None]
        lines = Content("\n").join(inner).split("\n")
        return Content("\n").join(Content.styled("▌ ", "dim") + line for line in lines)
    if kind == "hr":
        return Content.styled("─" * 40, "dim")
    if kind == "table":  # inside a list or a quote: its cells, a row to a line
        return Content(_table(node, dark).drawn()[1])
    if kind in ("html_block",):
        return Content(node.content.rstrip("\n"))
    return None


def _list(node: SyntaxTreeNode, dark: bool, depth: int) -> Content:
    start = int(node.attrs.get("start") or 1) if node.type == "ordered_list" else 1
    lines: list[Content] = []
    for n, item in enumerate(node.children):
        mark = f"{start + n}. " if node.type == "ordered_list" else BULLETS[depth % len(BULLETS)]
        inner = [b for child in item.children
                 if (b := _block(child, dark, depth + 1)) is not None]
        body = Content("\n").join(inner).split("\n") if inner else [Content("")]
        lines.append(Content.styled(mark, "bold") + body[0])
        lines += [Content(" " * len(mark)) + line for line in body[1:]]
    return Content("\n").join(lines)


def _table(node: SyntaxTreeNode, dark: bool) -> Table:
    table = Table()
    for section in node.children:
        for tr in section.children:
            cells = [_inline(c.children[0], dark) if c.children else Content("")
                     for c in tr.children]
            if section.type == "thead":
                table.headers = cells
            else:
                table.rows.append(cells)
    return table


def _inline(node: SyntaxTreeNode, dark: bool) -> Content:
    pieces: list[tuple[str, str]] = []
    for text, style in _spans(node, dark, ()):
        pieces.append((text, " ".join(style)))
    return Content.assemble(*pieces)


def _spans(node: SyntaxTreeNode, dark: bool, style: tuple[str, ...]
           ) -> Iterator[tuple[str, tuple[str, ...]]]:
    for child in node.children:
        kind = child.type
        if kind == "text":
            yield child.content, style
        elif kind == "code_inline":
            yield child.content, (*style, CODE[dark])
        elif kind == "softbreak":  # as Textual's Markdown (and HTML) show it
            yield " ", style
        elif kind == "hardbreak":
            yield "\n", style
        elif kind == "strong":
            yield from _spans(child, dark, (*style, "bold"))
        elif kind == "em":
            yield from _spans(child, dark, (*style, "italic"))
        elif kind == "s":
            yield from _spans(child, dark, (*style, "strike"))
        elif kind == "link":
            yield from _spans(child, dark, (*style, "underline"))
        elif kind == "image":
            yield str(child.attrs.get("alt") or child.content or ""), style
        elif kind == "html_inline":
            yield child.content, style
        elif child.children:
            yield from _spans(child, dark, style)
        else:
            yield child.content, style
