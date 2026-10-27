"""The tool step: gate each call, run what may run, park what must ask."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from noeta.events import (
    Event,
    ExtensionRecorded,
    InboxKind,
    Say,
    TaskCreated,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.ext.base import Allow, Ask, Deny, Gate, Rewrite, TaskView
from noeta.loop.state import CORE
from noeta.runner import texts
from noeta.store.base import NewTask, Patch, Post
from noeta.tools.base import Tool, ToolContext, ToolOutput
from noeta.types import Message, Text, ToolResult, ToolUse, user

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
    # A call whose tool declares ``run_last`` (delegation) runs after the
    # reply's ordinary calls, as 0.6 spawned only once they had finished.
    runnable.sort(key=lambda item: bool(getattr(item[1], "run_last", False)))
    for group in _groups(runnable):
        await run.append([ToolStarted(call=u) for u, _ in group])
        await asyncio.gather(*(_run_one(run, u, t) for u, t in group))
    if asks and run.state.config.get("unattended"):
        await run.fail_turn(texts.NEEDS_HUMAN, texts.NEEDS_HUMAN_DETAIL)
        return False
    if asks:
        await run.append(
            [Waiting(on="approval", ids=list(asks),
                     detail={k: {"prompt": a.prompt, **a.detail} for k, a in asks.items()})],
            patch=Patch(status="waiting", waiting_on="approval",
                        inbox_read_seq=run.state.inbox_read),
        )
        return False
    return True


def _context(run: TaskRun, use: ToolUse, records: list[Event],
             creates: list[NewTask]) -> ToolContext:
    def record(extension: str, kind: str, data: Any) -> None:
        records.append(ExtensionRecorded(extension=extension, kind=kind, data=data))

    def spawn(agent: str, prompt: str | Message, *, config: dict[str, Any] | None = None,
              background: bool = False, queue: str = "default") -> str:
        child_id = f"{run.task_id}.{use.id}.{len(creates) + 1}"
        message = user(prompt) if isinstance(prompt, str) else prompt
        creates.append(NewTask(
            task_id=child_id,
            created=TaskCreated(agent=agent, config=dict(config or {}), parent_id=run.task_id,
                                root_id=run.state.root_id or run.task_id),
            queue=queue,
            inbox=[Say(message=message)],
        ))
        records.append(ExtensionRecorded(extension=CORE, kind="spawned", data={"children": [
            {"id": child_id, "call_id": use.id, "background": background}]}))
        return child_id

    async def post(body: InboxKind, task_id: str | None = None) -> None:
        await run.store.post(Post(task_id=task_id or run.task_id, body=body))

    return ToolContext(task_id=run.task_id, call_id=use.id, env=run.env(),
                       state=dict(run.state.ext), record=record, spill_dir=run.rt.spill_root,
                       spawn=spawn, post=post, root_id=run.state.root_id or run.task_id)


async def _finish(run: TaskRun, use: ToolUse, output: ToolOutput, records: list[Event],
                  creates: list[NewTask]) -> None:
    view = run.view()
    if output.park is not None:
        park = output.park
        keys = park.keys or [use.id]
        records.append(ExtensionRecorded(extension=CORE, kind="parked", data={
            "call_id": use.id, "on": park.on, "keys": keys, "detail": park.detail}))
        await run.append(records, creates=creates)
        return
    for ext in run.exts:
        output = await ext.after_tool(view, use, output)
    meta = dict(output.meta)
    if output.display is not None:
        meta["display"] = output.display
    finished = ToolFinished(result=ToolResult(call_id=use.id, content=output.content,
                                              is_error=output.is_error), meta=meta)
    await run.append([*records, *view.pending, finished], creates=creates)


async def _run_one(run: TaskRun, use: ToolUse, tool: Tool) -> None:
    records: list[Event] = []
    creates: list[NewTask] = []
    ctx = _context(run, use, records, creates)
    try:
        output = await tool.run(use.input, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a tool bug becomes an error result, not a dead task
        log.exception("tool %s failed", use.name)
        output = ToolOutput(content=[Text(text=f"{type(exc).__name__}: {exc}")], is_error=True)
        creates.clear()
    await _finish(run, use, output, records, creates)


def _outcome(run: TaskRun, parked: dict[str, Any], call_id: str) -> Any:
    if parked.get("on") == "question":
        return run.state.answers.get(call_id)
    return {k: run.state.children.get(k) for k in parked.get("keys") or []}


async def resume_calls(run: TaskRun, call_ids: Sequence[str], *, withdrawn: bool = False) -> None:
    """Finish parked calls with what arrived (or, ``withdrawn``, with nothing:
    the turn was stopped)."""
    tools = {t.name: t for t in run.tools(run.view())}
    for c in [c for c in run.state.calls if c.use.id in call_ids and c.parked]:
        records: list[Event] = []
        creates: list[NewTask] = []
        tool = tools.get(c.use.name)
        outcome = None if withdrawn else _outcome(run, c.parked or {}, c.use.id)
        resume = getattr(tool, "resume", None)
        if resume is None:
            output = ToolOutput(content=[Text(text=texts.STOPPED_CALL_ERROR)], is_error=True)
        else:
            try:
                output = await resume(c.use.input, _context(run, c.use, records, creates), outcome)
            except Exception as exc:
                log.exception("tool %s resume failed", c.use.name)
                output = ToolOutput(content=[Text(text=f"{type(exc).__name__}: {exc}")],
                                    is_error=True)
        if output.park is not None:  # a resume may not park again
            output = ToolOutput(content=[Text(text="the call could not finish")], is_error=True)
        await _finish(run, c.use, output, records, creates)
