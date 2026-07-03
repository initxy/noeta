"""fold: rebuild a ``Task`` from an EventLog (+ ContentStore).

Both the from-scratch path and the snapshot-accelerated path (skip the
prefix-scan when a snapshot exists) live here.

The function intentionally takes no other inputs: ``fold(eventlog,
contentstore, task_id)`` is the entire signature, matching the SDD.
"""

from __future__ import annotations

import logging

from noeta.core.snapshot import deserialize_task_state, rehydrate_task
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.event_log import EventLogReader
from noeta.protocols.events import EventEnvelope
from noeta.protocols.messages import Message
from noeta.protocols.task import Task, TaskState
from noeta.protocols.tool_args import resolve_tool_call_arguments

_log = logging.getLogger(__name__)


def fold(
    event_log: EventLogReader,
    content_store: ContentStore,
    task_id: str,
    *,
    ignore_snapshots: bool = False,
) -> Task:
    """Reconstruct the current ``Task`` state from its event stream.

    ``ignore_snapshots`` exists so tests can verify that a from-scratch
    fold produces byte-equal state vs a snapshot-accelerated fold.
    """
    snap = None if ignore_snapshots else event_log.find_latest_snapshot(task_id)
    if snap is not None:
        body = content_store.get(snap.payload.state_ref)
        state_dict = deserialize_task_state(body)
        if _snapshot_is_legacy_for_issue18(state_dict):
            # Pre-issue-18 snapshot bodies do not carry the governance
            # accumulation fields fold now relies on. Treating them
            # as authoritative would let BudgetGuard read undercounted
            # values for the snapshot's event prefix. Discard the
            # snapshot and fall back to the from-scratch path; the
            # next ``_write_snapshot`` from a post-18 Engine will
            # produce a body that reactivates the accelerated path.
            events = event_log.read(task_id)
            task = _bootstrap_from_genesis(events, task_id)
            tail = events[1:] if events else []
        else:
            task = rehydrate_task(state_dict)
            tail = event_log.read(task_id, after_seq=snap.seq)
    else:
        events = event_log.read(task_id)
        task = _bootstrap_from_genesis(events, task_id)
        tail = events[1:] if events else []

    for env in tail:
        _apply_event(task, env, content_store)
    return task


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _snapshot_is_legacy_for_issue18(state_dict: dict[str, object]) -> bool:
    """Detect snapshots written before issue 18 introduced governance
    accumulation.

    The presence of ``spawned_subtasks`` in the governance dict is the
    stable schema sentinel: pre-18 fold never wrote it, so any snapshot
    body without that key was produced by old code where
    ``iterations / tool_calls / cost_usd / denied`` carried only their
    default zeros — i.e. they do not represent the real prefix counters
    BudgetGuard would need. Treating those snapshots as authoritative
    would silently undercount and defeat the fold-from-EventLog
    Guard-read model.
    """
    governance = state_dict.get("governance", {})
    if not isinstance(governance, dict):
        return False
    return "spawned_subtasks" not in governance


def _bootstrap_from_genesis(events: list[EventEnvelope], task_id: str) -> Task:
    if not events:
        return Task(task_id=task_id)
    genesis = events[0]
    if genesis.type != "TaskCreated":
        raise ValueError(
            f"task {task_id!r} first event is {genesis.type!r}, expected TaskCreated"
        )
    payload = genesis.payload
    return Task(
        task_id=task_id,
        status="pending",
        parent_task_id=getattr(payload, "parent_task_id", None),
        subtask_depth=getattr(payload, "subtask_depth", 0),
        state=TaskState(goal=getattr(payload, "goal", "")),
    )


def messages_from_appended(
    env: EventEnvelope, content_store: ContentStore
) -> list[Message]:
    """Issue 14: dereference ``MessagesAppendedPayload.messages_ref`` to
    the underlying ``list[Message]`` body.

    Inspect / CLI all hit this same pattern (the payload only
    carries the ref + count to keep the envelope under the
    4 KB ceiling). Centralised so a backend swap (Sqlite, S3) is one
    callsite.
    """
    body = content_store.get(env.payload.messages_ref)
    restored = from_canonical_bytes(body)
    return list(restored)


