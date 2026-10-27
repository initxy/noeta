"""Turning a fetched body into text: the media-type checks, charset
decoding, and the small dependency-free HTML-to-Markdown heuristic WebFetch
used in 0.6 (deterministic for identical bytes)."""

from __future__ import annotations

import html as _html
import re

__all__ = [
    "FetchedPage",
    "check_text_media",
    "collapse_whitespace",
    "decode_body",
    "html_to_markdown",
    "media_type_of",
    "render",
]

_ALL_WS_RE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """Every whitespace run, newlines included, to one space: server-supplied
    titles and snippets never open a line of a template."""
    return _ALL_WS_RE.sub(" ", text).strip()


_STRIP_BLOCKS_RE = re.compile(
    r"<(script|style|title|head|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_LIST_ITEM_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_BLOCK_BREAK_RE = re.compile(
    r"</?(p|div|section|article|br|tr|table|ul|ol|blockquote|pre)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\f\v]+")
_MANY_BLANKS_RE = re.compile(r"\n{3,}")


def _collapse_inline_ws(text: str) -> str:
    return _INLINE_WS_RE.sub(" ", text).strip()


def extract_title(raw: str) -> str:
    match = _TITLE_RE.search(raw)
    return collapse_whitespace(_html.unescape(match.group(1))) if match else ""


def _strip_tags_text(fragment: str) -> str:
    return _collapse_inline_ws(_html.unescape(_TAG_RE.sub(" ", fragment)))


def html_to_markdown(raw: str) -> str:
    body = _STRIP_BLOCKS_RE.sub("\n", raw)

    def _heading(m: re.Match[str]) -> str:
        return f"\n\n{'#' * int(m.group(1))} {_strip_tags_text(m.group(2))}\n\n"

    body = _HEADING_RE.sub(_heading, body)

    def _anchor(m: re.Match[str]) -> str:
        href = _collapse_inline_ws(_html.unescape(m.group(1)))
        text = _strip_tags_text(m.group(2))
        if not text:
            return href
        if not href:
            return text
        return f"[{text}]({href})"

    body = _ANCHOR_RE.sub(_anchor, body)
    body = _LIST_ITEM_RE.sub(lambda m: f"\n- {_strip_tags_text(m.group(1))}\n", body)
    body = _BLOCK_BREAK_RE.sub("\n\n", body)
    body = _TAG_RE.sub(" ", body)
    body = _html.unescape(body)
    out = "\n".join(_collapse_inline_ws(line) for line in body.splitlines())
    return _MANY_BLANKS_RE.sub("\n\n", out).strip()


class FetchedPage(str):
    """A decoded body that also carries its ``Content-Type`` (``""`` when the
    server sent none)."""

    content_type: str

    def __new__(cls, text: str, content_type: str = "") -> FetchedPage:
        page = super().__new__(cls, text)
        page.content_type = content_type
        return page


_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_MEDIA_TYPES = frozenset({
    "application/json", "application/xml", "application/javascript",
    "application/ecmascript", "application/x-javascript", "application/yaml",
    "application/x-yaml", "application/toml", "application/sql", "application/graphql",
    "application/x-sh", "application/x-httpd-php",
})
_CHARSET_PARAM_RE = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", re.IGNORECASE)


def media_type_of(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_text_media(media: str) -> bool:
    return (media.startswith("text/") or media in _TEXT_MEDIA_TYPES
            or media.endswith("+json") or media.endswith("+xml"))


def check_text_media(url: str, content_type: str) -> None:
    """Raise when ``content_type`` names a body WebFetch cannot read."""
    media = media_type_of(content_type)
    if not media or media in _HTML_MEDIA_TYPES or _is_text_media(media):
        return
    raise ValueError(
        f"{url} returned {media} content, which WebFetch cannot read — it "
        "reads HTML and text (text/*, JSON, XML)"
    )


def decode_body(raw: bytes, content_type: str) -> str:
    match = _CHARSET_PARAM_RE.search(content_type)
    charset = match.group(1) if match else ""
    if not charset:
        media = media_type_of(content_type)
        if not media or media in _HTML_MEDIA_TYPES:
            meta = _META_CHARSET_RE.search(raw[:4096])
            charset = meta.group(1).decode("ascii", "replace") if meta else ""
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def render(fetched: str | FetchedPage) -> tuple[str, str]:
    """``(title, text)``: HTML through :func:`html_to_markdown`, other text
    as-is. A bare ``str`` is HTML; a body with no type is sniffed."""
    text = str(fetched)
    if not isinstance(fetched, FetchedPage):
        return extract_title(text), html_to_markdown(text)
    media = media_type_of(fetched.content_type)
    if media in _HTML_MEDIA_TYPES or (not media and text.lstrip()[:1] == "<"):
        return extract_title(text), html_to_markdown(text)
    return "", text.strip()
