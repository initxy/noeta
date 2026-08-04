"""The Tool protocol, its ToolResult value object, and the per-call ToolContext.

The EventLog payload is capped at 4 KB, so any tool output larger than that
ceiling MUST go to the ContentStore as an artifact. Tools reach the store only
through ``ToolContext.artifact_store``, which keeps them independent of which
store the host injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from noeta.protocols.values import ContentRef


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A Tool's return value.

    Inline ``output`` is only for small structured results that fit under the
    4-KB EventLog payload ceiling; anything bigger belongs in ``artifacts``.
    ``output_ref`` is a runtime-side companion for truncation markers and audit
    cross-references — the ToolRuntime writes it post-hoc via
    ``object.__setattr__`` once it has offloaded the body, and tools never set
    it themselves.
    """

    success: bool
    output: Any = None
    summary: str = ""
    artifacts: list[ContentRef] = field(default_factory=list)
    #: Images the tool surfaces for a vision model to *see*, each a
    #: ``ContentRef`` to the bytes in the ContentStore. ``wrap_tool_result_block``
    #: moves them into ``ToolResultBlock.images``, and the bound adapter derefs
    #: and inlines them into the provider's tool-result image slot when the model
    #: is vision-capable, degrading to text otherwise.
    images: list[ContentRef] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    #: Assigned by the ToolRuntime after offload, not by the tool.
    output_ref: Optional[ContentRef] = None
    #: A write-side fs tool that actually mutated the workspace surfaces the
    #: touched files here so the ToolRuntime can stash each one's PRE-edit
    #: content as the turn's rewind baseline. Each entry is
    #: ``{"path": <workspace-relative str>, "before": <bytes | None>}``, where
    #: ``before=None`` means the file did not exist, so a rewind deletes it.
    #: Transient: never serialized into an event — the runtime reads it, records
    #: the dedup'd baseline ref onto ``ToolResultRecorded``, and drops it.
    file_changes: Optional[list[dict[str, Any]]] = None


class _ArtifactStore(Protocol):
    """The subset of ContentStore that tools may use.

    Deliberately narrow: a tool gets neither the EventLog nor the Engine, only
    this content seam. A real :class:`noeta.protocols.content_store.ContentStore`
    satisfies it structurally.
    """

    def put(self, body: bytes, *, media_type: str) -> ContentRef: ...

    def get(self, ref: ContentRef) -> bytes: ...


class BackgroundRunner(Protocol):
    """The seam a tool uses to launch, poll and kill a background process.

    Deliberately tiny: a tool gets this spawn/poll/kill surface and not the
    ``ProcessRegistry`` type behind it, which
    :class:`noeta.runtime.background_shell.ProcessRegistry` satisfies
    structurally. ``None`` on :class:`ToolContext` means the host did not enable
    background execution, so the tool refuses cleanly. ``kill`` only *requests*
    a SIGTERM→SIGKILL teardown and returns promptly; the watcher reaps the job
    and records ``BackgroundShellKilled``.
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


class FileReadRegistry(Protocol):
    """Session-scoped record of the file bodies the model has ``Read``.

    ``Edit`` and ``Write``-overwrite consult it for their read-first
    precondition: an entry must exist for the path AND its digest must still
    match the file's current bytes, so the model can neither mutate a file it
    never saw nor one that changed underneath it. Keyed by the canonical
    absolute path; the digest is the sha256 hex of the raw bytes as read.
    In-memory by design — a resumed session starts empty and requires a fresh
    ``Read`` before the next mutation, which matches the reference behavior.
    """

    def record(self, path: str, digest: str) -> None: ...

    def digest(self, path: str) -> Optional[str]: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Per-call context handed to ``Tool.invoke``.

    Tools never see the EventLog or the Engine — only the ``artifact_store``,
    an optional ``background_runner``, the session's ``file_read_registry``,
    and a free-form ``metadata`` bag. ``ToolRuntime.invoke`` threads the
    current ``task_id`` / ``trace_id`` through that bag rather than as fields,
    so a tool can attribute a spawn to its task without the context growing a
    dependency on task identity.
    """

    artifact_store: _ArtifactStore
    metadata: dict[str, Any] = field(default_factory=dict)
    background_runner: Optional["BackgroundRunner"] = None
    #: ``None`` ⇒ no registry wired (a hand-built context in a host/test):
    #: ``Read`` records nothing and the read-first preconditions are skipped.
    #: The ToolRuntime always wires one, so the builtin flow always enforces.
    file_read_registry: Optional["FileReadRegistry"] = None


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
