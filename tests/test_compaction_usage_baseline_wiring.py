"""Seam regression — the real-usage compaction baseline must survive a
multi-step tool loop, not just a lease boundary.

``ReActPolicy._trigger_estimate`` mixes the provider's REAL recorded input
count with a chars/4 estimate of what was appended since. That real baseline is
the only defence against the chars/4 heuristic under-counting a payload: CJK
text, JSON structure, base64 thinking signatures and tool schemas all tokenise
far denser than the 4-chars-per-token rule of thumb assumes, and a production
session was measured at ~1.2 chars/token — a ~4x under-read, enough to sail
past a 200k window with the trigger still reading ~54k.

Both sides of the seam were already covered in isolation:

* ``test_governance_fold_counters`` asserts ``fold`` writes
  ``RuntimeState.last_input_tokens`` from ``Usage.input``;
* ``test_react_policy_prune_summarize`` asserts the mix, injecting
  ``last_input_tokens`` straight into a hand-built ``StepContext``.

Neither exercised the WIRE between them, and the wire was broken. ``Engine``
rebuilds ``StepContext`` each turn from ``task.runtime.last_input_tokens``, but
that field is only ever written by ``fold`` — and the ``LLMRequestFinished``
the LLM client emits mid tool-loop is appended straight to the EventLog without
being applied to the in-memory ``task`` (``Engine._emit`` emits; it does not
``apply_event``). So inside one ``Engine.run_one_step`` the ctx baseline stayed
frozen at the entry value (``0`` on a first turn) and the trigger silently
degraded to the pure estimate for the WHOLE turn, however long the tool loop
ran — precisely where an under-counting estimate is most dangerous.

The provider here models the real shape: it reports an input count
``_DENSITY``x the chars/4 estimate of the request it was handed, so reported
usage rises and falls WITH the history the way a real gateway's does.
"""

from __future__ import annotations

from typing import Any

from noeta.context.composer import _COMPOSER_VERSION, ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.policies.react import ReActPolicy
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.token_estimate import estimate_messages_tokens
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.llm import RuntimeLLMClient
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


_SYSTEM_PROMPT = "You are a coding agent. Work the task then finish."
_SUMMARY_MARKER = "Summarize the conversation so far"

#: How much denser the provider tokenises than chars/4 assumes. 4x is the ratio
#: measured on a real CJK+JSON+signature session (est 54,426 / real 215,836).
_DENSITY = 4

#: Window knobs. ``available`` = 5200-100-100 = 5000 estimated tokens. The
#: history below never estimates anywhere near that, so ONLY the real-usage
#: baseline (estimate x _DENSITY) can cross it — which is the whole point: a
#: run that compacts here proves the real count reached the trigger.
_CONTEXT_WINDOW = 5_200
_MAX_OUTPUT = 100
_BUFFER = 100
_AVAILABLE = _CONTEXT_WINDOW - _MAX_OUTPUT - _BUFFER

#: Protected tail, in chars/4 units — small enough that a summary boundary can
#: actually advance through this history.
_TAIL = 150

_BULK = "z" * 700


class _NoopTool:
    name = "work"
    risk_level = "low"
    input_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
    }

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext  # noqa: ARG002
    ) -> ToolResult:
        return ToolResult(success=True, output="ok", summary="ok")


class _DenseTokenizingProvider:
    """Reports ``_DENSITY``x the chars/4 estimate of the request it is handed.

    Models a gateway whose tokeniser is denser than the heuristic: reported
    usage tracks the real history, so it DROPS after a compaction the way a
    real one does (a provider that reported a flat number would re-trigger
    forever and prove nothing).
    """

    def __init__(self, turns: int) -> None:
        self.turns = turns
        self.summarize_calls = 0
        self.main_calls = 0
        self.reported: list[int] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        system_text = ""
        if request.system is not None:
            system_text = "".join(
                b.text
                for b in request.system.content
                if isinstance(b, TextBlock)
            )
        usage = Usage(
            uncached=estimate_messages_tokens(request.messages) * _DENSITY
        )
        if _SUMMARY_MARKER in system_text:
            self.summarize_calls += 1
            return LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="CONDENSED-SUMMARY")],
                usage=usage,
            )
        self.main_calls += 1
        self.reported.append(usage.input)
        if self.main_calls > self.turns:
            return LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="all done")],
                usage=usage,
            )
        return LLMResponse(
            stop_reason="tool_use",
            content=[
                TextBlock(text=f"step {self.main_calls}: " + _BULK),
                ToolUseBlock(
                    call_id=f"call-{self.main_calls}",
                    tool_name="work",
                    arguments={"step": self.main_calls},
                ),
            ],
            usage=usage,
        )


