"""Building the model request from the folded state: one pure function, so a
recorded log always produces the same request bytes (and the prompt cache
keeps hitting)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from noeta.core.extension import ContextPart, ToolEntry
from noeta.core.loop.state import TaskState
from noeta.core.tool import Tool, tool_flag
from noeta.core.types import ModelRequest, SystemPart, ToolSpec, dumps


def build_request(
    state: TaskState,
    *,
    model: str,
    system_prompt: str,
    parts: Sequence[ContextPart],
    tools: Sequence[Tool],
    withheld: Mapping[str, str] | None = None,
    entries: Sequence[ToolEntry] = (),
    plain: Collection[str] = (),
    identity: str = "",
    effort: str | None = None,
    max_tokens: int | None = None,
    output_schema: dict[str, Any] | None = None,
) -> ModelRequest:
    """The system prompt is two cached blocks, as Claude Code sends it: the
    identity line, then the agent's prompt with the extensions' parts."""
    system: list[SystemPart] = [SystemPart(text=identity, cache=True)] if identity else []
    text = system_prompt
    for part in sorted(parts, key=lambda p: p.order):
        text = _place(text, part)
    if text:
        system.append(SystemPart(text=text, cache=True))
    messages = list(state.messages)
    return ModelRequest(
        model=model,
        system=system,
        messages=messages,
        tools=request_tools(tools, withheld or {}, entries,
                            lambda t, text: state.prompt(f"tool/{t.name}", text),
                            plain=plain),
        max_tokens=max_tokens,
        effort=effort,  # type: ignore[arg-type]
        output_schema=output_schema,
        cache_after=cache_breakpoints(state),
        root_id=state.root_id or state.task_id or None,
    )


def request_tools(tools: Sequence[Tool], withheld: Mapping[str, str],
                  entries: Sequence[ToolEntry], describe: Callable[[Tool, str], str], *,
                  plain: Collection[str] = ()) -> list[ToolSpec]:
    """The tool list of a request: every tool not ``withheld``, sorted by
    name, then the ones that declare ``listed_last`` (server tools) sorted by
    name; a deferred tool is sent with ``defer_loading`` (and its
    ``loaded_description`` when it has one), unless ``plain`` names it. Each
    entry goes where it says (:class:`~noeta.core.extension.ToolEntry`).
    ``describe(tool, text)`` gives a tool's (possibly overridden) description,
    ``text`` being its own."""
    sent = [t for t in tools if t.name not in withheld]

    def spec(t: Tool) -> ToolSpec:
        loaded = tool_flag(t, "deferred") and t.name not in plain
        own = getattr(t, "loaded_description", None) if loaded else None
        text = own if isinstance(own, str) else t.description
        return ToolSpec(name=t.name, description=describe(t, text), input_schema=t.input_schema,
                        defer_loading=loaded)

    head = [spec(t) for t in sorted((t for t in sent if not tool_flag(t, "listed_last")),
                                    key=lambda t: t.name)]
    tail = [spec(t) for t in sorted((t for t in sent if tool_flag(t, "listed_last")),
                                    key=lambda t: t.name)]
    for entry in entries:
        group = next((g for g in (head, tail) if any(s.name == entry.before for s in g)), None)
        if entry.before and group is not None:
            group.insert(next(i for i, s in enumerate(group) if s.name == entry.before),
                         entry.spec)
        else:
            head.append(entry.spec)
    return head + tail


def _place(text: str, part: ContextPart) -> str:
    lines = text.split("\n")
    if part.before and part.before in lines:
        i = lines.index(part.before)
        head, tail = "\n".join(lines[:i]), "\n".join(lines[i:])
        return f"{head}\n{part.text}\n\n{tail}" if i else f"{part.text}\n\n{tail}"
    return f"{text}\n{part.text}" if text else part.text


def cache_breakpoints(state: TaskState) -> list[int]:
    """One breakpoint, on the newest message, as Claude Code places it: the
    next step reads everything up to it from cache."""
    return [len(state.messages) - 1] if state.messages else []


def request_hash(request: ModelRequest) -> str:
    return hashlib.sha256(dumps(request)).hexdigest()[:32]
