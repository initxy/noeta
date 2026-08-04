"""The shared tool-result text rendering
(``noeta.builtins.providers.impl.codecs.render_tool_result_body``) and the
session read registry / middle-elision helpers that ride the same slice.

All three provider adapters render a ``ToolResultBlock``'s model-facing text
through ``render_tool_result_body``: string outputs verbatim, structured
outputs JSON-encoded without ASCII escaping, a failed call's error text
leading the body (OpenAI-shaped wires have no error flag, so the text is the
only channel that survives every provider).
"""

from __future__ import annotations

from noeta.builtins.providers.impl.codecs import render_tool_result_body
from noeta.runtime.tool import InMemoryFileReadRegistry
from noeta.tools.limits import elide_middle


# ---------------------------------------------------------------------------
# render_tool_result_body
# ---------------------------------------------------------------------------


def test_string_output_passes_through_verbatim() -> None:
    text = "     1\tline one\n     2\tline two"
    assert render_tool_result_body(text, None) == text


def test_dict_output_json_encodes_without_ascii_escapes() -> None:
    rendered = render_tool_result_body({"note": "中文内容"}, None)
    # ensure_ascii=False: CJK stays raw UTF-8, not \uXXXX escapes.
    assert "中文内容" in rendered
    assert "\\u" not in rendered


def test_error_text_leads_the_body() -> None:
    assert render_tool_result_body("partial", "boom") == "boom\npartial"


def test_error_alone_when_output_empty() -> None:
    assert render_tool_result_body("", "boom") == "boom"
    assert render_tool_result_body(None, "boom") == "boom\nnull"


# ---------------------------------------------------------------------------
# InMemoryFileReadRegistry
# ---------------------------------------------------------------------------


def test_registry_records_and_returns_latest_digest() -> None:
    reg = InMemoryFileReadRegistry()
    assert reg.digest("/w/a.py") is None
    reg.record("/w/a.py", "d1")
    assert reg.digest("/w/a.py") == "d1"
    reg.record("/w/a.py", "d2")
    assert reg.digest("/w/a.py") == "d2"


def test_registry_keys_paths_independently() -> None:
    reg = InMemoryFileReadRegistry()
    reg.record("/w/a.py", "d1")
    assert reg.digest("/w/b.py") is None


# ---------------------------------------------------------------------------
# elide_middle
# ---------------------------------------------------------------------------


def test_elide_middle_under_cap_is_identity() -> None:
    assert elide_middle("short", 30000) == "short"


def test_elide_middle_keeps_head_and_tail_and_names_dropped_count() -> None:
    value = "H" * 100 + "M" * 100 + "T" * 100
    out = elide_middle(value, 100)
    assert out.startswith("H" * 50)
    assert out.endswith("T" * 50)
    assert "[200 chars truncated]" in out
