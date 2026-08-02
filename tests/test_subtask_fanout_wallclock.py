"""Wall-clock concurrency of a one-turn spawn fan-out.

The decision-level suite (``test_subtask_fanout.py``) pins that a >=2-member
batch carries ``concurrent=True`` unless the escape valve forces it off; these
tests pin that the flag actually buys OVERLAPPING execution — the behaviour the
``spawn_subagent`` description advertises as "run CONCURRENTLY".

Strategy: a content-routing responder that, for each MEMBER turn, records a
[start, end] wall-clock interval and blocks on a ``threading.Barrier(N)``:

* concurrent members all reach the barrier, it releases, and their intervals
  overlap;
* serial members leave member #1 stuck at the barrier until its timeout breaks
  it (``BrokenBarrierError``) while member #2 has not even started — serial
  proven without waiting on wall-clock heuristics.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests._sdk_session import (
    coding_replay_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)

GOAL_A = "MEMBER_GOAL_ALPHA"
GOAL_B = "MEMBER_GOAL_BETA"
GOAL_C = "MEMBER_GOAL_GAMMA"
#: Seconds each member "thinks" past the barrier, so concurrent intervals have
#: a comfortably measurable overlap.
MEMBER_SLEEP = 0.3


def _req_text(req: LLMRequest) -> str:
    parts = []
    for m in req.messages:
        for b in m.content:
            # tool_use args + text both stringify usefully
            parts.append(repr(getattr(b, "arguments", "")))
            parts.append(getattr(b, "text", "") or "")
            parts.append(getattr(b, "output", "") or "")
    return " ".join(parts)


class _Probe:
    """Content-routing responder that times MEMBER turns and barriers them."""

    def __init__(self, member_goals: list[str], barrier_timeout: float) -> None:
        self.member_goals = member_goals
        self.barrier = threading.Barrier(len(member_goals), timeout=barrier_timeout)
        self.intervals: dict[str, tuple[float, float]] = {}
        self.barrier_broke = False
        self._lock = threading.Lock()
        self._parent_calls = 0

    def _which_member(self, text: str) -> str | None:
        hits = [g for g in self.member_goals if g in text]
        # a member turn sees exactly ITS OWN goal; the parent resume turn sees
        # ALL member goals (they're in its recorded spawn tool_use args).
        return hits[0] if len(hits) == 1 else None

    def __call__(self, req: LLMRequest) -> LLMResponse:
        text = _req_text(req)
        member = self._which_member(text)
        if member is None:
            # parent turn: 1st = the spawn fan-out, 2nd = final end_turn
            with self._lock:
                n = self._parent_calls
                self._parent_calls += 1
            if n == 0:
                return LLMResponse(
                    stop_reason="tool_use",
                    content=[
                        ToolUseBlock(
                            call_id="spawn",
                            tool_name=SPAWN_SUBAGENT_TOOL,
                            arguments={
                                "spawns": [
                                    {"agent": "general-purpose", "goal": g}
                                    for g in self.member_goals
                                ]
                            },
                        )
                    ],
                    usage=Usage(uncached=1, output=1),
                    raw={"id": "spawn"},
                )
            return LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="parent done")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "pend"},
            )
        # MEMBER turn: time it + hit the barrier (proves overlap).
        start = time.perf_counter()
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            with self._lock:
                self.barrier_broke = True
        time.sleep(MEMBER_SLEEP)
        end = time.perf_counter()
        with self._lock:
            self.intervals[member] = (start, end)
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text=f"{member} done")],
            usage=Usage(uncached=1, output=1),
            raw={"id": member},
        )


def _run(
    tmp_path: Path, member_goals: list[str], *, barrier_timeout: float
) -> _Probe:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "x.py").write_text("foo\n")
    probe = _Probe(member_goals, barrier_timeout)
    main = runner_main_spec("main", delegation=True, spawnable=("general-purpose",))
    host = make_host(
        make_registry(main, preset_spec("general-purpose")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responder=probe),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        budget=coding_replay_budget(3),
    )
    driver = make_driver(host)
    out = driver.start(goal="root", agent="main")
    assert out.status == "terminal", f"drive did not finish: {out.status}"
    return probe


def _max_overlap(intervals: dict[str, tuple[float, float]]) -> float:
    """Largest pairwise wall-clock overlap in seconds (0 if fully serial)."""
    vals = list(intervals.values())
    best = 0.0
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            (s1, e1), (s2, e2) = vals[i], vals[j]
            best = max(best, min(e1, e2) - max(s1, s2))
    return best


def test_fanout_members_overlap_in_wall_clock(tmp_path, monkeypatch) -> None:
    """Default fan-out: all members reach the barrier together and their
    intervals overlap — the advertised concurrency is real, not just a flag.
    The generous barrier timeout costs nothing while the members overlap; it
    only burns time in the regressed (serial) case this test exists to catch."""
    monkeypatch.delenv("NOETA_SUBTASK_CONCURRENCY", raising=False)
    monkeypatch.delenv("NOETA_MAX_SUBTASK_CONCURRENCY", raising=False)
    probe = _run(tmp_path, [GOAL_A, GOAL_B, GOAL_C], barrier_timeout=5.0)
    assert len(probe.intervals) == 3
    assert not probe.barrier_broke, "barrier broke -> members ran SERIALLY"
    overlap = _max_overlap(probe.intervals)
    assert overlap > MEMBER_SLEEP * 0.5, f"insufficient overlap: {overlap:.3f}s"


def test_escape_valve_forces_serial_execution(tmp_path, monkeypatch) -> None:
    """``NOETA_SUBTASK_CONCURRENCY=0`` must plumb through to real serial
    execution: member #1 breaks the barrier on timeout (member #2 never
    arrived) and the intervals do not overlap at all. The short timeout IS
    this test's runtime, so it stays small."""
    monkeypatch.setenv("NOETA_SUBTASK_CONCURRENCY", "0")
    probe = _run(tmp_path, [GOAL_A, GOAL_B], barrier_timeout=1.5)
    assert probe.barrier_broke, "members overlapped despite the escape valve"
    assert _max_overlap(probe.intervals) == 0.0
