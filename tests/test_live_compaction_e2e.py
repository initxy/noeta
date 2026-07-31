"""Real-LLM context compaction, end to end (live marker).

Answers one question a fake provider cannot: **does compaction actually fire and
produce a real summary against a live model?** The fake-LLM suite already pins
the mechanism deterministically (boundary math in
``test_compaction_boundary_alignment.py``, the event contract in
``test_engine_compaction_step.py``, the tail-prune in
``test_composer_tail_pruning.py``). What none of them prove is that a real model,
handed the fixed summarize prompt, returns a usable structured note that the
Engine accepts — so this drives the **real** ``Engine`` + ``ReActPolicy`` +
``ThreeSegmentComposer`` + ``RuntimeLLMClient`` with a live provider.

Scope — what this file targets and what it does NOT:

* **Macro layer (summarize round-trip)** — the real LLM call. This is the whole
  point: assert a ``CompactionRequested`` + ``Compacted`` pair lands, the summary
  body is stored and non-empty and carries the fixed structured sections, the
  boundary advanced past 0, and the session stays healthy (no anti-spiral fail).
* **Micro layer (composer tail-prune)** — the deterministic clearing of stale
  tool-result output to ``[tool output cleared]``. Deliberately NOT a live test:
  it is a pure function of history (no LLM), already covered end to end by
  ``test_three_segment_composer_prune.py`` (``compose`` →
  ``ContextPlan.cleared_outputs`` populated + off-prompt dereference) and
  ``test_composer_tail_pruning.py``. In a live run it cannot be isolated anyway —
  the prune valve and the summarize trigger share ONE water mark, and the
  summarize round-trip fires first, folding stale tool output into the summary
  before the prune is the visible actor. A real model adds nothing to proving a
  deterministic char-budget walk.
* **NOT in scope**: the unrelated ``CompactionWorker`` snapshot mechanism (emits
  ``TaskSnapshot``, no LLM); and "hot/cold" / tiered compaction, which does not
  exist in this codebase (the only tiering is memory recall). The "verbatim
  tail" is the sole hot/cold distinction and it is a property of the boundary,
  covered by the fake suite.

Forcing the trigger: the live ``SdkHost`` path derives its window from the model
catalog, and an uncatalogued gateway model resolves to ``COMPACTION_OFF`` — so
we construct ``ReActPolicy`` directly with a deliberately SMALL
``context_window`` (the same lever ``test_compaction_boundary_alignment.py``
uses), which makes a modest seeded history overflow within one step.
``max_output_tokens`` is kept GENEROUS on purpose: the summarize call forwards it
(``react.py``), and a reasoning model that spends a stingy budget on hidden
thinking returns an empty summary → a false ``compaction_summary_failed``.

Config from the git-ignored ``.env`` via ``tests._live_env``; auto-skips in CI.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.context.composer import COMPOSER_VERSION, ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.builtins.react.impl import ReActPolicy
from noeta.protocols.messages import TextBlock
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.llm import RuntimeLLMClient
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)

from tests import _live_env

pytestmark = pytest.mark.live


_SYSTEM_PROMPT = (
    "You are a coding assistant working a long task. Keep working, using the "
    "'work' tool when you need to record progress, and reply 'done' when the "
    "task is complete."
)

# Window sizing (in real provider tokens). The available history window is
# ``context_window - max_output_tokens - compaction_buffer`` (floored at 0), so
# the output cap MUST sit well under the window or available collapses to 0 and
# every compose triggers a compaction that cannot make progress. Here available
# = 8000 - 1500 - 500 = 6000: a seeded history above ~6000 tokens overflows and
# trips the proactive trigger once, and after the summary replaces the prefix the
# note (a few hundred tokens) plus the protected tail fits comfortably back under
# 6000, so the session then runs to a clean terminal instead of spiralling.
_CONTEXT_WINDOW = 8000
_MAX_OUTPUT_TOKENS = 1500
_COMPACTION_BUFFER = 500
_TAIL_TOKEN_BUDGET = 300

# Bulk carried on each seeded user turn so the history overflows the tiny
# window. Text blocks are never tail-pruned, so this is the part that reliably
# trips the proactive estimate.
_BULK = "context line that must be summarized when the window overflows. "


class _WorkTool:
    """A trivial recording tool the model may call while working the task. Not
    essential to the macro assertion (a text-only session compacts just as
    well), but present so a live session has a realistic tool to reach for."""

    name = "work"
    risk_level = "low"
    input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG002
        step = arguments.get("step", "?")
        return ToolResult(success=True, output=f"work step {step} recorded", summary="ok")


def _build_stack():
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)
    provider = _live_env.build_anthropic_provider()
    llm = RuntimeLLMClient(
        provider=provider, event_log=event_log, content_store=content_store
    )
    tools = {"work": _WorkTool()}
    policy = ReActPolicy(
        llm=llm,
        tools=tools,
        system_prompt=_SYSTEM_PROMPT,
        model=_live_env.live_model(),
        max_steps=12,
        context_window=_CONTEXT_WINDOW,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        compaction_buffer=_COMPACTION_BUFFER,
        tail_token_budget=_TAIL_TOKEN_BUDGET,
        composer_version=COMPOSER_VERSION,
    )
    composer = ThreeSegmentComposer(
        system_prompt=_SYSTEM_PROMPT,
        tools=tools,
        content_store=content_store,
        tail_token_budget=_TAIL_TOKEN_BUDGET,
    )
    tool_runtime = ToolRuntime(event_log=event_log, content_store=content_store)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools=tools,
        tool_runtime=tool_runtime,
        hooks=HookManager(),
    )
    return engine, event_log, content_store, dispatcher


def _seed_and_run(engine: Engine, dispatcher: InMemoryDispatcher, *, turns: int):
    task = engine.create_task(goal="long task with lots of early context", policy_name="react")
    dispatcher.enqueue(task.task_id)
    # A compaction step makes TWO real round-trips (the main turn + the summarize
    # call) against a reasoning gateway, which can run well past the default 30s
    # lease TTL. A lapsed lease rejects the next emit with InvalidLease mid-step
    # — a harness artifact, not a compaction fault — so lease generously.
    lease = dispatcher.lease(worker_id="live-compaction", lease_seconds=600.0)
    assert lease is not None
    # Seed a long user history — enough bulk that the first composed request
    # overflows the window (available ~6000 tokens ≈ 24000 chars under the
    # chars/4 estimate) and the proactive trigger fires before the first main
    # round-trip. Each turn carries ~1600 chars (~400 tokens); the caller picks
    # a turn count well above the overflow point.
    for i in range(turns):
        engine.append_user_message(
            task,
            content=[TextBlock(text=f"turn {i}: " + _BULK * 28)],
            lease_id=lease.lease_id,
        )
    final = engine.run_one_step(task, lease_id=lease.lease_id)
    return task, final


# --------------------------------------------------------------------------- #
# Macro layer — the real summarize round-trip
# --------------------------------------------------------------------------- #


@_live_env.requires_live
def test_live_compaction_produces_real_summary() -> None:
    engine, event_log, content_store, dispatcher = _build_stack()
    task, final = _seed_and_run(engine, dispatcher, turns=30)

    events = list(event_log.read(task.task_id))
    types = [e.type for e in events]

    # The proactive trigger fired and a real summary was accepted.
    assert "CompactionRequested" in types, types
    compacted = [e for e in events if e.type == "Compacted"]
    assert compacted, f"no Compacted event — compaction did not complete; {types}"

    payload = compacted[0].payload
    # The boundary advanced past 0: a non-empty prefix was actually collapsed.
    assert payload.boundary_count > 0
    assert payload.replaced_count > 0

    # The summary body is real, non-empty, and carries the structured note the
    # fixed prompt asks for. Wording is model-dependent, so match on the section
    # scaffold, not verbatim content.
    summary_text = content_store.get(payload.summary_ref).decode("utf-8")
    assert summary_text.strip(), "stored summary body is empty"
    lowered = summary_text.lower()
    # At least a couple of the seven mandated sections should surface.
    section_hits = sum(
        kw in lowered
        for kw in ("intent", "technical", "files", "errors", "user", "pending", "decision")
    )
    assert section_hits >= 2, f"summary lacks structured sections:\n{summary_text}"

    # Fold applied it: the task's summary boundary matches the event.
    folded = fold(event_log, content_store, task.task_id)
    assert folded.context.summary_boundary == payload.boundary_count

    # Compaction kept the session healthy: the post-summary context fits back
    # under the window, so the step reaches a clean terminal instead of the
    # anti-spiral killing it (compaction_no_progress / compaction_overflow_spiral).
    assert final.status == "terminal"
    assert "TaskFailed" not in types, types
