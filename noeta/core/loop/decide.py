"""What the runner does next, decided from the folded state alone."""

from __future__ import annotations

from typing import Union

from noeta.core.loop.state import Call, TaskState
from noeta.core.types import Struct


class CallModel(Struct, tag="call_model"):
    pass


class Continue(Struct, tag="continue"):
    """The reply hit the output cap without a tool call: ask for the rest."""


class RunCalls(Struct, tag="run_calls"):
    call_ids: list[str]


class Interrupted(Struct, tag="interrupted"):
    """Calls that started but never finished (a crash or lost claim): they
    get an "outcome unknown" result and are never run again."""

    call_ids: list[str]


class FinalReport(Struct, tag="final_report"):
    """The turn used up its step budget: one last tool-free call asks for a
    report of where the work stands."""


class StopTurn(Struct, tag="stop_turn"):
    """Someone stopped the turn: open calls get a "stopped" result and the
    task waits for the next message."""

    call_ids: list[str]


class ResumeCalls(Struct, tag="resume_calls"):
    """Parked calls whose answer or children have arrived."""

    call_ids: list[str]


class WaitParked(Struct, tag="wait_parked"):
    """Every open call is parked and nothing it waits for has arrived."""

    on: str
    ids: list[str]


class WaitInput(Struct, tag="wait_input"):
    pass


class End(Struct, tag="end"):
    status: str
    answer: str = ""
    error: str = ""


class Idle(Struct, tag="idle"):
    """Nothing to do: the task already ended."""


Action = Union[
    CallModel, Continue, RunCalls, ResumeCalls, Interrupted, FinalReport, StopTurn,
    WaitParked, WaitInput, End, Idle,
]


def next_action(state: TaskState, *, one_shot: bool, max_steps: int) -> Action:
    if state.ended is not None:
        return Idle()
    if state.cancel_requested is not None:
        return End(status="cancelled", error=state.cancel_requested)
    if state.stop_turn_requested is not None:
        return StopTurn(call_ids=[c.use.id for c in state.open_calls])
    if state.calls:
        open_calls: list[Call] = state.open_calls
        started = [c.use.id for c in open_calls if c.started and not c.parked]
        if started:
            return Interrupted(call_ids=started)
        unstarted = [c.use.id for c in open_calls if not c.started]
        if unstarted:
            return RunCalls(call_ids=unstarted)
        ready = [c.use.id for c in open_calls if state.parked_ready(c)]
        if ready:
            return ResumeCalls(call_ids=ready)
        # A question nobody has answered surfaces even while children run.
        kinds = [str((c.parked or {}).get("on") or "question") for c in open_calls]
        on = "question" if "question" in kinds else kinds[0]
        ids = [k for c in open_calls for k in ((c.parked or {}).get("keys") or [c.use.id])]
        return WaitParked(on=on, ids=ids)
    if state.turn_open:
        if state.steps_in_turn >= max_steps:
            return FinalReport()
        last = state.messages[-1] if state.messages else None
        if state.last_stop == "max_tokens" and last is not None and last.role == "assistant":
            return Continue()
        return CallModel()
    if one_shot and state.messages:
        live = state.live_children()
        if live:
            # Its background sub-agents still run: their hand-backs may open
            # another turn, so the task ends only once they have.
            return WaitParked(on="children", ids=live)
        return End(status="done", answer=state.last_answer)
    return WaitInput()
