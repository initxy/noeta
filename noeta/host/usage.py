"""What a task tree has used and what fills the next request: the numbers
behind ``/cost`` and ``/context``, the headless ``result`` message and the
HTTP ``usage`` / ``context`` endpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from noeta.core import models
from noeta.core.events import ModelCalled, ModelReplied, TaskCreated
from noeta.core.types import ModelRequest, Usage
from noeta.toolkit.context import estimate_tokens

__all__ = ["ModelUse", "TreeUsage", "context_breakdown", "context_text", "cost_text",
           "tree_usage"]


@dataclass
class ModelUse:
    """One model's share: tokens, model calls, USD (0 when the catalog has
    no price) and seconds spent waiting on the model."""

    usage: Usage = field(default_factory=Usage)
    calls: int = 0
    cost_usd: float = 0.0
    api_seconds: float = 0.0


@dataclass
class TreeUsage:
    by_model: dict[str, ModelUse] = field(default_factory=dict)
    tasks: int = 0

    @property
    def usage(self) -> Usage:
        total = Usage()
        for m in self.by_model.values():
            total = total + m.usage
        return total

    @property
    def cost_usd(self) -> float:
        return sum(m.cost_usd for m in self.by_model.values())

    @property
    def api_seconds(self) -> float:
        return sum(m.api_seconds for m in self.by_model.values())

    def add(self, model: str, usage: Usage, seconds: float) -> None:
        use = self.by_model.setdefault(model, ModelUse())
        use.usage = use.usage + usage
        use.calls += 1
        use.cost_usd += models.price(model, usage) if model else 0.0
        use.api_seconds += max(seconds, 0.0)


def add_events(total: TreeUsage, events: list[Any], *, after: int = 0) -> None:
    """Fold one task's model calls (those after event ``after``) into
    ``total``."""
    model, called_at = "", 0.0
    for rec in events:
        ev = rec.event
        if isinstance(ev, TaskCreated):
            model = str(ev.config.get("model") or "")
        elif isinstance(ev, ModelCalled):
            model, called_at = ev.model or model, rec.at
        elif isinstance(ev, ModelReplied) and rec.seq > after:
            total.add(ev.response.model or model, ev.response.usage,
                      rec.at - called_at if called_at else 0.0)


async def tree_usage(client: Any, task_id: str) -> TreeUsage:
    """Every model call of the task and its sub-agents, by model."""
    total = TreeUsage()
    pending = [task_id]
    while pending:
        tid = pending.pop()
        total.tasks += 1
        add_events(total, await client.events(tid))
        pending.extend(c.task_id for c in await client.tasks(parent_id=tid, limit=10_000))
    return total


def cost_text(total: TreeUsage) -> str:
    """``/cost``: the cost and tokens per model, and API time."""
    lines = [f"Total cost:            ${total.cost_usd:.4f}",
             f"Total duration (API):  {total.api_seconds:.1f}s",
             f"Tasks (with sub-agents): {total.tasks}",
             "Usage by model:"]
    if not total.by_model:
        lines.append("  (no model calls yet)")
    for name, use in sorted(total.by_model.items()):
        u = use.usage
        lines.append(f"  {name}: {u.uncached:,} input, {u.output:,} output, "
                     f"{u.cache_read:,} cache read, {u.cache_write:,} cache write "
                     f"({use.calls} call{'s' if use.calls != 1 else ''}, ${use.cost_usd:.4f})")
    return "\n".join(lines)


def _tool_tokens(spec: Any) -> int:
    return estimate_tokens(spec.name + spec.description + json.dumps(spec.input_schema))


def context_breakdown(request: ModelRequest) -> dict[str, Any]:
    """Estimated tokens per part of ``request`` (a ``Runtime.preview``),
    against the model's window: ``categories`` (name -> tokens, in Claude
    Code's ``/context`` order), ``total``, ``window``, ``model``."""
    system = sum(estimate_tokens(p.text) for p in request.system)
    loaded = [t for t in request.tools if not t.defer_loading]
    mcp = sum(_tool_tokens(t) for t in loaded if t.name.startswith("mcp__"))
    tools = sum(_tool_tokens(t) for t in loaded if not t.name.startswith("mcp__"))
    messages = sum(estimate_tokens(json.dumps(_plain(m))) for m in request.messages)
    window, _ = models.model_limits(request.model)
    used = system + tools + mcp + messages
    categories = {"System prompt": system, "System tools": tools, "MCP tools": mcp,
                  "Messages": messages, "Free space": max(window - used, 0)}
    return {"model": request.model, "window": window, "total": used,
            "categories": categories, "deferred_tools": len(request.tools) - len(loaded)}


def _plain(message: Any) -> Any:
    out = []
    for block in message.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            out.append(text)
        elif hasattr(block, "input"):
            out.append(json.dumps(block.input))
        elif hasattr(block, "content"):
            out.extend(getattr(b, "text", "") for b in block.content)
        else:
            out.append("[image]")
    return out


def context_text(breakdown: dict[str, Any]) -> str:
    """``/context``: one line per category with its share of the window."""
    window = max(int(breakdown["window"]), 1)
    lines = [f"Context: {breakdown['model']} · ~{breakdown['total']:,} of {window:,} tokens "
             f"({breakdown['total'] / window:.0%}) — estimated for the next request"]
    for name, tokens in breakdown["categories"].items():
        lines.append(f"  {name:<14} {tokens:>9,} tokens ({tokens / window:.1%})")
    if breakdown.get("deferred_tools"):
        lines.append(f"  ({breakdown['deferred_tools']} deferred tools load on demand)")
    return "\n".join(lines)
