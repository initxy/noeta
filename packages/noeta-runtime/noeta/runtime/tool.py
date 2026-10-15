"""ToolRuntime — records every tool call as a three-event envelope::

    ToolCallStarted → ToolResultRecorded(output_ref, summary, artifacts,
                                         side_effects) → ToolCallFinished

The envelope is the invariant: whatever the tool does — return, raise, or
blow up a transform stage — all three events are appended under the active
``lease_id``, so an assistant ``tool_use`` is never left without its paired
``tool_result``. Output bodies go to the ContentStore, keeping the inline
``output_ref`` under the event payload ceiling.

A run of concurrency-safe calls (read-only tools that declare
``concurrency_safe = True``) may execute together: each call is started (via
``start``) in order, then ``complete`` runs the tool bodies on up to
:data:`MAX_TOOL_USE_CONCURRENCY` threads and records each result in call
order, so the stream is the same whatever order the bodies finished in.
"""

from __future__ import annotations

import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import ToolCall
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import (
    FileBaseline,
    ToolCallFinishedPayload,
    ToolResultRecordedPayload,
)
from noeta.protocols.tool import (
    FileReadRegistry,
    Tool,
    ToolContext,
    ToolResult,
)
from noeta.protocols.tool_args import build_tool_call_started_payload


class InMemoryFileReadRegistry:
    """Default in-memory :class:`FileReadRegistry`, one per ToolRuntime.

    The ToolRuntime is session-scoped on live hosts, so "read this session"
    reduces to "recorded on this instance". Locked because background
    completions can drive turns on a thread other than the interactive one.
    """

    def __init__(self) -> None:
        self._digests: dict[str, str] = {}
        self._lock = threading.Lock()

    def record(self, path: str, digest: str) -> None:
        with self._lock:
            self._digests[path] = digest

    def digest(self, path: str) -> Optional[str]:
        with self._lock:
            return self._digests.get(path)

    def clear(self) -> None:
        with self._lock:
            self._digests.clear()


#: The side-effect type a read-first recording rides on ``ToolResultRecorded``
#: (``{"type": "file_read", "path": <canonical>, "digest": <sha256 hex>}``).
FILE_READ_EFFECT = "file_read"


def live_side_effects(events: Iterable[Any]) -> list[dict[str, Any]]:
    """The side effects a task's LIVE history recorded, oldest first.

    Live = not folded over: a ``StepAttemptAbandoned`` seal kills
    ``[abandoned_from_seq, seal)`` and a ``TaskRewound`` kills
    ``(target_seq, rewind)``, exactly the stretches fold rebuilds past, so a
    result the model no longer has in its conversation vouches for nothing."""
    envelopes = list(events)
    dead: list[tuple[int, int]] = []
    for env in envelopes:
        payload = env.payload
        if env.type == "StepAttemptAbandoned":
            dead.append((int(payload.abandoned_from_seq), int(env.seq)))
        elif env.type == "TaskRewound":
            dead.append((int(payload.target_seq) + 1, int(env.seq)))
    effects: list[dict[str, Any]] = []
    for env in envelopes:
        if env.type != "ToolResultRecorded":
            continue
        if any(lo <= env.seq < hi for lo, hi in dead):
            continue
        for effect in env.payload.side_effects or ():
            if isinstance(effect, Mapping) and isinstance(effect.get("type"), str):
                effects.append(dict(effect))
    return effects


