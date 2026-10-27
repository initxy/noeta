"""The tool step: gate each call, run what may run, park what must ask."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from noeta.events import Event, ExtensionRecorded, ToolFinished, ToolStarted, Waiting
from noeta.ext.base import Allow, Ask, Deny, Gate, Rewrite, TaskView
from noeta.runner import texts
from noeta.store.base import Patch
from noeta.tools.base import Tool, ToolContext, ToolOutput
from noeta.types import Text, ToolResult, ToolUse

if TYPE_CHECKING:
    from noeta.runner.task import TaskRun

log = logging.getLogger("noeta.runner")


async def gate(run: TaskRun, view: TaskView, call: ToolUse) -> Gate:
    """A recorded answer decides; otherwise every extension is asked: any
    deny wins, then the first ask, then the first rewrite."""
    answer = run.state.answers.get(call.id)
    if answer is not None and answer.decision is not None:
        return Allow() if answer.decision == "allow" else Deny(reason=texts.DENIED_BY_HUMAN)
    verdicts: list[Gate] = []
    for ext in run.exts:
        try:
            v = await ext.before_tool(view, call)
        except Exception as exc:  # noqa: BLE001 — a failing check denies
            v = Deny(reason=f"permission check failed: {exc}")
        if v is not None:
            verdicts.append(v)
    for kind in (Deny, Ask, Rewrite):
        for v in verdicts:
            if isinstance(v, kind):
                return v
    return Allow()


def _groups(runnable: list[tuple[ToolUse, Tool]]) -> list[list[tuple[ToolUse, Tool]]]:
    """Consecutive read-only calls run together (at most 10 at once); every
    other call runs alone, in order."""
    groups: list[list[tuple[ToolUse, Tool]]] = []
    for item in runnable:
        last = groups[-1] if groups else None
        if (item[1].read_only and last and last[-1][1].read_only
                and len(last) < texts.MAX_TOOL_USE_CONCURRENCY):
            last.append(item)
        else:
            groups.append([item])
    return groups


async def run_calls(run: TaskRun, call_ids: Sequence[str]) -> bool:
    view = run.view()
    tools = {t.name: t for t in run.tools(view)}
    runnable: list[tuple[ToolUse, Tool]] = []
    asks: dict[str, Ask] = {}
    for c in [c for c in run.state.calls if c.use.id in call_ids]:
        use = c.use
        verdict = await gate(run, view, use)
        if isinstance(verdict, Rewrite):
            use = ToolUse(id=use.id, name=verdict.name or use.name, input=verdict.input)
            verdict = Allow()
        if isinstance(verdict, Deny):
            await run.append([run.error_result(use.id, verdict.reason or "denied")])
            continue
        if isinstance(verdict, Ask):
            asks[use.id] = verdict
            continue
        tool = tools.get(use.name)
        if tool is None:
            await run.append([run.error_result(use.id, texts.UNKNOWN_TOOL.format(name=use.name))])
            continue
        runnable.append((use, tool))
    await run.flush(view)
    for group in _groups(runnable):
        await run.append([ToolStarted(call=u) for u, _ in group])
        await asyncio.gather(*(_run_one(run, u, t) for u, t in group))
    if asks:
        await run.append(
            [Waiting(on="approval", ids=list(asks),
                     detail={k: {"prompt": a.prompt, **a.detail} for k, a in asks.items()})],
            patch=Patch(status="waiting", waiting_on="approval",
                        inbox_read_seq=run.state.inbox_read),
        )
        return False
    return True


async def _run_one(run: TaskRun, use: ToolUse, tool: Tool) -> None:
    records: list[Event] = []

    def record(extension: str, kind: str, data: Any) -> None:
        records.append(ExtensionRecorded(extension=extension, kind=kind, data=data))

    ctx = ToolContext(task_id=run.task_id, call_id=use.id, env=run.rt.env_for(run.state),
                      state=dict(run.state.ext), record=record, spill_dir=run.rt.spill_root)
    try:
        output = await tool.run(use.input, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a tool bug becomes an error result, not a dead task
        log.exception("tool %s failed", use.name)
        output = ToolOutput(content=[Text(text=f"{type(exc).__name__}: {exc}")], is_error=True)
    view = run.view()
    for ext in run.exts:
        output = await ext.after_tool(view, use, output)
    meta = dict(output.meta)
    if output.display is not None:
        meta["display"] = output.display
    finished = ToolFinished(result=ToolResult(call_id=use.id, content=output.content,
                                              is_error=output.is_error), meta=meta)
    await run.append([*records, *view.pending, finished])
