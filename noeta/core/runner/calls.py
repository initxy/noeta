"""The tool step: gate the calls in order, run what may run, stop at the
first that must ask."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine, Sequence
from typing import TYPE_CHECKING, Any

from noeta.core import records as core_records
from noeta.core.errors import UnknownTask, UnservedQueue
from noeta.core.events import (
    Event,
    ExtensionRecorded,
    InboxKind,
    Say,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.core.extension import Allow, Ask, Deny, Gate, Rewrite, TaskView, child_task
from noeta.core.large_output import ReplyBudget, bound_result
from noeta.core.runner import closing, texts
from noeta.core.store import NewTask, Post
from noeta.core.tool import MAX_ATTEMPTS, Tool, ToolContext, ToolOutput, tool_flags
from noeta.core.tool_input import NO_SUCH_TOOL, check_input, tool_use_error
from noeta.core.types import Delta, Message, Text, ToolResult, ToolUse, user

if TYPE_CHECKING:
    from noeta.core.runner.task import TaskRun

log = logging.getLogger("noeta.core.runner")


#: Rewrites of one call before it is refused (an extension that keeps
#: rewriting would otherwise loop).
MAX_REWRITES = 3


async def gate(run: TaskRun, view: TaskView, call: ToolUse) -> Gate:
    """Every extension is asked about this exact call: any deny wins, then
    the first rewrite, then the asks. A human's recorded answer denies, or
    satisfies the asks — never a deny, and never instead of a rewrite. An
    approving ``Allow`` satisfies asks that are not firm; otherwise each
    extension may answer the asks (``resolve_ask``) before the call parks."""
    answer = run.state.answers.get(call.id)
    if answer is not None and answer.decision == "deny":
        return Deny(reason=rejected_text(answer.data))
    verdicts: list[Gate] = []
    for ext in run.exts:
        try:
            v = await ext.before_tool(view, call)
        except Exception as exc:  # noqa: BLE001 — a failing check denies
            v = Deny(reason=f"permission check failed: {exc}")
        if isinstance(v, Rewrite) and (v.name or call.name) == call.name \
                and v.input == call.input:
            v = None  # a rewrite to the same call is no rewrite
        if v is not None:
            verdicts.append(v)
    for kind in (Deny, Rewrite):
        for v in verdicts:
            if isinstance(v, kind):
                return v
    asks = [v for v in verdicts if isinstance(v, Ask)]
    if not asks:
        # Outright only when nobody answered for the call either.
        return Allow(outright=answer is None and any(
            isinstance(v, Allow) and v.outright for v in verdicts))
    if answer is not None and answer.decision == "allow":
        return Allow()
    if not any(a.firm for a in asks) and any(
            isinstance(v, Allow) and v.approve for v in verdicts):
        return Allow()
    for ext in run.exts:
        try:
            resolved = await ext.resolve_ask(view, call, asks)
        except Exception as exc:  # noqa: BLE001 — a failing resolver denies
            return Deny(reason=f"permission check failed: {exc}")
        if isinstance(resolved, (Allow, Deny, Rewrite, Ask)):
            return resolved
    return asks[0]


def rejected_text(data: Any) -> str:
    """A person's refusal at an approval prompt, with what they said when
    they said something (``Answer.data`` ``{"message": ...}`` or a string)."""
    message = data.get("message") if isinstance(data, dict) else data
    if isinstance(message, str) and message.strip():
        return texts.REJECTED_BY_USER + texts.REJECTED_WITH_MESSAGE.format(
            message=message.strip())
    return texts.REJECTED_BY_USER


async def resolve(run: TaskRun, view: TaskView, use: ToolUse) -> tuple[ToolUse, Gate]:
    """The call that will run and its verdict: a rewritten call is gated
    again, so every guard sees (and a human approves) the call that runs."""
    for _ in range(MAX_REWRITES):
        verdict = await gate(run, view, use)
        if not isinstance(verdict, Rewrite):
            return use, verdict
        use = ToolUse(id=use.id, name=verdict.name or use.name, input=verdict.input)
    verdict = await gate(run, view, use)
    if isinstance(verdict, Rewrite):
        verdict = Deny(reason=f"the call was rewritten more than {MAX_REWRITES} times")
    return use, verdict


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


async def _together(runs: list[Coroutine[Any, Any, None]]) -> None:
    """Run a group's calls at once; when one fails (its result could not be
    written), the others are cancelled before the failure goes up."""
    started = [asyncio.ensure_future(r) for r in runs]
    try:
        await asyncio.gather(*started)
    except BaseException:
        for task in started:
            task.cancel()
        await asyncio.gather(*started, return_exceptions=True)
        raise


async def run_calls(run: TaskRun, call_ids: Sequence[str]) -> bool:
    view = run.view()
    offer = run.tool_offer(view)
    tools = {t.name: t for t in offer.tools}
    runnable: list[tuple[ToolUse, Tool]] = []
    #: The first call that must ask: gating stops there, as Claude Code asks
    #: one call at a time in the reply's order. The calls after it are
    #: neither started nor refused; they are gated once it is answered.
    asked: tuple[str, Ask] | None = None
    outright: set[str] = set()
    for c in [c for c in run.state.calls if c.use.id in call_ids]:
        withheld = offer.withheld.get(c.use.name)
        if withheld is not None:
            # Not in the request the model saw: refused before any gate.
            await run.append([run.error_result(c.use.id, tool_use_error(withheld))])
            continue
        known = tools.get(c.use.name)
        # Claude Code checks the arguments against the schema before any gate,
        # and the gates and the tool get them coerced (from the logged call,
        # so every worker derives the same input).
        checked, problems = (c.use.input, None) \
            if known is None or tool_flags(known).unchecked_input \
            else check_input(known.name, known.input_schema, c.use.input)
        if problems is not None:
            await run.append([run.error_result(c.use.id, tool_use_error(problems))])
            continue
        answer = run.state.answers.get(c.use.id)
        if answer is not None and answer.decision == "deny" and isinstance(answer.data, dict) \
                and answer.data.get("interrupt") is False:
            # Refused by a permission callback that lets the turn go on
            # (Claude Code's SDK can_use_tool deny): the model reads why.
            message = str(answer.data.get("message") or "").strip()
            await run.append([run.error_result(
                c.use.id, message or texts.DENIED_BY_CALLBACK.format(name=c.use.name))])
            continue
        if answer is not None and answer.decision == "deny":
            # Declined at its prompt: the call does not run and, as in Claude
            # Code, the turn stops once the reply's results land.
            await run.append([ToolFinished(
                result=ToolResult(call_id=c.use.id, content=[Text(text=rejected_text(
                    answer.data))], is_error=True), end_turn="",
                note=texts.REJECTED_MARKER)])
            continue
        call = c.use if checked == c.use.input else ToolUse(
            id=c.use.id, name=c.use.name, input=dict(checked))
        use, verdict = await resolve(run, view, call)
        if isinstance(verdict, Deny):
            await run.append([run.error_result(use.id, verdict.reason or "denied")])
            continue
        if isinstance(verdict, Ask):
            asked = (use.id, verdict)
            break
        tool = tools.get(use.name)
        if tool is None:
            await run.append([run.error_result(
                use.id, tool_use_error(NO_SUCH_TOOL.format(name=use.name)))])
            continue
        if isinstance(verdict, Allow) and verdict.outright:
            outright.add(use.id)
        runnable.append((use, tool))
    await run.flush(view)
    # A call whose tool declares ``run_last`` (delegation) runs after the
    # reply's ordinary calls, as 0.6 spawned only once they had finished.
    runnable.sort(key=lambda item: tool_flags(item[1]).run_last)
    budget = _reply_budget(run)
    for group in _groups(runnable):
        await run.append([ToolStarted(call=u) for u, _ in group])
        await _together([_run_one(run, u, t, budget, outright=u.id in outright)
                         for u, t in group])
    if asked is None:
        return True
    if run.state.config.get("unattended"):
        # One-shot: the task ends. Otherwise the turn closes and the loop
        # goes on to wait for input (and gives the task back).
        return await closing.fail_turn(run, texts.NEEDS_HUMAN, texts.NEEDS_HUMAN_DETAIL)
    # Recorded, so a wake before the answer does not gate it again
    # (``TaskState.asking``).
    call_id, ask = asked
    await run.append([Waiting(on="approval", ids=[call_id],
                              detail={call_id: {"prompt": ask.prompt, **ask.detail}})])
    return False


def _context(run: TaskRun, use: ToolUse, records: list[Event],
             creates: list[NewTask], *, outright: bool = False) -> ToolContext:
    def record(extension: str, kind: str, data: Any) -> None:
        core_records.check_writable(extension, kind)
        records.append(ExtensionRecorded(extension=extension, kind=kind, data=data))

    def spawn(agent: str, prompt: str | Message, *, config: dict[str, Any] | None = None,
              background: bool = False, queue: str | None = None,
              history_from: str | None = None, history_through: int | None = None) -> str:
        child_id = f"{run.task_id}.{use.id}.{len(creates) + 1}"
        message = user(prompt) if isinstance(prompt, str) else prompt
        creates.append(NewTask(
            task_id=child_id,
            created=child_task(run.task_id, run.state, run.exts, agent, config,
                               history_from=history_from, history_through=history_through),
            queue=queue or run.claim.queue,
            inbox=[Say(message=message)],
        ))
        records.append(core_records.event(core_records.Spawned(children=[
            {"id": child_id, "call_id": use.id, "background": background}])))
        return child_id

    async def post(body: InboxKind, task_id: str | None = None,
                   dedupe_key: str | None = None, forward_if_ended: bool = False) -> None:
        target = task_id or run.task_id
        if target != run.task_id:
            if await run.store.get(target) is None:
                raise UnknownTask(target)
        await run.store.post(Post(task_id=target, body=body, dedupe_key=dedupe_key,
                                  forward_if_ended=forward_if_ended))

    async def tasks(**kw: Any) -> list[Any]:
        return list(await run.store.tasks(**kw))

    async def task(task_id: str) -> Any:
        return await run.store.get(task_id)

    sink, started = run.rt.on_delta, time.monotonic()

    def progress(text: str) -> None:
        if sink is not None:
            sink(run.task_id, Delta(kind="tool", text=text, call_id=use.id, tool=use.name,
                                    elapsed=time.monotonic() - started))

    root = run.state.root_id or run.task_id
    answer = run.state.answers.get(use.id)
    return ToolContext(task_id=run.task_id, call_id=use.id, env=run.env(),
                       state=dict(run.state.ext), record=record, spill_dir=run.rt.spill_root,
                       spawn=spawn, post=post, root_id=root, prompt=run.state.prompt,
                       tasks=tasks, task=task, count_tokens=run.counter(run.model),
                       complete=run.complete, progress=progress if sink else None,
                       hold=lambda: run.rt.hold(root), by_model=True,
                       approved=answer is not None and answer.decision == "allow",
                       allowed_outright=outright, model=run.model,
                       identity=run.identity, effort=run.effort,
                       serves_queue=run.serves_queue)


async def _unknown_history(run: TaskRun, creates: list[NewTask]) -> str | None:
    """A ``history_from`` among the staged children that is no task
    (``Client.create`` refuses it the same way)."""
    for c in creates:
        source = c.created.history_from
        if source is not None and await run.store.get(source) is None:
            return source
    return None


def _unserved_queue(run: TaskRun, creates: list[NewTask]) -> str | None:
    """A staged child's queue the child could not run on now
    (:meth:`TaskRun.serves_queue`)."""
    return next((c.queue for c in creates if not run.serves_queue(c.queue)), None)


async def _finish(run: TaskRun, use: ToolUse, output: ToolOutput, records: list[Event],
                  creates: list[NewTask]) -> None:
    unknown = await _unknown_history(run, creates)
    if unknown is not None:
        output = ToolOutput(content=[Text(text=f"UnknownTask: {unknown}")], is_error=True)
        _drop_staged(records, creates)
    unserved = _unserved_queue(run, creates)
    if unserved is not None:
        output = _unserved_output(unserved)
        _drop_staged(records, creates)
    view = run.view()
    if output.park is not None:
        park = output.park
        keys = park.keys or [use.id]
        try:
            await run.append([*records, core_records.event(core_records.Parked(
                call_id=use.id, on=park.on, keys=keys, detail=park.detail))], creates=creates)
            return
        except UnservedQueue as exc:  # held since the check above
            output = _unserved_output(exc.queue)
            _drop_staged(records, creates)
    for ext in run.exts:
        try:
            output = await ext.after_tool(view, use, output)
        except Exception:  # a failing hook keeps the output it was given
            log.exception("extension %s after_tool failed", ext.name)
    meta = dict(output.meta)
    if output.display is not None:
        meta["display"] = output.display
    finished = ToolFinished(result=ToolResult(call_id=use.id, content=output.content,
                                              is_error=output.is_error,
                                              block_list=output.block_list), meta=meta,
                            end_turn=output.end_turn)
    try:
        await run.append([*records, *view.pending, finished],
                         creates=[*creates, *run.staged(view)], posts=await run.enqueued(view))
    except UnservedQueue as exc:
        # The tree was held since the check above (a hook may take seconds):
        # the call fails and stages nothing, its hooks' records included.
        await run.append([run.error_result(
            use.id, texts.UNSERVED_QUEUE_ERROR.format(queue=exc.queue))])


def _unserved_output(queue: str) -> ToolOutput:
    return ToolOutput(content=[Text(text=texts.UNSERVED_QUEUE_ERROR.format(queue=queue))],
                      is_error=True)


def _drop_staged(records: list[Event], creates: list[NewTask]) -> None:
    """A failed call stages nothing: no child it spawned, and no record of
    one (a record naming a child that was never created would leave the
    task waiting on, or cancelling, a task that does not exist)."""
    records.clear()
    creates.clear()


async def _run_one(run: TaskRun, use: ToolUse, tool: Tool, budget: ReplyBudget, *,
                   outright: bool = False) -> None:
    records: list[Event] = []
    creates: list[NewTask] = []
    ctx = _context(run, use, records, creates, outright=outright)
    try:
        output = await tool.run(use.input, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a tool bug becomes an error result, not a dead task
        log.exception("tool %s failed", use.name)
        output = ToolOutput(content=[Text(text=f"{type(exc).__name__}: {exc}")], is_error=True)
        _drop_staged(records, creates)
    output = await bound_result(output, tool, ctx, budget)
    await _finish(run, use, output, records, creates)


def _reply_budget(run: TaskRun) -> ReplyBudget:
    """The reply's result budget, counting the text its calls returned so
    far."""
    return ReplyBudget(used=sum(len(b.text) for c in run.state.calls if c.result is not None
                                for b in c.result.content if isinstance(b, Text)))


async def recover_calls(run: TaskRun, call_ids: Sequence[str]) -> bool:
    """Calls a crash (or a lost claim) left started and unfinished: each
    gets the result its tool recovers, or runs again when its tool is
    ``retry_safe`` (and has not started :data:`MAX_ATTEMPTS` times), or is
    answered "outcome unknown". They are not gated again: each was allowed
    as it ran (``Call.ran``)."""
    tools = {t.name: t for t in run.tools(run.view())}
    again: list[tuple[ToolUse, Tool]] = []
    for c in [c for c in run.state.calls if c.use.id in call_ids]:
        use = c.ran or c.use
        tool = tools.get(use.name)
        recover = getattr(tool, "recover", None)
        if recover is not None:
            records: list[Event] = []
            creates: list[NewTask] = []
            try:
                output = await recover(use.input, _context(run, use, records, creates))
            except asyncio.CancelledError:
                raise
            except Exception:  # a failing lookup tells nothing
                log.exception("tool %s recover failed", use.name)
                output = None
            if output is not None and output.park is None:
                await _finish(run, use, output, records, creates)
                continue
        if tool is not None and tool_flags(tool).retry_safe and c.attempts < MAX_ATTEMPTS:
            again.append((use, tool))
            continue
        await run.append([run.error_result(use.id, texts.INTERRUPTED_CALL_ERROR)])
    budget = _reply_budget(run)
    for group in _groups(again):
        await run.append([ToolStarted(call=u) for u, _ in group])
        await _together([_run_one(run, u, t, budget) for u, t in group])
    return True


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
        use = c.ran or c.use  # the call as it ran (after any rewrite)
        tool = tools.get(use.name)
        outcome = None if withdrawn else _outcome(run, c.parked or {}, use.id)
        resume = getattr(tool, "resume", None)
        if resume is None:
            output = ToolOutput(content=[Text(text=texts.STOPPED_CALL_ERROR)], is_error=True)
        else:
            try:
                output = await resume(use.input, _context(run, use, records, creates), outcome)
            except Exception as exc:
                log.exception("tool %s resume failed", use.name)
                output = ToolOutput(content=[Text(text=f"{type(exc).__name__}: {exc}")],
                                    is_error=True)
                _drop_staged(records, creates)
        if output.park is not None:  # a resume may not park again
            output = ToolOutput(content=[Text(text="the call could not finish")], is_error=True)
            _drop_staged(records, creates)
        await _finish(run, use, output, records, creates)