class RecordedToolState:
    """What a task's tools carry from call to call, rebuilt from its record.

    Two things live here: the read-first record (``reads``) and the newest
    side effect of each type (what :attr:`ToolContext.recorded` answers).
    Both are a cache over the task's live history — the ``ToolRuntime``
    seeds it from the event log the first time it serves the task in this
    process, notes every result it records afterwards, and :meth:`reset`
    sends it back to the log (a crash-recovery seal folded part of the
    history away). One per task, kept by the host's task-local registry."""

    def __init__(self, reads: Optional[FileReadRegistry] = None) -> None:
        self.reads: FileReadRegistry = (
            reads if reads is not None else InMemoryFileReadRegistry()
        )
        self._latest: dict[str, dict[str, Any]] = {}
        self._seeded = False
        self._lock = threading.Lock()

    @property
    def seeded(self) -> bool:
        with self._lock:
            return self._seeded

    def seed(self, events: Iterable[Any]) -> None:
        """Rebuild from ``events`` (the task's stream) unless already seeded."""
        with self._lock:
            if self._seeded:
                return
            self._seeded = True
        self.note(live_side_effects(events))

    def note(self, effects: Iterable[Mapping[str, Any]]) -> None:
        """Take in side effects just recorded (or replayed), oldest first."""
        for effect in effects:
            kind = effect.get("type")
            if not isinstance(kind, str):
                continue
            with self._lock:
                self._latest[kind] = dict(effect)
            if kind == FILE_READ_EFFECT:
                path, digest = effect.get("path"), effect.get("digest")
                if isinstance(path, str) and isinstance(digest, str):
                    self.reads.record(path, digest)

    def latest(self, kind: str) -> Optional[Mapping[str, Any]]:
        with self._lock:
            found = self._latest.get(kind)
            return dict(found) if found is not None else None

    def reset(self) -> None:
        """Forget everything; the next serve re-seeds from the log."""
        with self._lock:
            self._latest.clear()
            self._seeded = False
        clear = getattr(self.reads, "clear", None)
        if callable(clear):
            clear()


class _CapturingReads:
    """The call's view of the read-first record: forwards everything and
    keeps what the call recorded, for the result's side effects."""

    def __init__(self, inner: FileReadRegistry) -> None:
        self._inner = inner
        self.recorded: list[dict[str, Any]] = []

    def record(self, path: str, digest: str) -> None:
        self._inner.record(path, digest)
        self.recorded.append(
            {"type": FILE_READ_EFFECT, "path": path, "digest": digest}
        )

    def digest(self, path: str) -> Optional[str]:
        return self._inner.digest(path)


#: How many concurrency-safe tool calls run at once — Claude Code's
#: ``CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`` default.
MAX_TOOL_USE_CONCURRENCY = 10


@dataclass(frozen=True, slots=True)
class StartedCall:
    """A call whose ``ToolCallStarted`` is recorded and whose body has not
    run yet — what :meth:`ToolRuntime.start` hands to
    :meth:`ToolRuntime.complete`."""

    tool: Tool
    call: ToolCall
    task_id: str
    lease_id: str
    trace_id: str


_OUTPUT_MEDIA_TYPE = "application/json"

#: Media type the PRE-edit baseline bytes are offloaded under. Deliberately
#: matches what ``read`` offloads file bodies as, so a file the model read
#: before editing dedups to ONE ContentStore blob (the baseline ref and the
#: read-precondition ref are then the same content-addressed key).
_BASELINE_MEDIA_TYPE = "text/plain"

#: Single-file ceiling for a rewind baseline. A larger (or binary) pre-edit
#: blob is not stashed, so rewind cannot cover it — accepted, because
#: ordinary source edits are tiny and the cap exists solely to fence off a
#: runaway large/binary file. Created files have no pre-edit bytes to weigh.
_BASELINE_MAX_BYTES = 1_048_576  # 1 MiB