def apply_event(
    task: Task, env: EventEnvelope, content_store: ContentStore
) -> None:
    """Live-path entrypoint to the same handlers fold uses on resume.

    Engine calls this after emitting events whose effect on the Task
    state lives exclusively under fold's ownership (issue 14:
    ``ContextPlanComposed``; single-writer means the
    ``task.context.plan_ref = ...`` line must stay inside this module).
    Keeping live and resume paths converged through one handler set
    means a snapshot taken mid-step captures the same state fold would
    rebuild from the prefix.

    ``content_store`` is required because issue 14's
    ``MessagesAppended`` handler dereferences ``messages_ref`` to
    rebuild ``RuntimeState.messages``.
    """
    _apply_event(task, env, content_store)


def _apply_event(
    task: Task, env: EventEnvelope, content_store: ContentStore
) -> None:
    """Route an event to the correct slice's reducer."""
    handler = _HANDLERS.get(env.type)
    if handler is None:
        # Unknown types are intentionally non-fatal so future schema
        # additions never break resume of historical streams (SDD: "adding
        # an event type does not affect old folds"). We log a warning so
        # the divergence is visible in inspect output.
        _log.warning(
            "fold: unknown event type %r at seq=%d on task %r; skipping",
            env.type,
            env.seq,
            env.task_id,
        )
        return
    handler(task, env, content_store)


