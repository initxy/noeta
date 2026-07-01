"""Tool protocol, ToolResult value object, and ToolContext.

The EventLog payload is capped at 4 KB; tool outputs larger
than that ceiling MUST go into the ContentStore as artifacts. Tools talk
to the store through ``ToolContext.artifact_store`` so the runtime can
inject any compatible store (in-memory in Phase 0; filesystem / S3 in
later Phases).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from noeta.protocols.values import ContentRef


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A Tool's return value.

    Big ``output`` belongs in ``artifacts`` (ContentStore-backed); the
    inline ``output`` field is reserved for small structured results
    (e.g. numeric answers, short strings, status dicts) that fit under
    the 4-KB EventLog payload ceiling.

    ``output_ref`` is populated **post-hoc** by :class:`ToolRuntime`
    after the body has been offloaded to the
    ContentStore (the runtime uses ``object.__setattr__`` because this
    dataclass is frozen). Tools never set it; it is a runtime-side
    companion used for truncation markers and audit cross-references.
    """

    success: bool
    output: Any = None
    summary: str = ""
    artifacts: list[ContentRef] = field(default_factory=list)
    #: Images the tool surfaces for a vision model to *see* (the ``read`` tool
    #: reading an image file). Each ``ContentRef`` points at the image bytes in
    #: the ContentStore (put via ``ToolContext.artifact_store``). The projection
    #: (``wrap_tool_result_block``) wraps these into ``ToolResultBlock.images``;
    #: the bound adapter deref→inlines them into the provider's tool-result
    #: image slot when the model is vision-capable, else degrades to text. Empty
    #: for every non-image tool ⇒ behavior is byte-identical to a pre-image
    #: ToolResult.
    images: list[ContentRef] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    #: Populated by the ToolRuntime after offload (frozen-slot, runtime-assigned).
    output_ref: Optional[ContentRef] = None
    #: File-checkpoint capture. A write-side fs tool (``edit`` /
    #: ``write`` / ``apply_patch``) that actually mutated the workspace surfaces
    #: the touched files here so the ToolRuntime can stash each one's PRE-edit
    #: content as the turn's rewind baseline. Each entry is
    #: ``{"path": <workspace-relative str>, "before": <bytes | None>}`` —
    #: ``before=None`` means the file did NOT exist before (AI created it, so a
    #: rewind deletes it). ``None`` (every non-mutating / non-fs tool, and every
    #: dry-run) ⇒ no capture, byte-identical to a pre-0043 ToolResult. NOT
    #: serialized into any event — the runtime reads it, records the dedup'd
    #: baseline ref onto ``ToolResultRecorded``, and drops it (transient).
    file_changes: Optional[list[dict[str, Any]]] = None


class _ArtifactStore(Protocol):
    """Subset of ContentStore that tools may use.

    ``put`` offloads large outputs; ``get`` reads an artifact back so a
    tool can verify against earlier content (e.g. a citation tool checking
    that a quote came from a fetched page). Deliberately narrow — tools
    get neither the EventLog nor the Engine, only this content seam. A
    real :class:`noeta.protocols.content_store.ContentStore` satisfies it
    structurally.
    """

    def put(self, body: bytes, *, media_type: str) -> ContentRef: ...

    def get(self, ref: ContentRef) -> bytes: ...


class BackgroundRunner(Protocol):
    """Narrow seam a tool uses to launch / poll a background process.

    Deliberately tiny — a tool gets neither the EventLog nor the
    ``ProcessRegistry`` type, only this spawn/poll/kill surface. The host's
    :class:`noeta.runtime.background_shell.ProcessRegistry` satisfies it
    structurally; ``None`` on :class:`ToolContext` means the host did not
    enable background execution and the tool refuses cleanly. ``kill`` (issue
    03) requests a SIGTERM→SIGKILL teardown of one job and returns promptly
    (the watcher reaps + records ``BackgroundShellKilled``).
    """

    def spawn(
        self,
        *,
        argv: list[str],
        cwd: Any,
        env: dict[str, str],
        command: str,
        spawned_by_task_id: str,
        trace_id: str,
    ) -> dict[str, Any]: ...

    def poll(self, job_id: str) -> dict[str, Any]: ...

    def kill(self, job_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Per-call context handed to ``Tool.invoke``.

    Tools never see the EventLog or the Engine directly. They only get
    the ``artifact_store`` so large outputs can be offloaded (and earlier
    artifacts read back), plus a free-form ``metadata`` bag for future
    fields (trace_id, principal, etc.) that later Phases will start
    populating.

    ``background_runner`` is the optional host-supplied seam a
    tool uses to launch / poll a background process; ``None`` (the default,
    every earlier construction) means background execution is not
    available — keeping all existing ``ToolContext`` constructions
    byte-identical. The current ``task_id`` / ``trace_id`` are threaded in via
    the ``metadata`` bag (so the tool can attribute a spawn to its task) —
    ``ToolRuntime.invoke`` populates ``metadata={"task_id": ..., "trace_id":
    ...}`` when it builds the context; a tool reads them with ``ctx.metadata.get``.
    """

    artifact_store: _ArtifactStore
    metadata: dict[str, Any] = field(default_factory=dict)
    background_runner: Optional["BackgroundRunner"] = None


class Tool(Protocol):
    """Structural shape of a runnable Tool.

    Tools are pure callables from arguments to a ``ToolResult``. The
    metadata fields (``name`` / ``description`` / ``risk_level`` /
    ``input_schema``) are used by Governance hooks, by the ToolRuntime when
    recording events, and by the LLM adapter when advertising the tool to
    the model; they live on the type rather than on each call so the same
    Tool instance always carries the same metadata.

    ``input_schema`` is a JSON-Schema-shaped dict, LLM-facing metadata
    only — Noeta does **not** validate ``arguments`` against it at
    runtime. A lax schema such as
    ``{"type": "object", "additionalProperties": True}`` is legal.

    ``description`` is the hand-written, LLM-facing one-or-two-sentence
    statement of what the tool does — the **single source of truth** for
    tool semantics the model reads. It is rendered into the
    provider tool schema by the ContextComposer, never restated in the
    system prompt. Authored by hand, not derived from the docstring
    (docstrings carry developer-facing internal references).
    """

    name: str
    description: str
    risk_level: str
    input_schema: dict[str, Any]

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult: ...
