"""Agent files: the frontmatter, the body kept byte for byte, plugins by
entry-point name (a third-party one the same as a built-in), sharing."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest

from noeta.core import registry
from noeta.core.agent_files import (
    AgentFileError,
    build_agents,
    parse_agent_file,
    read_agent_dir,
)
from noeta.core.extension import Extension

FULL = """---
name: reviewer
description: Reviews a diff.
model: some-model
one_shot: true
effort: high
tools: [Read, Grep]
plugins:
  - fs: {tools: [Read, Grep]}
  - instructions
color: blue
---
You review.

Twice.
"""


def test_parse_keeps_the_body_and_reads_every_field() -> None:
    f = parse_agent_file(FULL, source="x")
    assert f.name == "reviewer" and f.description == "Reviews a diff."
    assert f.prompt == "You review.\n\nTwice.\n"
    assert f.model == "some-model" and f.one_shot and f.effort == "high"
    assert f.tools == ("Read", "Grep")
    assert f.plugins == (("fs", {"tools": ["Read", "Grep"]}), ("instructions", {}))
    assert f.extra == {"color": "blue"}


def test_claude_code_agent_file() -> None:
    f = parse_agent_file("---\nname: helper\ndescription: Helps.\ntools: Read, Bash\n"
                         "model: sonnet\n---\nHelp.\n")
    assert f.tools == ("Read", "Bash") and f.model is None and f.plugins is None


@pytest.mark.parametrize("text", [
    "no frontmatter\n",
    "---\nname: x\n",
    "---\n: [\n---\nbody",
    "---\n- a\n---\nbody",
    "---\ndescription: d\n---\nbody",
    "---\nname: x\nplugins: fs\n---\n",
    "---\nname: x\nplugins: [{fs: [1]}]\n---\n",
    "---\nname: x\none_shot: maybe\n---\n",
])
def test_bad_files(text: str) -> None:
    with pytest.raises(AgentFileError):
        parse_agent_file(text)


def test_read_agent_dir_skips_bad_files(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("---\ndescription: d\n---\nP\n")
    (tmp_path / "bad.md").write_text("nothing\n")
    [f] = read_agent_dir(tmp_path)
    assert f.name == "good"  # the file name when the frontmatter has none
    assert read_agent_dir(tmp_path / "missing") == []


class Greeter(Extension):
    name = "greeter"

    def __init__(self, greeting: str = "hi") -> None:
        self.greeting = greeting


def test_a_third_party_plugin_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = {"greeter": EntryPoint("greeter", f"{__name__}:Greeter", registry.PLUGINS)}
    real = registry._group

    def group(name: str) -> dict[str, Any]:
        return eps if name == registry.PLUGINS else real(name)

    monkeypatch.setattr(registry, "_group", group)
    files = [parse_agent_file("---\nname: a\nplugins: [{greeter: {greeting: hello}}]\n---\nP\n"),
             parse_agent_file("---\nname: b\nplugins: [{greeter: {greeting: hello}}]\n---\nP\n"),
             parse_agent_file("---\nname: c\nplugins: [greeter]\n---\nP\n")]
    a, b, c = build_agents(files, model="m", config={"greeter": {"greeting": "hey"}})
    assert a.extensions[0] is b.extensions[0]  # same arguments, one instance
    assert a.extensions[0].greeting == "hello"  # type: ignore[attr-defined]
    assert c.extensions[0].greeting == "hey"  # type: ignore[attr-defined]
    with pytest.raises(registry.UnknownName, match="available: greeter"):
        build_agents([parse_agent_file("---\nname: d\nplugins: [nope]\n---\n")], model="m")


def test_disabled_instances_defaults_and_models() -> None:
    shared = Greeter()
    files = [parse_agent_file("---\nname: a\nplugins: [greeter, fs]\n---\nP\n"),
             parse_agent_file("---\nname: b\nmodel: other\n---\nQ\n")]
    a, b = build_agents(files, model="m", instances={"greeter": shared}, disabled={"fs"},
                        default_plugins=[("greeter", {})])
    assert list(a.extensions) == [shared] and a.model == "m"
    assert list(b.extensions) == [shared] and b.model == "other"