def _on_task_created(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # TaskCreated already consumed by _bootstrap_from_genesis; no-op here.
    return


def _on_task_started(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.status = "running"


def _on_task_state_patched(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    raw = env.payload.patch
    patch = (
        raw if isinstance(raw, TaskStatePatch) else TaskStatePatch.from_dict(raw)
    )
    patch.apply(task.state)


def _on_messages_appended(
    task: Task, env: EventEnvelope, content_store: ContentStore
) -> None:
    # Issue 14: dereference messages_ref from ContentStore. Frozen
    # Messages need no defensive copy.
    for m in messages_from_appended(env, content_store):
        task.runtime.messages.append(m)


def _on_task_snapshot(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # In the snapshot-accelerated path we never reach here; in the from-
    # scratch path the snapshot body is the same state we are already
    # rebuilding, so it is intentionally a no-op.
    return


def _on_task_rewound(
    task: Task, env: EventEnvelope, content_store: ContentStore
) -> None:
    # The rewind baseline. In the accelerated path
    # ``find_latest_snapshot`` already returned the latest TaskRewound (it
    # carries a ``state_ref`` like TaskSnapshot), rehydrated it, and started the
    # tail after it — so this handler is never reached there. In the from-scratch
    # (``ignore_snapshots``) path we DO reach it while resuming the full prefix:
    # the marker re-bases the conversation onto the state folded at
    # ``target_seq``, discarding everything the dead ``target_seq+1..M`` segment
    # accreted. We rehydrate the recorded baseline and overwrite the working
    # Task's slices in place (the fold loop holds one Task reference, so a rebind
    # would not propagate). This makes a from-scratch fold land byte-equal to the
    # accelerated fold — the invariant resume relies on. Append-only is intact:
    # nothing on the stream is rewritten, the marker simply names a new baseline.
    body = content_store.get(env.payload.state_ref)
    baseline = rehydrate_task(deserialize_task_state(body))
    task.status = baseline.status
    task.parent_task_id = baseline.parent_task_id
    task.subtask_depth = baseline.subtask_depth
    task.runtime = baseline.runtime
    task.state = baseline.state
    task.context = baseline.context
    task.governance = baseline.governance
    task.wake_on = baseline.wake_on


def _on_task_completed(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.status = "terminal"


def _on_task_failed(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.status = "terminal"


def _on_task_suspended(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.status = "suspended"
    task.wake_on = env.payload.wake_on


def _on_task_woken(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.status = "running"
    task.wake_on = None


def _on_subtask_spawned(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.governance.spawned_subtasks += 1


def _on_subtask_completed(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.governance.subtask_results.append(env.payload.result)


def _on_context_plan_composed(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Issue 14 / Grill round 2 #10: ContextState's single writer is
    # Engine fold (from this event). Composer computes the plan body
    # but never mutates ``task.context`` directly — that would break
    # the Composer's pure-function contract.
    task.context.plan_ref = env.payload.plan_ref
    # Issue 18 / core #2: ``ContextPlanComposed`` is emitted
    # **unconditionally once per Engine step** — with ``plan_ref=None``
    # when the composer produced no stored plan (the protocols-only
    # ``PassthroughComposer`` fallback) — so it is the step-boundary
    # event this counter folds from regardless of which composer is
    # wired, and ``BudgetGuard.max_iterations`` is never inert.
    # Byte-safety: the shipped ``ThreeSegmentComposer`` always set
    # ``plan_ref``, so historical recordings are unchanged; only
    # Passthrough steps (which previously emitted nothing and never
    # counted) gained the event.
    task.governance.iterations += 1


def _on_tool_call_started(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Issue 18: count tool invocations as they begin so the in-flight
    # call is visible to BudgetGuard *before* the next one would be
    # admitted.
    task.governance.tool_calls += 1


def _on_llm_request_finished(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Issue 18: accumulate per-step LLM cost the provider reported.
    # Adapters that cannot price a call leave ``cost_usd`` at 0, which
    # contributes nothing — the cap is still enforceable for adapters
    # that do report cost.
    cost = float(getattr(env.payload, "cost_usd", 0.0) or 0.0)
    if cost > 0.0:
        task.governance.cost_usd += cost

    # Foundation A (D-A3): accumulate per-token counters from the typed Usage.
    # ``getattr`` tolerance is the byte-safe seam: an old recording's
    # LLMRequestFinished payload has no ``usage`` field → ``None`` →
    # nothing accumulates, so a from-scratch fold of an old stream lands
    # the same zero counters it always did. ``input`` is the derived
    # uncached+cache_read+cache_write total (kept distinct from the cache
    # breakdown so ① can price them at different unit rates).
    usage = getattr(env.payload, "usage", None)
    if usage is not None:
        task.governance.input_tokens += usage.input
        task.governance.output_tokens += usage.output
        task.governance.cache_read_tokens += usage.cache_read
        task.governance.cache_write_tokens += usage.cache_write
        task.governance.reasoning_tokens += usage.reasoning_tokens
        # Also project the LAST-turn input total (last-write-wins)
        # onto RuntimeState so the compaction trigger can use the real recorded
        # size as its history baseline. Unlike the governance accumulators above
        # this is NOT a running sum: each finished round-trip OVERWRITES it with
        # that turn's ``Usage.input``. Reading an already-recorded value keeps
        # resume re-derivation consistent (a refold lands the same baseline) —
        # we never re-count tokens live.
        task.runtime.last_input_tokens = usage.input


def _on_tool_call_denied(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.governance.denied.append(
        {
            "type": "ToolCallDenied",
            "call_id": env.payload.call_id,
            "tool_name": env.payload.tool_name,
            "reason": env.payload.reason,
        }
    )


def _on_tool_call_approval_requested(
    task: Task, env: EventEnvelope, content_store: ContentStore
) -> None:
    # Phase 4.5 Issue A: record the blocked call as the durable recovery
    # anchor so the approval-resume path can reconstruct the exact
    # ToolCall from the EventLog/snapshot after a restart. Resolve the
    # arguments back to a plain dict here (dereferencing ``arguments_ref``
    # from the ContentStore for offloaded large calls) so every downstream
    # reader of ``pending_approvals`` — engine resume, the detail read
    # model — sees the real arguments without knowing about the offload.
    task.governance.pending_approvals[env.payload.call_id] = {
        "tool_name": env.payload.tool_name,
        "arguments": resolve_tool_call_arguments(env.payload, content_store),
    }


def _on_tool_call_approval_resolved(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Phase 4.5 Issue A: the single authoritative resolution record.
    # Append to the audit list, clear the pending anchor, and — on a
    # deny — also append to the established ``denied`` governance
    # counter (no separate ToolCallDenied event is emitted for a human
    # deny).
    task.governance.pending_approvals.pop(env.payload.call_id, None)
    task.governance.approvals.append(
        {
            "call_id": env.payload.call_id,
            "tool_name": env.payload.tool_name,
            "approved": env.payload.approved,
            "reason": env.payload.reason,
            "resolver": env.payload.resolver,
        }
    )
    if not env.payload.approved:
        task.governance.denied.append(
            {
                "type": "ToolCallApprovalResolved",
                "call_id": env.payload.call_id,
                "tool_name": env.payload.tool_name,
                "reason": env.payload.reason,
            }
        )


def _on_user_question_requested(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.governance.pending_questions[env.payload.question_id] = {
        "call_id": env.payload.call_id,
        "questions_ref": env.payload.questions_ref,
        "question_count": env.payload.question_count,
        "reason": env.payload.reason,
    }


def _on_user_question_answered(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.governance.pending_questions.pop(env.payload.question_id, None)
    task.governance.question_answers.append(
        {
            "question_id": env.payload.question_id,
            "call_id": env.payload.call_id,
            "answers_ref": env.payload.answers_ref,
            "answer_count": env.payload.answer_count,
            "answered_by": env.payload.answered_by,
        }
    )


def _on_subtask_denied(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    task.governance.denied.append(
        {
            "type": "SubtaskDenied",
            "agent_name": env.payload.agent_name,
            "goal": env.payload.goal,
            "reason": env.payload.reason,
        }
    )


def _on_task_cancelled(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # TaskCancelled is a terminal lifecycle event per SDD; the
    # governance record is part of the audit trail, not a status
    # change in itself. Promote the task to ``terminal`` here so
    # fold doesn't leave it in ``running`` / ``suspended`` after a
    # cancel arrives.
    task.status = "terminal"
    task.wake_on = None
    task.governance.denied.append(
        {
            "type": "TaskCancelled",
            "reason": env.payload.reason,
            "cascade": env.payload.cascade,
        }
    )


def _on_model_bound(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Issue 06 (D2 / D3): fold the latest model binding
    # into GovernanceState so the resolver keys the Engine on
    # ``(agent_name, model)`` and inspect can trace every binding back to
    # the authorizing Principal. Writer is the Engine under a driver
    # command (validated *before* the emit), not a policy Decision.
    model = str(getattr(env.payload, "model", ""))
    principal_identity = str(getattr(env.payload, "principal_identity", ""))
    task.governance.model_binding = model
    task.governance.principal_identity = principal_identity
    # (I4): provider folds into the
    # same model binding. When ``None`` (an old recording has no provider field,
    # or a turn switched only the model), do **not** overwrite the existing
    # provider_binding — provider carries over from the current binding (per-turn
    # switch semantics: pass one and the other holds); the resolver falls back to
    # the host default when the value is missing.
    provider = getattr(env.payload, "provider", None)
    if provider is not None:
        task.governance.provider_binding = str(provider)
    task.governance.model_bindings.append(
        {
            "model": model,
            "principal_identity": principal_identity,
            "provider": provider,
        }
    )


def _on_task_host_bound(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # D4: fold the durable server host identity into GovernanceState.
    # Emitted once at task open on the server product path; old / non-server
    # recordings have no TaskHostBound → these stay None. The earlier
    # host/registry digest folds were retired along with the test infrastructure
    # that consumed them.
    task.governance.host_id = str(getattr(env.payload, "host_id", "")) or None
    # The per-session workspace absolute path is welded into durable state.
    # Legacy name-style records carried ``workspace`` (a name); those are superseded
    # and fold to None here — the resolver falls back to its host-fixed default
    # dir (D7 clean break: name-style records fold this field to None).
    task.governance.workspace = (
        str(getattr(env.payload, "workspace_dir", "") or "") or None
    )


def _on_mcp_provenance_recorded(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # (issue 07): fold the per-task MCP provenance into
    # GovernanceState so the inspect / read-model path can answer "which MCP
    # connectors + which tools was this task given this run" by fold (never from
    # an Observer projection). The payload's ``servers`` is already the
    # credential-free, alias-sorted record (names only, no url/token); we copy it
    # verbatim. Emitted once at connect time; a task with no MCP carries no such
    # event → this stays the empty default, byte-equal.
    servers = getattr(env.payload, "servers", None)
    if isinstance(servers, list):
        task.governance.mcp_provenance = [dict(s) for s in servers]


def _on_conversation_closed(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Issue 08 ("No synthesized terminal"): fold the close into
    # GovernanceState so the sessions-list / inspect hot path can query
    # "closed?" by fold — never from an Observer (which is a projection, not
    # state of record). This is a lifecycle flag ORTHOGONAL to task.status:
    # we deliberately do NOT touch ``task.status`` here — a closed
    # conversation stays ``suspended`` (no manufactured terminal). Writer is
    # the Engine under the driver's close command, not a policy Decision.
    closed_by = str(getattr(env.payload, "closed_by", ""))
    reason = getattr(env.payload, "reason", None)
    task.governance.closed = True
    task.governance.closed_by = closed_by
    task.governance.close_reason = reason
    task.governance.conversation_lifecycle.append(
        {"event": "closed", "by": closed_by, "reason": reason}
    )


def _on_conversation_reopened(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Issue 08: the audit-symmetric reopen. Clears the ``closed`` flag (a new
    # goal on a closed+suspended Task already works regardless — reopen is
    # advisory, not a lock). Like its sibling, leaves ``task.status`` alone.
    reopened_by = str(getattr(env.payload, "reopened_by", ""))
    reason = getattr(env.payload, "reason", None)
    task.governance.closed = False
    task.governance.closed_by = None
    task.governance.close_reason = None
    task.governance.conversation_lifecycle.append(
        {"event": "reopened", "by": reopened_by, "reason": reason}
    )


def _on_step_transition_marked(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Foundation B (D-B3): project the latest non-default continuation tag onto
    # ``RuntimeState.last_transition`` (last-write-wins — successive marks
    # simply overwrite, so the guard reads the most recent one). D-B5: an
    # unknown ``reason`` from a newer producer is NOT rejected here; we store
    # the raw value so inspect can see the drift, mirroring fold's
    # warning-not-fatal stance on unknown event types.
    task.runtime.last_transition = env.payload.reason


#: ⑥ compaction thrashing detection thresholds (D6.1). Aligned
#: with Claude Code's "Autocompact is thrashing" heuristic — "refilled to the
#: limit within 3 turns of the previous compact, 3 times in a row".
#: ``_THRASH_CLOSE_TURNS`` (K=3) is the turn-gap that still counts as a "close"
#: refill; ``_THRASH_RUN_LIMIT`` (M=3) is how many consecutive close refills
#: latch the thrashing flag. v1 is not configurable (constants, not Options).
_THRASH_CLOSE_TURNS = 3
_THRASH_RUN_LIMIT = 3


def _on_compacted(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # ③ (D-3): project the compaction result onto ContextState (single
    # writer). The first ``boundary_count`` messages have been
    # collapsed into the summary behind ``summary_ref``; the Composer reads
    # this slice next compose to swap the covered prefix for the summary.
    # ``CompactionRequested`` is observational (no state) → registered as a
    # no-op below.
    task.context.summary_ref = env.payload.summary_ref
    task.context.summary_boundary = env.payload.boundary_count
    # ⑥ thrashing detection (D6.1/D6.2): measure the turn-gap between this
    # ``Compacted`` and the previous one and latch a flag when several land
    # back-to-back. The gap is measured in ``GovernanceState.iterations`` — the
    # per-compose turn counter fold maintains from ``ContextPlanComposed`` (one
    # per Engine step, see ``_on_context_plan_composed``). It is chosen over
    # react.py's ``_step_count`` (a Policy *instance* attribute fold cannot see)
    # and over ``len(runtime.messages)`` (grows a variable 1-2 per turn, a
    # noisier proxy for "turns") because it is fold-visible, strictly monotonic,
    # EventLog-reconstructable, and maps 1:1 to Claude Code's "within N turns"
    # unit. The triggering compose's ``ContextPlanComposed`` is folded before
    # this ``Compacted``, so the counter already reflects this compaction's turn.
    # Complementary to the anti-spiral guard (a compaction with NO boundary
    # progress → FailDecision, in ``handle_compaction_requested``): thrashing is
    # the opposite case — compaction DOES make progress but the freed window is
    # immediately refilled by re-reading the same large content.
    marker = task.governance.iterations
    prev = task.context.last_compaction_marker
    if prev is not None and (marker - prev) <= _THRASH_CLOSE_TURNS:
        task.context.close_compaction_run += 1
    else:
        # First compaction (no prior marker) or a distant one (gap > K) — start
        # the run over. A distant compaction here is what clears a previously
        # latched ``compaction_thrashing`` flag below.
        task.context.close_compaction_run = 0
    task.context.last_compaction_marker = marker
    task.context.compaction_thrashing = (
        task.context.close_compaction_run >= _THRASH_RUN_LIMIT
    )


def _on_assistant_thinking_recorded(
    task: Task, env: EventEnvelope, content_store: ContentStore
) -> None:
    # Extended-thinking end-to-end (Slice B): deref the turn's ThinkingBlocks
    # and write them into ContextState under the turn's first tool_use
    # ``call_id`` (single writer). The Composer reads this slice to
    # re-attach the thinking ahead of the tool_use on the next compose. The
    # blocks are content-addressed (``thinking_ref``) so live + resume deref
    # the identical bytes — last-write-wins per call_id (an id is unique to
    # one turn, so there is never a real collision). Old recordings carry no
    # such event → the slice stays its empty default, byte-equal.
    blocks = list(from_canonical_bytes(content_store.get(env.payload.thinking_ref)))
    task.context.thinking_by_call_id[env.payload.call_id] = blocks


def _on_tool_schema_recorded(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # D3 — per-tool schema-hash provenance. The emission
    # contract guarantees one event per (task, tool_name); fold still uses
    # last-write-wins so a malformed stream with duplicates converges
    # deterministically instead of crashing. Old recordings carry no such
    # event → the dicts stay their empty defaults, byte-equal.
    tool_name = str(getattr(env.payload, "tool_name", ""))
    schema_hash = str(getattr(env.payload, "schema_hash", ""))
    if not tool_name or not schema_hash:
        return
    task.governance.tool_schema_hashes[tool_name] = schema_hash
    task.governance.tool_schema_versions[tool_name] = str(
        getattr(env.payload, "version", "1")
    )


def _on_skill_content_recorded(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # D8 — per-skill content-hash provenance. Same fold
    # discipline as ToolSchemaRecorded: last-write-wins per skill_name,
    # empty defaults keep old recordings byte-equal. Retained read-only
    # for old recordings (D2); it also merges into the generic
    # activation map as the skill-specific route.
    skill_name = str(getattr(env.payload, "skill_name", ""))
    content_hash = str(getattr(env.payload, "content_hash", ""))
    if not skill_name or not content_hash:
        return
    task.governance.skill_content_hashes[skill_name] = content_hash
    task.governance.skill_content_versions[skill_name] = str(
        getattr(env.payload, "version", "1")
    )
    _merge_active_content(task.state, "skill", skill_name)


def _on_context_content_recorded(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # D2 — the generic content-channel provenance
    # event: merge the resident's name into the activation map under its
    # recorded kind. The runtime is kind-neutral here — what a kind means
    # (and its drift policy, carried on the payload) is SDK territory; the
    # drift-comparison consumer has been removed. Blank fields are skipped so a
    # malformed stream converges deterministically instead of crashing.
    kind = str(getattr(env.payload, "kind", ""))
    name = str(getattr(env.payload, "name", ""))
    if not kind or not name or not getattr(env.payload, "content_hash", ""):
        return
    _merge_active_content(task.state, kind, name)


def _merge_active_content(state: TaskState, kind: str, name: str) -> None:
    """Append ``name`` under ``kind`` in the generic activation map
    (order preserved, no duplicates)."""
    names = state.active_content.get(kind, ())
    if name not in names:
        state.active_content[kind] = (*names, name)


def _find_background_job(task: Task, job_id: str) -> dict[str, object] | None:
    """Return the audit entry for ``job_id`` (or ``None`` if not yet started).

    The poll / exit / kill handlers all locate their job this way; a miss is
    defensive (an out-of-order or duplicated stream) and the caller ignores
    it rather than crashing fold.
    """
    for entry in task.governance.background_jobs:
        if entry.get("job_id") == job_id:
            return entry
    return None


def _on_background_shell_started(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # (issue 05): a new background process — append the running
    # audit entry. ``ref`` is the spawn snapshot the front-end derefs until a
    # poll/exit replaces it with a fresher one. Append-only: never removed.
    task.governance.background_jobs.append(
        {
            "job_id": env.payload.job_id,
            "command": env.payload.command,
            "status": "running",
            "spawned_by_task_id": env.payload.spawned_by_task_id,
            "ref": env.payload.ref,
        }
    )


def _on_background_shell_polled(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # Advance the job's ``ref`` to the latest poll snapshot so the
    # drill-in derefs the freshest recorded output. Defensive on a missing job.
    entry = _find_background_job(task, env.payload.job_id)
    if entry is None:
        return
    entry["ref"] = env.payload.ref


def _on_background_shell_exited(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # The process reached terminal naturally. Update status +
    # exit_code and point ``ref`` at the final snapshot — never delete (audit
    # trail). Defensive on a missing job.
    entry = _find_background_job(task, env.payload.job_id)
    if entry is None:
        return
    entry["status"] = "exited"
    entry["exit_code"] = env.payload.exit_code
    entry["ref"] = env.payload.final_ref


def _on_background_shell_killed(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # The process was killed (shell_kill / emergency-stop). Update
    # status + signal (+ exit_code if the payload carries one) — never delete
    # (audit trail). Defensive on a missing job.
    entry = _find_background_job(task, env.payload.job_id)
    if entry is None:
        return
    entry["status"] = "killed"
    entry["signal"] = env.payload.signal
    exit_code = getattr(env.payload, "exit_code", None)
    if exit_code is not None:
        entry["exit_code"] = exit_code


def _on_background_shell_lost(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # The job was orphaned by a host crash/restart — its
    # Started had no terminal, so startup recovery emits Lost. Flip status to
    # "lost" so the read model / model stop seeing it as forever-"running" —
    # never delete (audit trail). Defensive on a missing job (a Lost without a
    # folded Started, e.g. a partial resume window).
    entry = _find_background_job(task, env.payload.job_id)
    if entry is None:
        return
    entry["status"] = "lost"


def _find_background_subagent(
    task: Task, subtask_id: str
) -> dict[str, object] | None:
    for entry in task.governance.background_subagents:
        if entry.get("subtask_id") == subtask_id:
            return entry
    return None


def _on_background_subagent_started(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # (docs/adr/background-subagent.md): a sub-agent was launched in the
    # background — append the running audit entry. The parent did NOT suspend on
    # it, so this entry (not a SubtaskSpawned + suspend pair) is the durable
    # record. Append-only: never removed.
    task.governance.background_subagents.append(
        {
            "subtask_id": env.payload.subtask_id,
            "agent_name": env.payload.agent_name,
            "goal": env.payload.goal,
            "status": "running",
            "call_id": env.payload.call_id,
        }
    )


def _on_background_subagent_delivered(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    # The background sub-agent reached terminal and its result was injected as a
    # turn-boundary notice (Mechanism C). Flip the entry to the child's terminal
    # status and record the result snapshot + summary — this is also the
    # exactly-once DELIVERY ANCHOR (the driver reads a folded "already delivered"
    # so a resume never re-injects). Never delete (audit trail). Defensive on a
    # missing entry (a Delivered without a folded Started, e.g. a partial resume
    # window).
    entry = _find_background_subagent(task, env.payload.subtask_id)
    if entry is None:
        return
    entry["status"] = env.payload.status
    entry["result_ref"] = env.payload.result_ref
    entry["summary"] = env.payload.summary


def _on_noop(
    task: Task, env: EventEnvelope, content_store: ContentStore  # noqa: ARG001
) -> None:
    """Recognised event with no fold-side state effect.

    Used for record-keeping events the live runtime emits but that do
    not contribute to any state slice — registering them silences the
    'unknown event type' warning that would otherwise fire on every
    RuntimeLLMClient recording when fold runs (snapshot /
    resume / guard refold).
    """
    return


_HANDLERS = {
    "TaskCreated": _on_task_created,
    "TaskStarted": _on_task_started,
    "TaskStatePatched": _on_task_state_patched,
    "MessagesAppended": _on_messages_appended,
    "TaskSnapshot": _on_task_snapshot,
    # Conversation rewind baseline (snapshot-shaped marker).
    "TaskRewound": _on_task_rewound,
    "TaskCompleted": _on_task_completed,
    "TaskFailed": _on_task_failed,
    "TaskSuspended": _on_task_suspended,
    "TaskWoken": _on_task_woken,
    "SubtaskSpawned": _on_subtask_spawned,
    "SubtaskCompleted": _on_subtask_completed,
    "ContextPlanComposed": _on_context_plan_composed,
    "ToolCallStarted": _on_tool_call_started,
    # LLM round envelopes are observational — the request body / response
    # body are not part of any TaskState slice. Recognising them as
    # no-ops keeps RuntimeLLMClient recordings warning-free without
    # changing what fold derives.
    "LLMRequestStarted": _on_noop,
    "LLMRequestFinished": _on_llm_request_finished,
    "LLMResponseRecorded": _on_noop,
    # A live transient-retry marker (rate limit / flaky transport): purely
    # observational — the frontend paints "retrying", fold derives no state.
    # Additive event type: absent from old recordings → byte-equal.
    "LLMRetryScheduled": _on_noop,
    # Slice B: the assistant turn's extended-thinking, keyed by its first
    # tool_use call_id; the Composer re-attaches it on the next compose.
    "AssistantThinkingRecorded": _on_assistant_thinking_recorded,
    "ToolResultRecorded": _on_noop,
    "ToolCallFinished": _on_noop,
    "ToolCallDenied": _on_tool_call_denied,
    "ToolCallApprovalRequested": _on_tool_call_approval_requested,
    "ToolCallApprovalResolved": _on_tool_call_approval_resolved,
    "UserQuestionRequested": _on_user_question_requested,
    "UserQuestionAnswered": _on_user_question_answered,
    "SubtaskDenied": _on_subtask_denied,
    "TaskCancelled": _on_task_cancelled,
    "ModelBound": _on_model_bound,
    # AgentBound now carries only ``agent_name`` (a durable record, already on
    # TaskCreated); with the earlier digest fold gone it derives no
    # state. Registered as a no-op so old + new recordings stay warning-free.
    "AgentBound": _on_noop,
    "TaskHostBound": _on_task_host_bound,
    "McpProvenanceRecorded": _on_mcp_provenance_recorded,
    "ConversationClosed": _on_conversation_closed,
    "ConversationReopened": _on_conversation_reopened,
    "StepTransitionMarked": _on_step_transition_marked,
    # ③ (D-3): CompactionRequested is observational (fold derives no
    # state); Compacted writes the summary slice onto ContextState.
    "CompactionRequested": _on_noop,
    "Compacted": _on_compacted,
    # D3/D8 — per-task first-emission content-hash provenance.
    # Additive event types: absent from old recordings → byte-equal.
    "ToolSchemaRecorded": _on_tool_schema_recorded,
    "SkillContentRecorded": _on_skill_content_recorded,
    # D2 — the generic content-channel provenance event.
    "ContextContentRecorded": _on_context_content_recorded,
    # (issue 05) — background-shell lifecycle, folded into the
    # session's append-only ``background_jobs`` audit. Additive event types:
    # absent from old recordings → byte-equal.
    "BackgroundShellStarted": _on_background_shell_started,
    "BackgroundShellPolled": _on_background_shell_polled,
    "BackgroundShellExited": _on_background_shell_exited,
    "BackgroundShellKilled": _on_background_shell_killed,
    # issue 06 — the orphan-recovery mark: flips a job with no terminal to
    # status="lost" on host restart (never deleted — audit trail).
    "BackgroundShellLost": _on_background_shell_lost,
    # background sub-agents (docs/adr/background-subagent.md) — folded into the
    # session's append-only ``background_subagents`` audit. Additive event types:
    # absent from old recordings → byte-equal.
    "BackgroundSubagentStarted": _on_background_subagent_started,
    "BackgroundSubagentDelivered": _on_background_subagent_delivered,
}
