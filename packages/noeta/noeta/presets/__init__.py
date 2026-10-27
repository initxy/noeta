"""Ready-made agents. ``main`` is the general coding agent: Claude Code's
working style (``prompts/main.md``, byte for byte from 0.6)."""

from __future__ import annotations

from importlib import resources
from typing import Any

from noeta.agent import Agent
from noeta.ext.base import Extension
from noeta.ext.fs import FileTools
from noeta.ext.workspace import Environment


def load_prompt(name: str) -> str:
    return (resources.files("noeta.presets") / "prompts" / f"{name}.md").read_text("utf-8")


MAIN_PROMPT = load_prompt("main")


def main_agent(model: str, *, extensions: tuple[Extension, ...] = (), **kw: Any) -> Agent:
    return Agent(
        name=kw.pop("name", "main"),
        model=model,
        system_prompt=kw.pop("system_prompt", MAIN_PROMPT),
        extensions=(FileTools(), Environment(), *extensions),
        **kw,
    )
