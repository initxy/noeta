"""The eval harness core: the config, what a trial records (``Trace``),
scenarios, and the scored-trial wrapper. ``evals.engine`` supplies ``Judge``
and ``run_trial``.
"""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

#: Hard cap per trial; a stuck run is interrupted and scored as it stands.
TRIAL_TIMEOUT_S = 420.0


class ToolUse(Protocol):
    """What scoring reads from a tool call."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


class ToolResultView(Protocol):
    """What scoring reads from a tool result."""

    call_id: str
    success: bool


class Judge(Protocol):
    input_tokens: int
    output_tokens: int
    requests: int

    def ask(self, question: str, text: str) -> bool: ...


@dataclass
class EvalConfig:
    model: str
    provider: str = "anthropic"
    #: Replaces ``main``'s system prompt (the degraded-prompt check).
    system_prompt: str | None = None


@dataclass
class Trace:
    """What one trial did, in the shapes scoring needs."""

    workspace: Path
    status: str
    items: list[Any]
    #: Tool calls grouped by the model response that issued them.
    batches: list[list[ToolUse]]
    results: dict[str, ToolResultView]
    final_text: str
    spawned: int
    compactions: int
    error: str | None = None

    @property
    def calls(self) -> list[ToolUse]:
        return [c for b in self.batches for c in b]

    def named(self, *names: str) -> list[ToolUse]:
        return [c for c in self.calls if c.tool_name in names]

    def bash_commands(self) -> list[str]:
        return [str(c.arguments.get("command", "")) for c in self.named("Bash")]

    def failed(self, call: ToolUse) -> bool:
        r = self.results.get(call.call_id)
        return r is not None and not r.success

    def file(self, rel: str) -> str | None:
        p = self.workspace / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None


def path_arg(call: ToolUse) -> str:
    return str(call.arguments.get("file_path") or call.arguments.get("path") or "")


def mentions(call: ToolUse, rel: str) -> bool:
    p = path_arg(call)
    return p == rel or p.endswith("/" + rel)


#: A shell command doing what a dedicated read/search tool does.
SHELL_READ_SEARCH = re.compile(
    r"(^|[\s;&|(])(cat|head|tail|less|more|sed|awk|grep|egrep|rg|find|ag)\s"
)
SHELL_SEARCH = re.compile(r"(^|[\s;&|(])(grep|egrep|rg|ag|git grep)\s")


def cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


@dataclass
class Scenario:
    name: str
    #: The prompt-sensitive behavior under test, one line.
    behavior: str
    goal: str
    setup: Callable[[Path], None]
    #: ``(trace, judge) -> (score in 0..1, named sub-checks)``.
    score: Callable[[Trace, Judge], tuple[float, dict[str, Any]]]
    small_window: bool = False
    #: Wall-clock cap; a trial past it is interrupted and scored as it stands.
    timeout_s: float = TRIAL_TIMEOUT_S
    tags: tuple[str, ...] = field(default_factory=tuple)


RunTrial = Callable[..., tuple["Trace", dict[str, Any], Path]]


def time_trial(
    cfg: EvalConfig, sc: Scenario, judge: Judge, trial: int, run_trial: RunTrial
) -> dict[str, Any]:
    """One scored trial as a JSON-ready record; never raises."""
    start = time.monotonic()
    record: dict[str, Any] = {"scenario": sc.name, "trial": trial}
    root: Path | None = None
    try:
        trace, usage, root = run_trial(
            cfg, sc.goal, sc.setup, small_window=sc.small_window, timeout_s=sc.timeout_s
        )
        score, checks = sc.score(trace, judge)
        record.update(
            score=round(float(score), 4),
            checks=checks,
            status=trace.status,
            error=trace.error,
            tools=[[c.tool_name for c in b] for b in trace.batches],
            commands=[c[:300] for c in trace.bash_commands()],
            spawned=trace.spawned,
            compactions=trace.compactions,
            final=trace.final_text[:600],
            usage=usage,
        )
    except Exception as exc:  # noqa: BLE001 - a broken trial scores 0, the run goes on
        record.update(score=0.0, checks={}, status="harness_error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if root is not None:
            cleanup(root)
    record["seconds"] = round(time.monotonic() - start, 1)
    return record