def _tools() -> dict[str, Any]:
    return {"work": _NoopTool()}


def _run(
    *, seed_turns: int, tool_turns: int
) -> tuple[str, InMemoryEventLog, _DenseTokenizingProvider]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)
    provider = _DenseTokenizingProvider(turns=tool_turns)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=ThreeSegmentComposer(
            system_prompt=_SYSTEM_PROMPT,
            tools=_tools(),
            content_store=content_store,
            tail_token_budget=_TAIL,
        ),
        policy=ReActPolicy(
            llm=RuntimeLLMClient(
                provider=provider,
                event_log=event_log,
                content_store=content_store,
            ),
            tools=_tools(),
            system_prompt=_SYSTEM_PROMPT,
            model="gpt-4o",
            max_steps=30,
            context_window=_CONTEXT_WINDOW,
            max_output_tokens=_MAX_OUTPUT,
            compaction_buffer=_BUFFER,
            tail_token_budget=_TAIL,
            composer_version=_COMPOSER_VERSION,
        ),
        tools=_tools(),
        tool_runtime=ToolRuntime(
            event_log=event_log, content_store=content_store
        ),
        hooks=HookManager(),
    )
    task = engine.create_task(goal="long single turn", policy_name="react")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="rec")
    assert lease is not None
    for i in range(seed_turns):
        engine.append_user_message(
            task,
            content=[TextBlock(text=f"turn-{i} " + "y" * 300)],
            lease_id=lease.lease_id,
        )
    engine.run_one_step(task, lease_id=lease.lease_id)
    return task.task_id, event_log, provider


def test_real_usage_crosses_window_mid_tool_loop_and_compacts() -> None:
    """The core regression: within ONE ``run_one_step``, a history whose REAL
    token count crosses the window must compact — even though the chars/4
    estimate stays far below it.

    Pre-fix the baseline froze at ``0`` for the whole turn, the trigger fell
    back to the pure estimate, and nothing ever compacted while the real
    context sailed past the window.
    """
    task_id, log, provider = _run(seed_turns=14, tool_turns=6)
    types = [e.type for e in log.read(task_id)]

    # The real count DID cross the window (else the test proves nothing).
    assert max(provider.reported) >= _AVAILABLE, provider.reported
    # …and the chars/4 estimate never did — only the real baseline could fire.
    estimates = [
        e.payload.estimated_tokens
        for e in log.read(task_id)
        if e.type == "CompactionRequested"
    ]
    assert all(est < _AVAILABLE for est in estimates), estimates

    assert "Compacted" in types, types
    assert "TaskFailed" not in types, types


def test_baseline_invalidated_by_compaction_does_not_respin() -> None:
    """A compaction collapses the history, so every real count in hand
    describes a history that no longer exists.

    Keeping one would pin the trigger to a stale pre-compaction high: the next
    step re-fires on a just-shrunk history whose boundary can no longer
    advance, and the task dies on ``compaction_no_progress``. The session must
    instead complete.
    """
    task_id, log, _p = _run(seed_turns=14, tool_turns=6)
    events = log.read(task_id)
    types = [e.type for e in events]

    assert "TaskCompleted" in types, types
    assert "TaskFailed" not in types, types
    fails = [
        e for e in events
        if e.type == "TaskFailed"
        and getattr(e.payload, "reason", "") == "compaction_no_progress"
    ]
    assert not fails, fails


def test_recorded_usage_is_durable_in_the_event_log() -> None:
    """Guard rail: the usage the baseline rides on IS on the event stream.

    If this passes while the tests above fail, the defect is provably the wire
    (fold projection → policy), not the recording.
    """
    task_id, log, _p = _run(seed_turns=14, tool_turns=6)
    finished = [e for e in log.read(task_id) if e.type == "LLMRequestFinished"]
    assert finished, [e.type for e in log.read(task_id)]
    assert any(e.payload.usage.input > 0 for e in finished)
