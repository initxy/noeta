"""Strict-minimal SKILL.md frontmatter parser tests (issue 21).

Covers the parser only; SkillIndexer-level skip + log behaviour lives
in ``test_skill_indexer.py``.
"""

from __future__ import annotations

import pytest

from noeta.context.skills._frontmatter import (
    FrontmatterError,
    parse,
)


def _wrap(fm: str, body: str = "") -> str:
    return f"---\n{fm}---\n{body}"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parse_minimal_required_fields() -> None:
    text = _wrap("name: hello\ndescription: greeting skill\n", "hi there\n")
    fields, body, warnings = parse(text)
    assert fields == {"name": "hello", "description": "greeting skill"}
    assert body == "hi there\n"
    assert warnings == []


def test_parse_all_known_fields() -> None:
    text = _wrap(
        "name: x\n"
        "description: a thing\n"
        "version: 2\n"
        "priority: 50\n",
        "body content\n",
    )
    fields, body, _ = parse(text)
    assert fields == {
        "name": "x",
        "description": "a thing",
        "version": "2",
        "priority": "50",
    }
    assert body == "body content\n"


def test_parse_value_is_trimmed() -> None:
    text = _wrap("name:    spaced   \ndescription:   tabbed\t\n")
    fields, _, _ = parse(text)
    assert fields == {"name": "spaced", "description": "tabbed"}


def test_parse_blank_lines_in_frontmatter_are_skipped() -> None:
    text = _wrap(
        "name: x\n"
        "\n"
        "description: y\n"
        "   \n"
        "version: 1\n",
    )
    fields, _, _ = parse(text)
    assert fields == {"name": "x", "description": "y", "version": "1"}


def test_parse_crlf_line_endings_normalise_body_to_lf() -> None:
    text = "---\r\nname: x\r\ndescription: y\r\n---\r\nhello\r\nworld\r\n"
    fields, body, _ = parse(text)
    assert fields == {"name": "x", "description": "y"}
    assert body == "hello\nworld\n"


def test_parse_mixed_crlf_and_lf_body_preserves_normalisation() -> None:
    text = "---\nname: x\ndescription: y\n---\nfirst\r\nsecond\nthird\r\n"
    _, body, _ = parse(text)
    assert body == "first\nsecond\nthird\n"


def test_parse_unicode_body_preserved() -> None:
    text = _wrap("name: x\ndescription: y\n", "日本語 ☃\n")
    _, body, _ = parse(text)
    assert body == "日本語 ☃\n"


def test_parse_empty_body_after_terminator() -> None:
    text = "---\nname: x\ndescription: y\n---\n"
    _, body, _ = parse(text)
    assert body == ""


def test_parse_body_trailing_whitespace_and_blanks_preserved() -> None:
    text = _wrap(
        "name: x\ndescription: y\n",
        "  trailing spaces  \n\n  another  \n",
    )
    _, body, _ = parse(text)
    assert body == "  trailing spaces  \n\n  another  \n"


# ---------------------------------------------------------------------------
# Duplicate key — warning + last-wins
# ---------------------------------------------------------------------------


def test_parse_duplicate_key_takes_last_and_warns() -> None:
    text = _wrap(
        "name: first\n"
        "description: shared\n"
        "name: second\n",
    )
    fields, _, warnings = parse(text)
    assert fields["name"] == "second"
    assert any("duplicate" in w and "name" in w for w in warnings)


# ---------------------------------------------------------------------------
# Strict rejection
# ---------------------------------------------------------------------------


def test_parse_missing_leading_delimiter_raises() -> None:
    text = "name: x\ndescription: y\n"
    with pytest.raises(FrontmatterError, match="leading"):
        parse(text)


def test_parse_missing_terminating_delimiter_raises() -> None:
    text = "---\nname: x\ndescription: y\n"
    with pytest.raises(FrontmatterError, match="terminating"):
        parse(text)


def test_parse_malformed_line_raises() -> None:
    text = _wrap("name: x\nnot-a-key-value-line\n")
    with pytest.raises(FrontmatterError, match="invalid frontmatter line"):
        parse(text)


def test_parse_uppercase_key_raises() -> None:
    text = _wrap("Name: x\ndescription: y\n")
    with pytest.raises(FrontmatterError):
        parse(text)


# ---------------------------------------------------------------------------
# 4.5-I5: unknown / hyphenated keys are tolerated, not fatal
# ---------------------------------------------------------------------------
# These three cases were strict skips through Phase 1 (rev3 NB3) and are
# deliberately INVERTED in I5 so real public skills carrying arbitrary
# extra keys load unchanged. The parser now returns every key in
# ``fields``; SkillIndexer splits the semantic keys from opaque metadata.


def test_parse_unknown_key_is_tolerated() -> None:
    """Unknown key no longer invalidates the file (was strict skip)."""
    text = _wrap("name: x\ndescription: y\nextra: nope\n")
    fields, _, _ = parse(text)
    assert fields == {"name": "x", "description": "y", "extra": "nope"}


def test_parse_typo_key_is_tolerated_now_silent() -> None:
    """The cost of tolerating real skills: a typo of a known key
    (``descrption:``) silently becomes a separate (metadata) key rather
    than erroring. The Indexer then skips for a *missing* ``description``,
    which is the documented I5 trade-off."""
    text = _wrap("name: x\ndescrption: typo\n")
    fields, _, _ = parse(text)
    assert fields == {"name": "x", "descrption": "typo"}
    assert "description" not in fields


def test_parse_key_with_hyphen_is_tolerated() -> None:
    """Hyphenated keys (``argument-hint``, ``allowed-tools``) now parse;
    the key regex is ``^[a-z][a-z0-9_-]*$`` (underscore kept, hyphen
    added)."""
    text = _wrap(
        "name: x\n"
        "description: y\n"
        "argument-hint: <arg>\n"
        "allowed-tools: [Read, Bash]\n"
    )
    fields, _, _ = parse(text)
    assert fields == {
        "name": "x",
        "description": "y",
        "argument-hint": "<arg>",
        # inline list captured verbatim as an opaque string (no YAML parse)
        "allowed-tools": "[Read, Bash]",
    }


def test_parse_underscore_key_still_tolerated() -> None:
    """Underscore was always allowed and must stay allowed (architect P1)."""
    text = _wrap("name: x\ndescription: y\nmax_steps: 5\n")
    fields, _, _ = parse(text)
    assert fields["max_steps"] == "5"


def test_parse_nested_unknown_metadata_block_is_tolerated() -> None:
    """Unknown keys may carry indented YAML-ish blocks. Noeta treats the
    block as an opaque string so richer third-party metadata does not
    invalidate the skill."""
    text = _wrap(
        "name: x\n"
        "description: y\n"
        "metadata:\n"
        "  requires:\n"
        "    bins: [\"lark-cli\"]\n"
        "  cliHelp: \"lark-cli docs --help\"\n"
    )
    fields, _, _ = parse(text)
    assert fields == {
        "name": "x",
        "description": "y",
        "metadata": (
            "requires:\n"
            "  bins: [\"lark-cli\"]\n"
            "cliHelp: \"lark-cli docs --help\""
        ),
    }


def test_parse_folded_multiline_scalar_for_description() -> None:
    """Some real skills use YAML folded scalars for long descriptions."""
    text = _wrap(
        "name: whiteboard\n"
        "description: >\n"
        "  first sentence.\n"
        "  second sentence.\n"
        "version: 2\n"
    )
    fields, _, _ = parse(text)
    assert fields == {
        "name": "whiteboard",
        "description": "first sentence. second sentence.",
        "version": "2",
    }