class ToolRuntime:
    """Records a tool invocation as the three-event envelope."""

    def __init__(
        self,
        *,
        event_log: EventLogWriter,
        content_store: ContentStore,
        actor: str = "tool_runtime",
        background_runner: Optional[Any] = None,
        file_checkpoint_registry: Optional[Any] = None,
        tool_result_transforms: Sequence[Callable[[ToolResult], ToolResult]] = (),
        file_read_registry: Optional[FileReadRegistry] = None,
        recorded_state: Optional[RecordedToolState] = None,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._actor = actor
        # Applied INSIDE this boundary, before recording, so the transformed
        # result is the one that reaches the ledger + ContentStore — that is
        # what lets a redaction stage keep a secret out of both. Ordering is
        # the loader's job; this end just runs them in sequence.
        self._tool_result_transforms: tuple[
            Callable[[ToolResult], ToolResult], ...
        ] = tuple(tool_result_transforms)
        # ``None`` ⇒ background execution disabled; the ToolContext carries
        # ``None`` and the background tools refuse cleanly.
        self._background_runner = background_runner
        # The host's per-turn file-checkpoint gate. ``None`` ⇒ rewind
        # file-checkpoint is off: the tool still surfaces ``file_changes``,
        # but no baseline is stashed and no ``file_baselines`` are recorded.
        self._file_checkpoint_registry = file_checkpoint_registry
        # Memo of editing-task → root-task id, safe to cache because the
        # parent graph is immutable durable data.
        self._root_cache: dict[str, str] = {}
        # The session's read-first record. Always present on the builtin path:
        # a host that does not inject one gets a per-runtime default, which is
        # exactly the session scope the precondition wants.
        self._file_read_registry: FileReadRegistry = (
            file_read_registry
            if file_read_registry is not None
            else InMemoryFileReadRegistry()
        )
        # The task's carried tool state; a host keeps one per task across
        # turns (sharing ``file_read_registry`` as its ``reads``), so the
        # history scan runs once per process. Without one, this runtime
        # keeps its own and scans on its first call.
        self._recorded: RecordedToolState = (
            recorded_state
            if recorded_state is not None
            else RecordedToolState(self._file_read_registry)
        )

    def invoke(
        self,
        tool: Tool,
        call: ToolCall,
        *,
        task_id: str,
        lease_id: str,
        trace_id: str,
    ) -> ToolResult:
        started = self.start(
            tool, call, task_id=task_id, lease_id=lease_id, trace_id=trace_id
        )
        return self.complete([started])[0]

    def start(
        self,
        tool: Tool,
        call: ToolCall,
        *,
        task_id: str,
        lease_id: str,
        trace_id: str,
    ) -> StartedCall:
        """Record ``ToolCallStarted``; the body runs in :meth:`complete`."""
        self._event_log.emit(
            task_id=task_id,
            type="ToolCallStarted",
            payload=build_tool_call_started_payload(call, self._content_store),
            lease_id=lease_id,
            trace_id=trace_id,
            actor=self._actor,
            origin="tool",
        )
        return StartedCall(
            tool=tool,
            call=call,
            task_id=task_id,
            lease_id=lease_id,
            trace_id=trace_id,
        )

    def complete(self, started: Sequence[StartedCall]) -> list[ToolResult]:
        """Run the started calls' bodies and record each result, in order.

        More than one call runs concurrently on up to
        :data:`MAX_TOOL_USE_CONCURRENCY` threads; the caller passes several only
        for concurrency-safe tools. Recording waits for every body and walks
        the calls in the order given, so the recorded stream does not depend
        on which body finished first.
        """
        raws: list[tuple[ToolResult, list[dict[str, Any]]]]
        if len(started) <= 1:
            raws = [self._run(s) for s in started]
        else:
            with ThreadPoolExecutor(
                max_workers=min(MAX_TOOL_USE_CONCURRENCY, len(started)),
                thread_name_prefix="noeta-tool",
            ) as pool:
                futures = [
                    pool.submit(contextvars.copy_context().run, self._run, s)
                    for s in started
                ]
                raws = [f.result() for f in futures]
        return [
            self._record(s, result, reads)
            for s, (result, reads) in zip(started, raws)
        ]

    def _run(
        self, started: StartedCall
    ) -> tuple[ToolResult, list[dict[str, Any]]]:
        """The call's raw result and the read-first recordings it made."""
        call = started.call
        self._seed_recorded(started.task_id)
        reads = _CapturingReads(self._file_read_registry)
        ctx = ToolContext(
            artifact_store=self._content_store,
            metadata={
                "task_id": started.task_id,
                "root_task_id": self._root_task_id(started.task_id),
                "trace_id": started.trace_id,
            },
            background_runner=self._background_runner,
            file_read_registry=reads,
            recorded=self._recorded.latest,
        )
        try:
            return started.tool.invoke(call.arguments, ctx), reads.recorded
        except Exception as exc:  # noqa: BLE001 — see below
            # A tool that RAISES must not strand the conversation: the
            # assistant ``tool_use`` and this call's ``ToolCallStarted`` are
            # already committed, so bailing here would leave the ``tool_use``
            # with no matching ``tool_result``, and the next compose→decide
            # would take a fatal provider 400 that a resume replays forever.
            # Synthesise a failed result instead, so the trio still records.
            return (
                ToolResult(
                    success=False,
                    summary=f"tool {call.tool_name!r} raised — {_fault_message(exc)}",
                ),
                reads.recorded,
            )

    def _record(
        self,
        started: StartedCall,
        result: ToolResult,
        reads: list[dict[str, Any]],
    ) -> ToolResult:
        call = started.call
        task_id = started.task_id
        lease_id = started.lease_id
        trace_id = started.trace_id
        result = self._apply_transforms(result, call)

        # The recorded ``file_baselines`` are the AUTHORITATIVE rewind record:
        # fold reads them back and the live restore writes them out.
        file_baselines = self._capture_file_baselines(task_id, result)

        # The read-first recordings ride the result, so a later process
        # rebuilds the record from the log (``RecordedToolState.seed``).
        side_effects = list(result.side_effects) + reads
        output_body = _encode_output(result.output)
        output_ref = self._content_store.put(
            output_body, media_type=_OUTPUT_MEDIA_TYPE
        )
        self._event_log.emit(
            task_id=task_id,
            type="ToolResultRecorded",
            payload=ToolResultRecordedPayload(
                call_id=call.call_id,
                success=result.success,
                output_ref=output_ref,
                summary=result.summary,
                artifacts=list(result.artifacts),
                side_effects=side_effects,
                file_baselines=file_baselines,
            ),
            lease_id=lease_id,
            trace_id=trace_id,
            actor=self._actor,
            origin="tool",
        )

        self._event_log.emit(
            task_id=task_id,
            type="ToolCallFinished",
            payload=ToolCallFinishedPayload(call_id=call.call_id),
            lease_id=lease_id,
            trace_id=trace_id,
            actor=self._actor,
            origin="tool",
        )

        self._recorded.note(side_effects)

        # Populate the output_ref on the frozen ToolResult so downstream
        # handlers (e.g. inline truncation markers) can cross-reference the
        # full-audit ContentRef without re-encoding the payload.
        object.__setattr__(result, "output_ref", output_ref)
        return result

    def _seed_recorded(self, task_id: str) -> None:
        """Seed the carried tool state from the task's stream, once.

        An event log without ``read`` (a write-only double) seeds nothing."""
        if self._recorded.seeded:
            return
        read = getattr(self._event_log, "read", None)
        self._recorded.seed(read(task_id) if callable(read) else ())

    def _apply_transforms(self, result: ToolResult, call: ToolCall) -> ToolResult:
        """Run the ``tool_result_transform`` chain, containing any stage fault.

        A stage that raises or returns a non-``ToolResult`` collapses the whole
        call to a failed :class:`ToolResult` naming the offending stage. Two
        properties are load-bearing:

        * the fault never propagates out of :meth:`invoke`, so the three-event
          envelope still records and the ``tool_use`` keeps its ``tool_result``;
        * the substituted result carries **no** part of the original output, so a
          redaction stage that blew up cannot leak the payload it was meant to
          scrub into the EventLog or the ContentStore.
        """
        for transform in self._tool_result_transforms:
            stage = getattr(transform, "__name__", None) or type(transform).__name__
            try:
                transformed = transform(result)
            except Exception as exc:  # noqa: BLE001 — see the docstring
                return ToolResult(
                    success=False,
                    summary=(
                        f"tool {call.tool_name!r} result transform {stage!r} "
                        f"raised — {_fault_message(exc)}; the untransformed "
                        f"result was discarded"
                    ),
                )
            if not isinstance(transformed, ToolResult):
                return ToolResult(
                    success=False,
                    summary=(
                        f"tool {call.tool_name!r} result transform {stage!r} "
                        f"returned {type(transformed).__name__}, expected "
                        f"ToolResult; the untransformed result was discarded"
                    ),
                )
            result = transformed
        return result

    def _capture_file_baselines(
        self, task_id: str, result: ToolResult
    ) -> Optional[list[FileBaseline]]:
        """Turn the tool's ``file_changes`` into recorded baselines.

        Only the FIRST edit of a path within a turn is stashed — that is the
        state a rewind must restore. The per-turn gate is keyed by the
        ROOT-task, so a whole delegation tree shares ONE gate: a parent that
        edited X followed by a subtask editing the same X must not stash a
        second, already-dirty baseline.

        An oversize or binary pre-edit blob is not stashed (rewind cannot
        cover it) but its path IS still marked, so a later same-turn edit does
        not re-evaluate it and stash a mid-turn state instead."""
        registry = self._file_checkpoint_registry
        changes = result.file_changes
        if registry is None or not changes:
            return None
        root = self._root_task_id(task_id)
        baselines: list[FileBaseline] = []
        for change in changes:
            path = change["path"]
            if not registry.mark_if_first(root, path):
                # The first edit already pinned the turn's starting state;
                # later edits add nothing.
                continue
            before = change.get("before")
            if before is None:
                # Created this turn → the baseline is a "did not exist" marker
                # (content_ref=None), so a rewind DELETES the file.
                baselines.append(FileBaseline(path=path))
                continue
            before_bytes = bytes(before)
            # Oversize / binary: skip the stash; the path is marked above.
            if (
                len(before_bytes) > _BASELINE_MAX_BYTES
                or b"\x00" in before_bytes
            ):
                continue
            ref = self._content_store.put(
                before_bytes, media_type=_BASELINE_MEDIA_TYPE
            )
            baselines.append(FileBaseline(path=path, content_ref=ref))
        return baselines or None

    def _root_task_id(self, task_id: str) -> str:
        """The root-task id of the editing task, so a whole delegation tree
        shares ONE per-turn gate.

        Walks ``TaskCreated.parent_task_id`` up to the root, memoised because
        the parent graph is immutable durable data. Degrades to ``task_id``
        itself when the event_log exposes no ``read`` or the stream lacks a
        ``TaskCreated`` — still the correct key for a single-task turn."""
        cached = self._root_cache.get(task_id)
        if cached is not None:
            return cached
        read = getattr(self._event_log, "read", None)
        current = task_id
        if callable(read):
            seen: set[str] = set()
            while current not in seen:
                seen.add(current)
                parent: Optional[str] = None
                for env in read(current):
                    if env.type == "TaskCreated":
                        parent = getattr(env.payload, "parent_task_id", None)
                        break
                if not parent:
                    break
                current = str(parent)
        self._root_cache[task_id] = current
        return current


def _fault_message(exc: BaseException) -> str:
    """``Type: message``, capped so one exploding exception cannot flood a summary."""
    message = f"{type(exc).__name__}: {exc}"
    return message if len(message) <= 2000 else message[:2000] + "…"


def _encode_output(output: Any) -> bytes:
    """Canonical bytes for ``output`` so its ContentRef hash is stable.

    ``None`` encodes as the JSON literal ``null``, which keeps a non-empty
    ContentRef even when a tool offloaded everything into ``artifacts`` and
    left ``output`` unset.
    """
    if isinstance(output, (bytes, bytearray)):
        return bytes(output)
    return json.dumps(output, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
