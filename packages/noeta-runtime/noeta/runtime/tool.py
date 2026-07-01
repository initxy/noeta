"""ToolRuntime wrapper.

The normal recording flow records each call as a three-event envelope.

Recording protocol:

    ToolCallStarted  → ToolResultRecorded(output_ref, summary, artifacts,
                                          side_effects)
                    → ToolCallFinished

Each event is appended via the EventLog under the active ``lease_id``;
the body of the tool's output is offloaded to ContentStore so the
inline ``output_ref`` keeps the payload under the 4-KB ceiling.
"""

from __future__ import annotations

import json
from typing import Any, Optional

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

#: the media type the PRE-edit baseline bytes are offloaded
#: under. Deliberately ``text/plain`` to MATCH what ``read`` offloads file
#: bodies as (``noeta.tools.fs.read._READ_FILE_MEDIA_TYPE``), so a file the AI
#: read before editing dedups to ONE ContentStore blob (the baseline ref and
#: the read-precondition ref are the same content-addressed key).
_BASELINE_MEDIA_TYPE = "text/plain"

#: single-file ceiling for a rewind baseline. A pre-edit blob
#: larger than this (or one detected binary — see ``_capture_file_baselines``)
#: is NOT stashed: rewind cannot cover it (accepted, matching how Claude Code
#: excludes large files). Only the ContentStore blobs grow with checkpoints; ordinary source
#: edits are tiny, so the cap exists solely to fence off a runaway large/binary
#: file. Created files (no pre-edit bytes) are exempt from the check.
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
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._actor = actor
        # the host's ProcessRegistry (structurally a
        # ``noeta.protocols.tool.BackgroundRunner``). ``None`` ⇒ background
        # execution disabled; the ToolContext carries ``None`` and the
        # background tools refuse cleanly. Threaded into every ToolContext.
        self._background_runner = background_runner
        # the host's per-turn file-checkpoint gate (a
        # ``noeta.runtime.file_checkpoint.FileCheckpointRegistry``). ``None`` ⇒
        # rewind file-checkpoint is off (every pre-0043 construction);
        # the tool still surfaces ``file_changes`` but no baseline is stashed and
        # no ``file_baselines`` are recorded — byte-identical to a pre-0043
        # ToolResultRecorded.
        self._file_checkpoint_registry = file_checkpoint_registry
        # memo of editing-task → SESSION-ROOT task id (immutable
        # durable parent graph). Lets the per-turn gate key a whole delegation
        # tree under ONE root (see ``_session_root``). Live-only capture path.
        self._root_cache: dict[str, str] = {}

    # -- normal-mode invoke ----------------------------------------------

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
        result = tool.invoke(call.arguments, ctx)

        # stash this turn's rewind baseline for each file this
        # call edited for the FIRST time this turn. The per-turn gate dedups
        # repeats; the recorded ``file_baselines`` are the AUTHORITATIVE record
        # (fold reads them, the live restore writes them back).
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

        # Populate the post-hoc output_ref on the frozen ToolResult so
        # downstream handlers (e.g. inline truncation markers) can
        # cross-reference the full-audit ContentRef without re-encoding.
        object.__setattr__(result, "output_ref", output_ref)
        return result

    def _capture_file_baselines(
        self, task_id: str, result: ToolResult
    ) -> Optional[list[FileBaseline]]:
        """Turn the tool's ``file_changes`` into recorded
        baselines.

        For each file the call mutated (surfaced on ``result.file_changes`` by
        the write-side fs tools), ask the per-turn gate whether this is the
        FIRST edit of that path this turn; only then stash a baseline. The gate
        is keyed by the SESSION ROOT (``_session_root``) so a whole delegation
        tree shares ONE gate (D8): a parent that edited X then a subtask that
        edits the same X must not stash a SECOND (mid-turn, dirty) baseline. For
        a top-level turn the editing task IS the root, so this is byte-identical
        to issue 02.

        D7 — a pre-edit blob over :data:`_BASELINE_MAX_BYTES`, or one containing
        a NUL byte (binary), is NOT checkpointed: rewind cannot cover it
        (accepted). The path is still MARKED so a later same-turn edit does not
        re-evaluate it — the turn-start state was the oversize/binary one and is
        simply left out. Created files (``before is None``) skip the size/binary
        check (no pre-edit bytes to weigh).

        The ``None`` return (no gate, or nothing stashable this call) keeps the
        recording byte-identical to a pre-0043 one."""
        registry = self._file_checkpoint_registry
        changes = result.file_changes
        if registry is None or not changes:
            return None
        root = self._session_root(task_id)
        baselines: list[FileBaseline] = []
        for change in changes:
            path = change["path"]
            if not registry.mark_if_first(root, path):
                # Already baselined this turn — the first edit pinned the turn's
                # starting state; later edits add nothing (D6 "stash on first touch").
                continue
            before = change.get("before")
            if before is None:
                # AI created the file this turn → baseline is the "did not exist"
                # marker (content_ref=None), so a rewind DELETES it.
                baselines.append(FileBaseline(path=path))
                continue
            before_bytes = bytes(before)
            # D7 — oversize / binary: skip the stash (path already marked above).
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

    def _session_root(self, task_id: str) -> str:
        """The SESSION-ROOT task id of the (possibly subtask)
        editing task, so a whole delegation tree shares ONE per-turn gate.

        Walks ``TaskCreated.parent_task_id`` up to the root (``parent_task_id``
        falsy), memoised because the parent graph is immutable durable data. A
        top-level editing task has no parent and resolves to itself
        (byte-identical to issue 02). Degrades gracefully — if the event_log
        exposes no ``read`` (some test doubles) or a stream lacks
        ``TaskCreated``, it falls back to ``task_id`` itself, which is still the
        correct key for the common single-task case. Live-only."""
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


def _encode_output(output: Any) -> bytes:
    """Canonical bytes for ``output`` so its ContentRef hash is stable.

    ``None`` is encoded as the JSON literal ``null`` (4 bytes), preserving
    a non-empty ContentRef even when the tool offloaded everything into
    ``artifacts`` and left ``output`` unset.
    """
    if isinstance(output, (bytes, bytearray)):
        return bytes(output)
    return json.dumps(output, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
