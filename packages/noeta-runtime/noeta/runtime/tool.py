"""ToolRuntime — records every tool call as a three-event envelope::

    ToolCallStarted → ToolResultRecorded(output_ref, summary, artifacts,
                                         side_effects) → ToolCallFinished

The envelope is the invariant: whatever the tool does — return, raise, or
blow up a transform stage — all three events are appended under the active
``lease_id``, so an assistant ``tool_use`` is never left without its paired
``tool_result``. Output bodies go to the ContentStore, keeping the inline
``output_ref`` under the event payload ceiling.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional, Sequence

from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import ToolCall
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import (
    FileBaseline,
    ToolCallFinishedPayload,
    ToolResultRecordedPayload,
)
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.protocols.tool_args import build_tool_call_started_payload


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

    def invoke(
        self,
        tool: Tool,
        call: ToolCall,
        *,
        task_id: str,
        lease_id: str,
        trace_id: str,
    ) -> ToolResult:
        self._event_log.emit(
            task_id=task_id,
            type="ToolCallStarted",
            payload=build_tool_call_started_payload(call, self._content_store),
            lease_id=lease_id,
            trace_id=trace_id,
            actor=self._actor,
            origin="tool",
        )

        ctx = ToolContext(
            artifact_store=self._content_store,
            metadata={"task_id": task_id, "trace_id": trace_id},
            background_runner=self._background_runner,
        )
        try:
            result = tool.invoke(call.arguments, ctx)
        except Exception as exc:  # noqa: BLE001 — see below
            # A tool that RAISES must not strand the conversation: the
            # assistant ``tool_use`` and this call's ``ToolCallStarted`` are
            # already committed, so bailing here would leave the ``tool_use``
            # with no matching ``tool_result``, and the next compose→decide
            # would take a fatal provider 400 that a resume replays forever.
            # Synthesise a failed result instead, so the trio still records.
            result = ToolResult(
                success=False,
                summary=f"tool {call.tool_name!r} raised — {_fault_message(exc)}",
            )

        result = self._apply_transforms(result, call)

        # The recorded ``file_baselines`` are the AUTHORITATIVE rewind record:
        # fold reads them back and the live restore writes them out.
        file_baselines = self._capture_file_baselines(task_id, result)

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
                side_effects=list(result.side_effects),
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

        # Populate the output_ref on the frozen ToolResult so downstream
        # handlers (e.g. inline truncation markers) can cross-reference the
        # full-audit ContentRef without re-encoding the payload.
        object.__setattr__(result, "output_ref", output_ref)
        return result

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
