"""The one frontmatter splitter every markdown reader shares: CRLF line
endings and a byte order mark read the same as plain LF."""

from __future__ import annotations

import pytest

from noeta.agents.agent_files import parse_agent_file
from noeta.plugins.instructions.files import clean
from noeta.plugins.skills import _frontmatter as skill_frontmatter
from noeta.toolkit import frontmatter

RULE = "---\npaths: ['src/**/*.py']\n---\npython rule\n"


def _variants(text: str) -> list[str]:
    crlf = text.replace("\n", "\r\n")
    return [text, crlf, "﻿" + text, "﻿" + crlf]


@pytest.mark.parametrize("text", _variants(RULE))
def test_crlf_and_bom_split_like_lf(text: str) -> None:
    head, body = frontmatter.split(text)
    assert head == "paths: ['src/**/*.py']"
    assert body.replace("\r\n", "\n") == "python rule\n"


@pytest.mark.parametrize("text", _variants(RULE))
def test_a_crlf_or_bom_rule_keeps_its_paths(text: str) -> None:
    assert clean(text) == ("python rule", ("src/**/*.py",))


@pytest.mark.parametrize("text", _variants("---\nname: helper\n---\nYou help.\n"))
def test_a_crlf_or_bom_agent_file_reads(text: str) -> None:
    agent = parse_agent_file(text)
    assert agent.name == "helper"


@pytest.mark.parametrize("text", _variants("---\nname: s\ndescription: d\n---\nbody\n"))
def test_a_crlf_or_bom_skill_reads(text: str) -> None:
    assert skill_frontmatter.parse(text) == ({"name": "s", "description": "d"}, "body\n", [])


def test_no_frontmatter() -> None:
    assert frontmatter.split("just text\n---\n") == (None, "just text\n---\n")
    assert frontmatter.split("﻿just text") == (None, "just text")
    assert not frontmatter.opens("just text")


def test_an_unclosed_frontmatter_is_none() -> None:
    assert frontmatter.opens("---\nname: x\n")
    assert frontmatter.split("---\nname: x\n") == (None, "---\nname: x\n")


def test_empty_and_trailing_blank_delimiters() -> None:
    assert frontmatter.split("---\n---\nbody") == ("", "body")
    assert frontmatter.split("--- \na: 1\n---\t\nbody") == ("a: 1", "body")
    assert frontmatter.split("---\na: 1\n---") == ("a: 1", "")
