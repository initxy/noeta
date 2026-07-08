"""``InteractionDriver`` — the single conversation-command seam (D1/D3).

Issue 05. The product has two surfaces — a Claude-Code-class CLI and a
Claude-class web UI — that **both** expose the same capability over the
**same** runtime: open a conversation, drive turns, approve gated tools,
cancel. D1/D3 require **one** task-creation + drive code path that
both surfaces route through; the only per-surface difference is the
transport adapter's input validation (the web validates a model *selector*
and refuses raw provider config; the CLI on the user's own machine has no
trust boundary).

This module is that shared seam. It is **not** a second runtime: every
command walks the real dispatcher contract and hands the lease to the L2
canonical primitive :func:`noeta.runtime.worker.run_leased_task`, driving
each leased Task with **its own Agent's Engine** via the issue-01
:class:`noeta.execution.host.ResidentHost` Protocol. The per-command variation
is the typed woken-command-prelude (``AppendMessagePrelude`` /
``ResolveApprovalPrelude``) from issue 01 — never re-inlined here.

The driver is **Protocol-typed**: it depends
on the :class:`ResidentHost` structural seam and the
:class:`AgentRegistryProtocol` identity lookup — not the concrete
``CodeEngineResolver`` or ``CODE_AGENT_SPECS`` dict. Alternative host
implementations (SDK-side agent hosts, resume fakes, multi-product hosts)
slot in unchanged.

Five commands:

* :meth:`InteractionDriver.start` — create a Task from **server-side**
  config (only ``goal`` / ``agent`` / an optional model *selector* come
  from the caller), seed the goal, and drive the first turn. The driver
  never reads provider / endpoint / base_url / credentials / profile /
  tool-registry from the caller (R4).
* :meth:`InteractionDriver.send_goal` — the per-turn append-message
  command (issue 01 prelude = append-message).
* :meth:`InteractionDriver.approve` / :meth:`InteractionDriver.deny` —
  resolve a pending gated tool call (issue 01 prelude = resolve-approval).
* :meth:`InteractionDriver.cancel` — write the L0 ``TaskCancelled`` control
  event (no new schema; not a policy ``Decision``).

Interactive turns run ``final=False`` ("Interactive sessions
terminate on a trailing suspend"): a normally-finishing turn suspends on
the next-goal handle instead of completing, so the conversation's durable
end-state is a trailing suspend. The resolver wraps each agent's policy in
``MultiTurnReActPolicy(final=False)``; ``fail`` / ``approval`` / ``subtask``
keep their native semantics (a failed turn still terminates).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

from noeta.execution.multi_turn import (
    NEXT_GOAL_WAKE_HANDLE,
    MultiTurnReActPolicy,
)
from noeta.execution.environment import record_environment
from noeta.execution.host import ResidentHost
from noeta.execution.instructions import record_instructions
from noeta.execution.memory import (
    RecallGoalPrelude,
    append_user_message_with_recall,
    record_memory_index,
)
from noeta.execution.resolver import agent_name_of
from noeta.execution.subtask_drain import UnsupportedSubtaskSuspend
from noeta.providers.catalog import resolve_alias
from noeta.core.engine import suspend_on_human_handle
from noeta.core.fold import BoundedEventLog, fold
from noeta.core.snapshot import serialize_task_state, snapshot_media_type
from noeta.protocols.errors import CodedError, InvalidLease
from noeta.protocols.events import (
    BackgroundSubagentDeliveredPayload,
    EventEnvelope,
    TaskCancelledPayload,
    TaskFailedPayload,
    TaskHostBoundPayload,
    TaskRewoundPayload,
)
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.messages import ImageBlock, MessageOrigin, TextBlock
from noeta.protocols.policy import Policy
from noeta.protocols.task import Task
from noeta.policies.control_tools import (
    load_questions_body,
    normalize_answer_document,
    question_handle,
)
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.values import LOCAL_PRINCIPAL, ContentRef, Principal
from noeta.protocols.wake import (
    ExternalEvent,
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskGroupCompleted,
    WakeCondition,
)
from noeta.runtime.worker import (
    AppendMessagePrelude,
    AnswerUserQuestionPrelude,
    ResolveApprovalPrelude,
    WokenPrelude,
    keep_lease_alive,
    run_leased_task,
)

_log = logging.getLogger(__name__)

__all__ = [
    "InteractionDriver",
    "ModelBindPrelude",
    "ModelSelectorError",
    "NotResumableError",
    "ProviderSelectorError",
    "SeededTurn",
    "STUB_MODEL_ALLOWLIST",
    "TaskAlreadyTerminalError",
    "multi_turn_policy_wrapper",
]


#: Deployment model-selector allowlist (D2). The driver validates
#: ``selector ∈ principal.allowed_models ∩ this`` before binding — the
#: *deployment* half of "who may use which model" (the principal half is
#: :attr:`noeta.protocols.values.Principal.allowed_models`). Mirrors Claude
#: Code's ``/model`` names. Kept under the historical name so issue-05
#: callers/tests keep importing it.
STUB_MODEL_ALLOWLIST: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})


class ModelSelectorError(CodedError):
    """The model *selector* is not in ``principal.allowed_models ∩ allowlist``.

    Issue 06 (D2 / D3): the selector is rejected when the
    authenticated Principal is not sanctioned to bind it, or it is outside
    the deployment allowlist. On rejection the driver emits **no**
    ``ModelBound``, runs **no** turn, and leaves **no** binding — the refusal
    happens before any durable write. ``allowed`` is the intersection the
    selector failed (what the caller *could* have picked).
    """

    code = "model_selector_rejected"

    def __init__(self, *, selector: str, allowed: list[str]) -> None:
        self.selector = selector
        self.allowed = allowed
        super().__init__(
            f"model selector {selector!r} not permitted; allowed: {allowed}"
        )


class ProviderSelectorError(CodedError):
    """The ``(provider, model)`` selector is not a legal pair (I4).

    Rejected when the provider name is **not configured** on the host's
    provider registry, or the chosen ``model`` is **not in that provider's
    declared model list**. Like :class:`ModelSelectorError`, the refusal
    happens *before* any durable write — no ``ModelBound``, no Task, no turn,
    no binding left behind — so a rejected pair leaves the conversation
    untouched. ``available`` is the configured provider names (when the
    provider itself was unknown) or that provider's model list (when the model
    was the offending half), echoing what the caller *could* have picked.
    """

    code = "provider_selector_rejected"

    def __init__(
        self, *, provider: str, model: str, available: list[str]
    ) -> None:
        self.provider = provider
        self.model = model
        self.available = available
        super().__init__(
            f"(provider={provider!r}, model={model!r}) is not a legal pair; "
            f"available: {available}"
        )


class NotResumableError(CodedError, RuntimeError):
    """A command tried to resume a task that is not on the expected wake.

    Also a :class:`RuntimeError` so any historical ``except RuntimeError``
    contract keeps matching; the new :class:`CodedError` base is what the
    product backend switches on (``code``).
    """

    code = "not_resumable"

    def __init__(
        self,
        *,
        task_id: str,
        handle: str,
        status: str,
        wake_on: Any,
        dispatcher_status: Optional[str] = None,
        expected: Optional[str] = None,
    ) -> None:
        self.task_id = task_id
        self.handle = handle
        self.status = status
        self.wake_on = wake_on
        self.dispatcher_status = dispatcher_status
        # ``expected`` names the wake the command required when it is NOT the
        # human-handle default (e.g. ``deliver_event`` expecting an
        # ``ExternalEvent``); ``None`` keeps the historical message.
        if expected is None:
            expected = f"HumanResponseReceived(handle={handle!r})"
        message = f"task {task_id!r} is {status!r}, not waiting for {expected}"
        if dispatcher_status is not None:
            message += f"; dispatcher status is {dispatcher_status!r}"
        super().__init__(message)


class TaskAlreadyTerminalError(CodedError, RuntimeError):
    """A lifecycle verb (``cancel`` / ``close`` / ``reopen``) targeted a task
    that is already terminal — a terminal conversation is not cancellable,
    closable, or reopenable.

    Replaces the bare ``RuntimeError(f"...: already terminal")`` these verbs
    used to raise: the product backend matched it by the ``"already terminal"``
    message substring, which this ``code`` makes structural. Kept a
    :class:`RuntimeError` too so any ``except RuntimeError`` contract is
    unaffected. Carries the offending ``task_id`` and the ``verb``.
    """

    code = "task_already_terminal"

    def __init__(self, *, task_id: str, verb: str) -> None:
        self.task_id = task_id
        self.verb = verb
        super().__init__(f"cannot {verb} task {task_id!r}: already terminal")


@dataclass(frozen=True, slots=True)
class ModelBindPrelude:
    """A woken-command prelude that binds a model, then chains another step.

    Issue 06: a per-turn model switch must record its ``ModelBound`` in the
    same first-consume window (after ``TaskWoken``, before ``run_one_step``)
    as the turn's own prelude — so both land before the next
    ``ContextPlanComposed`` and a resumed fold reconstructs the same binding
    before re-composing. This composes ``note_model_bound`` with the inner
    prelude (normally ``AppendMessagePrelude`` for ``send_goal``):
    ``note_woken → ModelBound → MessagesAppended → run_one_step``.

    The Engine ``run_leased_task`` resolves for a woken task is keyed on the
    Task's *folded* model binding; ``note_model_bound`` runs ON that resolved
    Engine (the still-current-model one) and only writes the event — the new
    binding takes effect on the **next** resolve (Claude Code ``/model``
    semantics: the switch drives the next turn). ``note_model_bound`` simply
    appends the ``ModelBound`` to the same EventLog regardless of which
    Engine instance emits it.
    """

    model: str
    principal_identity: str
    inner: Optional[WokenPrelude] = None
    #: (I4) — the per-turn provider name folded into THIS same
    #: ``ModelBound`` (not a separate ProviderBound event). ``None`` ⇒ this turn
    #: switched only the model: fold leaves the current provider binding intact.
    provider: Optional[str] = None

    #: ``ModelBound`` + the chained append prelude are pure event appends —
    #: seed-time safe (D6). Only ever constructed over append-type inners.
    durable_at_seed: ClassVar[bool] = True

    def __call__(self, engine: Any, task: Any, *, lease_id: str) -> Any:
        task = engine.note_model_bound(
            task,
            lease_id=lease_id,
            model=self.model,
            principal_identity=self.principal_identity,
            provider=self.provider,
        )
        if self.inner is not None:
            task = self.inner(engine, task, lease_id=lease_id)
        return task


def multi_turn_policy_wrapper(policy: Policy) -> Policy:
    """Wrap a policy so interactive turns suspend instead of completing.

    The single ``policy_wrapper`` the :class:`InteractionDriver` hands the
    resolver: every Engine the resolver builds wraps the agent's policy in
    ``MultiTurnReActPolicy(final=False)``. ``send_goal`` is unconditionally
    ``final=False`` (lookahead stays a file-mode-only
    convenience and is *not* ported here), so a normally-finishing turn ends
    in a trailing next-goal suspend.
    """
    return MultiTurnReActPolicy(policy, final=False)


def _background_exit_notice(summary: str, ref: ContentRef, job_id: str) -> str:
    """Render the background-completion notice body (D3).

    One line of human summary + the content-addressed ``ref`` (so the model can
    deref the full output it never sees inline) + the ``job_id`` (so the model
    can ``shell_poll`` it). Kept tiny and pointer-only on purpose — the bytes
    live in the ContentStore."""
    return (
        f"{summary}\n"
        f'<background-job id="{job_id}" ref="{ref.hash}" size="{ref.size}"/>'
    )


def _background_subagent_notice(
    summary: str, result_text: str, subtask_id: str, status: str
) -> str:
    """Render the background-sub-agent completion notice body (Mechanism C).

    docs/adr/background-subagent.md. Mirrors :func:`_background_exit_notice`: one
    line of human summary + the sub-agent's ACTUAL result text (dereferenced from
    the ContentRef at delivery time so the model sees the real answer inline, not
    an opaque hash pointer it cannot read). The ``<background-subagent>`` tag
    carries the ``subtask_id`` and terminal ``status`` as a machine-greppable
    provenance marker. Inlining the result matches the foreground spawn path
    (``Engine.append_subagent_result_message`` dereferences via
    ``_deref_subagent_output``) and prevents the model from hallucinating a
    plausible-looking "result" when all it actually saw was an unreadable ref."""
    return (
        f"{summary}\n\n"
        f"{result_text}\n\n"
        f'<background-subagent id="{subtask_id}" status="{status}"/>'
    )


def _render_subagent_result(body: bytes) -> str:
    """Deref a sub-agent result body into inline notice text.

    Mirrors the foreground path (:meth:`Engine._deref_subagent_output`): a
    string answer renders as its raw text; a structured (dict / list) answer as
    JSON — never a Python ``repr`` — so the background notice shows the model the
    same shape a foreground ``tool_result`` would carry."""
    value = from_canonical_bytes(body)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _external_event_notice(event_kind: str, payload: Any) -> str:
    """Render the external-event payload notice body (``deliver_event``).

    Mirrors :func:`_background_exit_notice`: one human-readable line + a
    machine-greppable tag carrying the payload as deterministic JSON
    (``sort_keys`` keeps a resumed fold reading identical bytes). The payload
    rides the message channel, never the wake event — the wake domain rule on
    :class:`noeta.protocols.wake.ExternalEvent`."""
    try:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        body = json.dumps(str(payload), ensure_ascii=False)
    return (
        f"External event received: {event_kind}\n"
        f'<external-event kind="{event_kind}">{body}</external-event>'
    )


@dataclass(frozen=True, slots=True)
class DriveOutcome:
    """The folded result of one driven command — what the transport renders.

    ``status`` is the folded task status after the turn settled
    (``suspended`` for a normally-finishing interactive turn — the trailing
    next-goal suspend; ``terminal`` for a failed turn / a cancel).
    ``wake_handle`` is the ``HumanResponseReceived`` handle the task is now
    waiting on (``None`` when not suspended on a human handle) so a caller
    can tell a next-goal suspend from an ``approval-{call_id}`` suspend.
    """

    task_id: str
    status: str
    wake_handle: Optional[str]


@dataclass(frozen=True, slots=True)
class SeededTurn:
    """A created/woken Task whose turn has been seeded but **not yet driven**.

    The drive path splits in two so a transport can publish the
    ``task_id`` (and let an SSE/Trace consumer attach) *before* the blocking
    turn runs:

    * :meth:`InteractionDriver.seed_start` / ``seed_send_goal`` / ``seed_*``
      do every **durable, validated** step synchronously — create / wake,
      authorize the selector, write the opening ``ModelBound`` / goal /
      reopen, take the lease, and (D6) apply any append-type prelude —
      ``TaskWoken`` + the user's message are durable before the ack, so an
      acked command can never lose its input to a crash. A rejected
      selector or a not-suspended task still fails *here*, before any
      ``SeededTurn`` exists, so the transport keeps returning the same
      typed 4xx.
    * :meth:`InteractionDriver.drive_seeded` runs the actual turn
      (``run_leased_task`` + the S3b subtask drain) and folds the outcome.

    ``start`` / ``send_goal`` / ``approve`` / ``deny`` / ``answer`` remain the
    one-call seam (seed-then-drive) the CLI and the synchronous HTTP path use
    — byte-identical to the pre-split sequence. The async HTTP path calls
    ``seed_*`` on the request thread and ``drive_seeded`` on a background
    thread. ``prelude`` is ``None`` for ``start`` (the opening turn carries no
    woken prelude) and the typed woken-command prelude otherwise; ``lease`` is
    the dispatcher lease ``drive_seeded`` consumes (opaque to the transport).
    """

    task_id: str
    lease: Any
    prelude: Optional[WokenPrelude] = None
    #: The Engine resolved at seed time, BEFORE a seed-applied prelude's
    #: events landed (D6). Pinning it preserves the ``/model`` per-turn
    #: semantics: a seed-written ``ModelBound`` must drive the NEXT turn,
    #: but the drive's own fold would already see it. ``None`` (no
    #: seed-applied prelude) ⇒ the drive resolves as before.
    engine: Optional[Any] = None


# Hoisted to ``noeta.core.fold`` (the crash-recovery attempt seal in L2
# needs the same point-in-time bounded fold); aliased so the established
# in-module name keeps working.
_BoundedEventLog = BoundedEventLog


class InteractionDriver:
    """The shared conversation-command driver over a resident host.

    Wraps a :class:`noeta.execution.host.ResidentHost` (the issue-01
    Protocol seam: ``event_log`` / ``content_store`` / ``dispatcher`` +
    engine resolver + agent registry). Every command drives the canonical
    :func:`noeta.runtime.worker.run_leased_task` primitive against that
    host — there is no second task-creation or drive logic (D1).

    The driver is surface-agnostic: the ``python -m noeta.agent`` runner
    constructs one with the
    :data:`noeta.protocols.values.LOCAL_PRINCIPAL` (⊤ — no trust boundary,
    D3) over its in-process bundle; the web
    backend (``noeta.client.host`` driving ``noeta.agent.backend``) constructs one
    with the authenticated session's :class:`~noeta.protocols.values.Principal`
    over the server bundle. The model-selector check (D2) lives here, on
    the shared seam — ``selector ∈ principal.allowed_models ∩ allowlist`` —
    and refuses raw provider config by simply never accepting it as input
    (``start`` / ``send_goal`` take only ``goal`` / ``agent`` / a *selector*).
    """

    def __init__(
        self,
        host: ResidentHost,
        *,
        worker_id: str = "noeta-interaction",
        lease_seconds: float = 600.0,
        principal: Principal = LOCAL_PRINCIPAL,
        model_allowlist: frozenset[str] = STUB_MODEL_ALLOWLIST,
        default_model: Optional[str] = None,
    ) -> None:
        self._host = host
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        #: Issue 06 — who is acting + which models they may bind (
        #: D2/D3). CLI = ⊤ local principal; web = authenticated session.
        self._principal = principal
        #: Deployment allowlist — the *other* half of the selector check.
        self._model_allowlist = model_allowlist
        #: Host-fixed model bound when no selector is given (the deployment's
        #: own choice — not caller input). Defaults to the resolver's
        #: host-fixed model so the opening ``ModelBound`` records the same id
        #: the Engine would use anyway → byte-equal with the no-selector path.
        self._default_model = default_model or host.model

    # -- start ------------------------------------------------------------

    def start(
        self,
        *,
        goal: str,
        agent: str,
        model_selector: Optional[str] = None,
        provider_selector: Optional[str] = None,
        images: Sequence[ImageBlock] = (),
        host_binding: Optional[TaskHostBoundPayload] = None,
        workspace_dir: Optional[str] = None,
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        effort: Optional[str] = None,
        goal_origin: Optional[MessageOrigin] = None,
        attachment_texts: tuple[str, ...] = (),
        activations: tuple[str, ...] = (),
    ) -> DriveOutcome:
        """Create a Task from server-side config and drive its first turn.

        The one-call seam (seed-then-drive) the CLI and the synchronous HTTP
        path use — byte-identical to the pre-split sequence. The async HTTP
        path instead calls :meth:`seed_start` (request thread) and
        :meth:`drive_seeded` (background thread) so the ``task_id`` is
        published before the blocking turn runs.

        ``workspace_dir`` is the per-session workspace **absolute
        path** welded into the durable record (the agent layer resolves it; the
        driver only receives the final path); ``provider_selector``
        (I4) the per-session provider name —
        see :meth:`seed_start`. ``permission_mode`` (code-review) is
        the per-turn, NON-durable permission selector — also see :meth:`seed_start`.
        ``activations`` are the built-in skill names a slash command resolved to —
        see :meth:`seed_start`.
        """
        return self.drive_seeded(
            self.seed_start(
                goal=goal,
                agent=agent,
                model_selector=model_selector,
                provider_selector=provider_selector,
                images=images,
                host_binding=host_binding,
                workspace_dir=workspace_dir,
                permission_mode=permission_mode,
                enabled_mcp=enabled_mcp,
                effort=effort,
                goal_origin=goal_origin,
                attachment_texts=attachment_texts,
                activations=activations,
            )
        )

    def seed_start(
        self,
        *,
        goal: str,
        agent: str,
        model_selector: Optional[str] = None,
        provider_selector: Optional[str] = None,
        images: Sequence[ImageBlock] = (),
        host_binding: Optional[TaskHostBoundPayload] = None,
        workspace_dir: Optional[str] = None,
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        effort: Optional[str] = None,
        goal_origin: Optional[MessageOrigin] = None,
        attachment_texts: tuple[str, ...] = (),
        activations: tuple[str, ...] = (),
    ) -> SeededTurn:
        """Create a Task from server-side config and seed its first turn.

        Only ``goal`` / ``agent`` / ``model_selector`` come from the caller
        (D1/D2): the provider / endpoint / base_url / credentials /
        profile / tool-registry are all host-fixed on the resolver and are
        **never** read here (R4).

        D5: ``images`` is an additive channel —— the
        user's opening turn may carry :class:`ImageBlock`s alongside the goal
        text. They are appended after ``TextBlock(goal)`` in the seeded user
        message; an empty ``images`` keeps the seed byte-identical to the
        text-only path.

        Issue 06: the selector is validated against
        ``principal.allowed_models ∩ deployment-allowlist`` *before* anything
        durable is written; a rejected selector raises
        :class:`ModelSelectorError` and leaves **no** Task, **no**
        ``ModelBound``, **no** turn. On success the resolved bound model
        (the selector, or the host default when ``None``) is recorded as the
        **opening** ``ModelBound`` — written by the Engine right after
        ``TaskCreated`` so it sits in the pre-loop window (before
        ``TaskStarted``) and the resolver folds it to drive this first turn
        on the bound model. The seed Engine writes ``TaskCreated`` with the
        chosen ``agent`` (authoritative ``agent_name``, D2).

        ``workspace_dir`` is the per-session workspace **absolute
        path** welded into the durable record (the agent layer expands it once at
        ``POST /tasks``; the driver only receives the final absolute path). It is
        recorded on ``TaskHostBound.workspace_dir`` (merged into ``host_binding``
        when one was injected, else a workspace-only binding is minted); a
        resumed session reads this absolute path directly, no longer consulting
        a registry or base pool. It is ALSO passed to the seed Engine resolution here so the
        first turn runs in the session's own fs root. ``None`` ⇒ the host-fixed
        default workspace dir (the pre-decision path).

        (I4): ``provider_selector`` is the per-session provider
        **name** (never a key / endpoint — only the name selector). The
        ``(provider, model)`` pair is validated against the host's provider
        registry *before* anything durable is written (provider configured +
        model ∈ that provider's list); a bad pair raises
        :class:`ProviderSelectorError` and leaves NO Task / ``ModelBound`` /
        turn. The bound provider name is folded into the **opening**
        ``ModelBound`` (the same event the model rides) and passed to the seed
        Engine so the first turn runs on the right adapter. ``None`` ⇒ the host
        default provider (the pre-I4 single-provider path, byte-equal).

        (code-review): ``permission_mode`` is the per-turn permission
        selector (``"default"`` / ``"acceptEdits"`` / ``"bypassPermissions"``).
        Unlike workspace / provider it is **NOT durable** — never written to any
        event; instead it is stashed on the host (``note_turn_permission``) keyed
        by the new task_id so the seed-time resolve, the first-turn drive, and any
        approval-resume all derive the same gating set. ``None`` ⇒ the host-fixed
        default, byte-identical to every pre-#4 path.
        """
        bound_model = self._authorize_selector(model_selector)
        # I4: validate the (provider, model) pair on the ORIGINAL selector names
        # before any durable write. ``None`` provider ⇒ host default (no check).
        bound_provider = self._authorize_pair(
            provider_selector, model_selector, bound_model
        )
        host = self._host
        # ONE task-creation path — the resolver's seed Engine
        # (keyed on (agent, bound_model, workspace_dir)) writes TaskCreated from
        # server-side config. The agent_name is the only caller-influenced
        # field, and only via the allowlist-checked registry lookup the
        # resolver does. The absolute workspace_dir keys the seed
        # Engine so the first turn runs in the session's own fs root.
        # The seed Engine ONLY writes TaskCreated / ModelBound /
        # the seed user message — it never runs the ReAct loop, so it needs no
        # live MCP tools. We build it WITHOUT MCP (``mcp_aliases=()``) so the
        # single, authoritative MCP connection happens later at
        # ``resolve_engine(task)`` (line below), where the real ``task_id``
        # exists — that lets the connect's skip-on-failure record its
        # ``McpServerSkipped`` events on the task's own stream (the front-end
        # surface), and avoids a redundant pre-connect under a now-stale key.
        # Per-session sandbox (D4): eagerly provision THIS session's container
        # and weld its ``exec_env_ref`` into TaskHostBound (below) so a resumed /
        # reclaimed session reconnects to the SAME container, and pass the ref to
        # the seed resolve so the seed Engine targets it too. The container is
        # keyed by the ROOT task id, so we pre-mint it here (``create_task``
        # accepts an explicit ``task_id``) and hand it to ``allocate_exec_env``.
        # ``getattr`` guards a host / test double without the sandbox seam →
        # ``None`` (the local path, byte-identical: ``task_id`` stays ``None`` so
        # ``create_task`` mints it, exactly as before). Addressing only (D5).
        pre_minted_task_id: Optional[str] = None
        session_exec_env_ref: Optional[str] = None
        allocate = getattr(host, "allocate_exec_env", None)
        if callable(allocate):
            pre_minted_task_id = f"task-{uuid.uuid4().hex}"
            session_exec_env_ref = allocate(pre_minted_task_id, workspace_dir)
        seed_engine = host.resolve_engine_for_agent(
            agent, model=bound_model, workspace=workspace_dir, provider=bound_provider,
            permission_mode=permission_mode, effort=effort,
            exec_env_ref=session_exec_env_ref,
        )
        # Record the per-session workspace absolute path AND the sandbox
        # container address on the durable ``TaskHostBound`` so a resumed /
        # reclaimed session reproduces the same root dir and reconnects to the
        # same container. When a host binding was injected (CodeServer path)
        # merge them in; when none was (the bare CLI / web ConsoleBackend path
        # passes no binding) mint a binding carrying whichever is set. Both
        # ``None`` keeps the no-binding path untouched (byte-identical).
        bound_host_binding = host_binding
        if workspace_dir or session_exec_env_ref:
            if bound_host_binding is None:
                bound_host_binding = TaskHostBoundPayload(
                    host_id="",
                    workspace_dir=workspace_dir,
                    exec_env_ref=session_exec_env_ref,
                )
            else:
                bound_host_binding = dataclasses.replace(
                    bound_host_binding,
                    workspace_dir=workspace_dir,
                    exec_env_ref=session_exec_env_ref,
                )
        task = seed_engine.create_task(
            goal=goal,
            policy_name="react",
            agent_name=agent,
            host_binding=bound_host_binding,
            # Sandbox path pre-minted the root id so the container is keyed by it
            # (D4); ``None`` (the local path) lets ``create_task`` mint as before.
            task_id=pre_minted_task_id,
        )
        # A NEW user goal opens a turn — clear the per-turn
        # file-checkpoint gate so this turn re-stashes fresh baselines (root =
        # the new top-level task). No-op on a brand-new task, kept for symmetry
        # with ``seed_send_goal``.
        self._reset_file_checkpoint_turn(task.task_id)
        # Stash the per-turn, NON-durable permission_mode
        # now that the task_id exists, so the resolve_engine below AND the (possibly
        # background-thread) drive both derive the same gating set. ``None`` ⇒ host
        # default (byte-equal). Never written to the event log.
        host.note_turn_permission(task.task_id, permission_mode)
        note_effort = getattr(host, "note_turn_effort", None)
        if note_effort is not None:
            note_effort(task.task_id, effort)
        # Stash the per-turn enabled-MCP-alias list (clean names, no
        # url/token) so the resolve_engine below + the (possibly background-thread)
        # drive both connect the SAME servers for this turn. ``()`` ⇒ no live MCP
        # (byte-equal). Guarded: a host Protocol impl that omits it is a no-op.
        note_mcp = getattr(host, "note_turn_mcp", None)
        if note_mcp is not None:
            note_mcp(task.task_id, enabled_mcp)
        host.dispatcher.enqueue(task.task_id)
        lease = host.dispatcher.lease(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            task_id=task.task_id,
        )
        if lease is None:
            raise RuntimeError(
                f"dispatcher gave no lease for freshly enqueued task "
                f"{task.task_id!r}"
            )
        # Opening binding (issue 06): emit ModelBound BEFORE the goal/step so
        # it lands in the pre-loop window AND so run_leased_task's fold sees
        # the binding and resolves the bound-model Engine for this first turn.
        seed_engine.note_model_bound(
            task,
            lease_id=lease.lease_id,
            model=bound_model,
            principal_identity=self._principal.identity,
            provider=bound_provider,
        )
        # Seed the goal as the first user turn (durable, resume-safe) BEFORE
        # the step — same shape the in-process CodeSessionRunner uses.
        engine = host.resolve_engine(task)
        # Memory auto-recall (the deleted runner's prepare-time D5/D6 wiring,
        # ported onto the seed path). The host seam resolves the (store,
        # entries) pair ONLY for an agent whose spec enables
        # ``Capabilities.memory`` (``None`` otherwise) — a memory-off agent's
        # stream stays byte-identical, and the ``getattr`` guard keeps hosts
        # without the seam (test doubles / control-plane-only hosts) a clean
        # no-op. Runner-prepare order: the index resident is recorded FIRST
        # (one ``ContextContentRecorded`` kind=memory, policy=evolving; empty
        # entries no-op), then the goal enters through the recall seam below.
        # Retrieval runs on the WRITE side (now, at recording time), never at
        # compose time — hits land as ONE ``origin="memory"`` turn right after
        # the human goal, and resume folds them back without re-retrieving.
        memory_store = None
        recall_context = getattr(host, "memory_recall_context", None)
        if callable(recall_context):
            memory = recall_context(agent)
            if memory is not None:
                memory_store, memory_entries = memory
                record_memory_index(
                    host.event_log, host.content_store, task,
                    entries=memory_entries, lease_id=lease.lease_id,
                )
        # Unified ``@`` mention snapshots (workspace files + MCP
        # static resources, read host-side at send time) seed FIRST, each as its
        # own ``origin="system"`` user message — so the transcript attributes the
        # injected reference material distinctly from the human goal that follows.
        # Being ordinary recorded messages, resume reads them back, never re-reads.
        for text in attachment_texts:
            engine.append_user_message(
                task,
                content=[TextBlock(text=text)],
                lease_id=lease.lease_id,
                origin="system",
            )
        # ``goal_origin`` tags the opening message's
        # author source. ``None`` (a human-typed goal) keeps it byte-identical;
        # an MCP-prompt-expanded opening goal arrives ``origin="system"`` so the
        # transcript shows host-injected content riding the user channel — and,
        # being a recorded message, resume reads it back and never re-expands.
        # A memory-enabled session routes the goal through the recall seam
        # (the SDK port of the runner's ``append_user_message_with_recall``
        # intake): identical goal bytes, plus one ``origin="memory"`` follow-up
        # turn when the store has hits — no hits ⇒ exactly the plain bytes.
        if memory_store is not None:
            task = append_user_message_with_recall(
                engine, task,
                content=[TextBlock(text=goal), *images],
                lease_id=lease.lease_id,
                store=memory_store,
                origin=goal_origin,
            )
        else:
            task = engine.append_user_message(
                task,
                content=[TextBlock(text=goal), *images],
                lease_id=lease.lease_id,
                origin=goal_origin,
            )
        # Web-path parity: a slash command (``/review``-style) the host
        # resolved to built-in skill(s) deterministically pins their bodies for
        # this turn onward — the same pre-loop activation the resident CLI runner
        # does via ``activate_skills``, mirroring Claude Code's ``/skill-name``.
        # Emitted AFTER the goal message (goal-then-patch order); the Engine's
        # ``apply_state_patch`` records the per-skill content provenance itself
        # (fold-guarded, first-only). ``()`` ⇒ byte-identical to the no-skill path.
        if activations:
            engine.apply_state_patch(
                task,
                patch=TaskStatePatch(activate_skills=list(activations)),
                lease_id=lease.lease_id,
            )
        # Pre-loop activation of the session-level instructions + environment
        # content channels — the SAME activation the resident
        # ``AgentSessionRunner.prepare()`` did (skills → instructions →
        # environment order; the memory index is recorded above, at the recall
        # seam). Without this the server seed path emitted NO
        # ``ContextContentRecorded(kind=environment|instructions)``, so a
        # server-created task's request never carried the workspace dir / git /
        # platform block nor the project AGENTS.md/NOETA.md. ``seed_start`` is the
        # once-per-session open (the prepare() counterpart), so record here and
        # not in ``seed_send_goal`` (per-turn goal append); both record functions
        # are first-only/idempotent anyway, so an old task whose first interaction
        # is a send_goal is harmless. Snapshots come from the host's
        # ``session_content_snapshots`` (the same workspace_dir / instructions_file
        # resolution + the same pure loaders ``build_session_inputs`` feeds the
        # composer), so the recorded fingerprint matches the bytes this session's
        # composer renders. ``getattr``-guarded so a host without the seam (test
        # doubles / control-plane-only hosts) is a clean no-op.
        snapshots = getattr(self._host, "session_content_snapshots", None)
        if callable(snapshots):
            environment_snapshot, instructions_snapshot = snapshots(workspace_dir)
            record_instructions(
                host.event_log, host.content_store, task,
                snapshot=instructions_snapshot, lease_id=lease.lease_id,
            )
            record_environment(
                host.event_log, host.content_store, task,
                snapshot=environment_snapshot, lease_id=lease.lease_id,
            )
        return SeededTurn(task_id=task.task_id, lease=lease, prelude=None)

    def drive_seeded(self, seeded: SeededTurn) -> DriveOutcome:
        """Drive a :class:`SeededTurn` to its trailing suspend / terminal.

        The second half of the split drive path: runs the canonical
        ``run_leased_task`` primitive (with the seeded woken prelude, if any),
        drains any S3b delegation subtree synchronously, then folds the
        outcome. Identical work to the tail of the pre-split ``start`` /
        ``_drive_woken`` — only *when* it runs (request thread vs background
        thread) differs between the synchronous and async transports.
        """
        try:
            # ``keep_lease_alive`` renews the lease while the step runs: this
            # transport has no resident WorkerLoop heartbeat, so a step longer
            # than the lease TTL (a slow LLM round-trip retried to its budget)
            # would otherwise lose its lease mid-flight and fail its own
            # terminal write (InvalidLease) — hanging the task non-terminal.
            with keep_lease_alive(self._host.dispatcher, seeded.lease):
                if seeded.prelude is None:
                    run_leased_task(
                        self._host, seeded.lease,
                        next_goal_handle=NEXT_GOAL_WAKE_HANDLE,
                        engine=seeded.engine,
                    )
                else:
                    run_leased_task(
                        self._host, seeded.lease, prelude=seeded.prelude,
                        next_goal_handle=NEXT_GOAL_WAKE_HANDLE,
                        engine=seeded.engine,
                    )
        except InvalidLease as exc:
            # The lease lapsed mid-step despite the heartbeat (cap hit, or an
            # external reclaim), so the step's own terminal write was rejected.
            # Unlike the WorkerLoop path there is NO resident worker to retry a
            # requeued task here, so leaving it would hang the task non-terminal
            # forever (the UI can neither resume nor delete it). Converge it to a
            # durable terminal via the control plane, then re-raise so the
            # transport still surfaces / logs the fault.
            self._force_terminal_on_lost_lease(seeded.task_id, exc)
            raise
        except Exception as exc:  # noqa: BLE001
            # The async transport drives this off the request thread, so unlike
            # the WorkerLoop path there is no outer ``_execute_step`` to fail
            # the lease on an in-process / storage fault. Without this the lease
            # would leak until its TTL (next-goal 409s meanwhile). Fail it
            # retryable (the dispatcher decides requeue-vs-terminal via
            # max_fail_attempts), then re-raise so the transport still surfaces
            # / logs the error.
            self._host.dispatcher.fail(
                seeded.lease.lease_id, retryable=True, reason=str(exc)
            )
            raise
        # S3b: if this turn delegated, drive the (possibly nested) delegation
        # tree to terminal synchronously before folding the outcome. The drain
        # leases + runs each node itself (each wrapped in its own heartbeat); a
        # node that still loses its lease converges to terminal the same way.
        try:
            self._drain_pending_subtasks(seeded.task_id)
        except InvalidLease as exc:
            self._force_terminal_on_lost_lease(seeded.task_id, exc)
            raise
        except UnsupportedSubtaskSuspend:
            # A delegated descendant suspended for approval / human input
            # (the drain released every lease in its true suspended state
            # before raising, so the whole tree is durably consistent). This
            # is a legitimate suspend, NOT a transport error: the child's
            # ToolCallApprovalRequested is on its own stream (the SSE tree
            # surfaces it), and the later approve / deny / answer on the
            # child re-enters the tree via _resume_woken_ancestors below.
            # Raising here used to 409 the command AND strand the parent
            # forever — no code path ever re-entered the drain.
            pass
        # Out-of-band parent resume: if THIS driven task settled a child
        # whose parent tree was stranded on an approval suspend, the
        # ChildLifecycleObserver has already delivered the parent's wake —
        # walk up and drive the woken ancestors.
        self._resume_woken_ancestors(seeded.task_id)
        return self._outcome(seeded.task_id)

    def _force_terminal_on_lost_lease(
        self, task_id: str, exc: Exception
    ) -> None:
        """Converge a task whose in-flight step lost its lease to a durable
        terminal.

        The in-request transports run a leased step with no resident
        :class:`WorkerLoop`; if the lease lapses mid-step (heartbeat cap, or an
        external reclaim) the step's own terminal write is rejected
        (``InvalidLease``) and, because nothing re-drives a requeued task here,
        the task would hang non-terminal forever — the UI can neither resume
        (not at a next-goal suspend) nor delete it (the dispatcher still reads
        an active lease). Mirroring :meth:`cancel`, write a **lease-free**
        control-plane ``TaskFailed`` (``system_emit``, no lease / no
        ``state_patch``) so fold always reaches a terminal. No-op when the task
        already terminal — a racing reclaim/cancel won, or the step did land
        its own terminal before the lease lapsed.
        """
        host = self._host
        task = fold(host.event_log, host.content_store, task_id)
        if getattr(task, "status", None) == "terminal":
            return
        host.event_log.system_emit(
            task_id=task_id,
            type="TaskFailed",
            payload=TaskFailedPayload(
                reason=f"execution lease lost mid-step: {exc}",
                retryable=False,
            ),
            actor="interaction-driver",
            origin="system",
            trace_id=self._trace_id(task_id),
        )
        # A lost-lease terminal must also reap the session's background shell
        # jobs, same as cancel/close (an orphaned ``npm run dev`` outlives the
        # task that started it). ``getattr`` so a host without background
        # execution (test doubles) is a clean no-op.
        kill_bg = getattr(host, "kill_background_session", None)
        if callable(kill_bg):
            kill_bg(task_id)

    # -- per-turn commands ------------------------------------------------

    def send_goal(
        self,
        task_id: str,
        *,
        goal: str,
        model_selector: Optional[str] = None,
        provider_selector: Optional[str] = None,
        images: Sequence[ImageBlock] = (),
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        effort: Optional[str] = None,
        goal_origin: Optional[MessageOrigin] = None,
        attachment_texts: tuple[str, ...] = (),
        activations: tuple[str, ...] = (),
    ) -> DriveOutcome:
        """Append a new user turn and drive it (issue-01 append-message).

        Requires the conversation to be suspended on the next-goal handle
        (a normally-finishing interactive turn). Walks the real dispatcher
        contract (``wake`` → targeted ``lease`` consuming the matched wake)
        then hands the lease to ``run_leased_task`` with an
        ``AppendMessagePrelude`` — the single drive primitive, never an
        inline second resume machine.

        Issue 06 — ``model_selector`` is the per-turn ``/model`` switch.
        When given, it is validated against
        ``principal.allowed_models ∩ allowlist`` (rejected → no switch, no
        durable write) and emitted as a ``ModelBound`` in the same
        post-``TaskWoken`` window as the appended goal (a
        :class:`ModelBindPrelude` chaining the append). Mirroring Claude
        Code's ``/model``, the new binding takes effect **going forward**:
        the seed resolves the driving Engine *before* applying the prelude
        (D6 seed-time durability) and the drive reuses that pinned Engine,
        so this turn runs on the binding in force when it started and the
        switch drives the **next** turn (whose fold now sees the new
        ``ModelBound``). With no selector the conversation keeps its
        current binding (no ``ModelBound`` written).

        ``provider_selector`` is the per-turn provider switch
        (folded into the SAME ``ModelBound`` as the model). Like
        ``model_selector`` the new binding takes effect **going forward** (the
        next turn). When only one of the two selectors is given the other sticks
        at its current binding (switchable per turn: a model-only turn leaves the
        provider unchanged, and vice-versa). A bad ``(provider, model)`` pair raises
        :class:`ProviderSelectorError` before any durable write — see
        :meth:`seed_send_goal`.
        """
        return self.drive_seeded(
            self.seed_send_goal(
                task_id,
                goal=goal,
                model_selector=model_selector,
                provider_selector=provider_selector,
                images=images,
                permission_mode=permission_mode,
                enabled_mcp=enabled_mcp,
                effort=effort,
                goal_origin=goal_origin,
                attachment_texts=attachment_texts,
                activations=activations,
            )
        )

    def seed_send_goal(
        self,
        task_id: str,
        *,
        goal: str,
        model_selector: Optional[str] = None,
        provider_selector: Optional[str] = None,
        images: Sequence[ImageBlock] = (),
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        effort: Optional[str] = None,
        goal_origin: Optional[MessageOrigin] = None,
        attachment_texts: tuple[str, ...] = (),
        activations: tuple[str, ...] = (),
    ) -> SeededTurn:
        """Validate + seed an appended-goal turn without driving it.

        The synchronous, durable half of :meth:`send_goal` (issue-01
        append-message): refuse a non-next-goal-suspended task, authorize any
        per-turn selector, write the CW2 reopen, then wake + lease. Raises
        *before* any :class:`SeededTurn` on a rejected selector / wrong wake —
        so the async transport returns the same typed 4xx as the synchronous
        path; only ``run_leased_task`` moves off the request thread.

        ``provider_selector`` is the per-turn provider switch,
        folded into the SAME ``ModelBound`` as the model (switchable per turn). When
        only ONE of model / provider is given the other sticks at its current binding:
        a provider-only switch re-records the *current* model alongside the new
        provider (so fold keeps the model), and a model-only switch leaves
        ``provider=None`` on the event (so fold keeps the provider). When at
        least one selector is given the validated ``(provider, model)`` pair is
        checked *before* any durable write; a bad pair raises
        :class:`ProviderSelectorError` (no reopen / ModelBound / turn).
        """
        task = self._require_human_suspend(task_id, NEXT_GOAL_WAKE_HANDLE)
        # Human-stop hygiene: a prior ``close`` (or cancel attempt) pressed while
        # NO turn was in flight leaves its cancel-registry mark behind. An
        # explicit new goal means "go", so clear any stale mark FIRST — else this
        # fresh turn would trip its own first poll and abort. A mark that lands
        # DURING this turn still trips normally. No-op without the host seam.
        discard = getattr(self._host, "discard_cancellation", None)
        if callable(discard):
            discard(task_id)
        # This new user goal opens a fresh turn — clear the per-turn
        # file-checkpoint gate (root = this top-level task) so a file edited in
        # an EARLIER turn re-stashes its turn-start baseline now, instead of the
        # earlier turn's gate suppressing it (which would over-revert a rewind to
        # THIS turn back to the earlier turn's content).
        self._reset_file_checkpoint_turn(task_id)
        # Build the woken prelude, validating any selector FIRST: a rejected
        # selector must leave NO durable write (no reopen, no ModelBound, no
        # turn), so ``_authorize_selector`` / ``_authorize_pair`` raise before
        # the CW2 reopen below.
        # ``goal_origin`` tags this appended turn's
        # author source (``None`` ⇒ a human follow-up goal, byte-identical;
        # ``"system"`` ⇒ an MCP-prompt-expanded goal — host-injected content the
        # transcript attributes, and a recorded message resume reads back without
        # re-expanding).
        # The unified ``@`` mention snapshots seed ahead of the goal
        # as their own ``origin="system"`` messages (see AppendMessagePrelude).
        # A memory-enabled agent (host seam, ``None`` for a memory-off spec —
        # byte-identical stream) swaps in ``RecallGoalPrelude``: the SDK port of
        # the runner's ``_goal_prelude`` seam, so a resume goal gets the same
        # recall intake the opening turn got. The recall reads the store live
        # inside the woken window, so a memory written by an EARLIER turn's
        # ``memory_write`` is immediately recallable.
        append: WokenPrelude = AppendMessagePrelude(
            content=[TextBlock(text=goal), *images],
            origin=goal_origin,
            attachment_texts=tuple(attachment_texts),
            activate_skills=tuple(activations),
        )
        recall_context = getattr(self._host, "memory_recall_context", None)
        if callable(recall_context):
            memory = recall_context(
                agent_name_of(self._host.event_log, task_id)
            )
            if memory is not None:
                memory_store, _memory_entries = memory
                append = RecallGoalPrelude(
                    content=[TextBlock(text=goal), *images],
                    store=memory_store,
                    origin=goal_origin,
                    attachment_texts=tuple(attachment_texts),
                    activate_skills=tuple(activations),
                )
        prelude: WokenPrelude
        if model_selector is None and provider_selector is None:
            # No per-turn switch — the conversation keeps both bindings; no
            # ModelBound is written (byte-equal with the pre-I4 no-selector path).
            prelude = append
        else:
            # The model riding this ModelBound: the new selector when given,
            # else the CURRENT folded model binding (a provider-only switch must
            # re-record the model so fold does not lose it). ``None`` current
            # binding (a session that never bound) ⇒ the driver default model.
            if model_selector is not None:
                bound_model = self._authorize_selector(model_selector)
            else:
                current = getattr(task.governance, "model_binding", None)
                bound_model = (
                    current if isinstance(current, str) and current
                    else self._default_model
                )
            # Validate against the EFFECTIVE pair in force after this turn: the
            # provider the user switched to, else the current sticky provider
            # binding. So a model-only switch still re-checks (current_provider,
            # new_model) — a model the sticky provider does not offer is rejected.
            effective_provider = provider_selector
            if effective_provider is None:
                cur = getattr(task.governance, "provider_binding", None)
                effective_provider = cur if isinstance(cur, str) and cur else None
            self._authorize_pair(
                effective_provider, model_selector, bound_model
            )
            # What to WRITE on the ModelBound: only the explicitly-switched
            # provider name (``None`` ⇒ provider sticks, fold keeps the current
            # binding). The effective name above is for validation only.
            bound_provider = provider_selector
            prelude = ModelBindPrelude(
                model=bound_model,
                principal_identity=self._principal.identity,
                inner=append,
                provider=bound_provider,
            )
        # Stash this turn's per-turn, NON-durable
        # permission_mode keyed by task_id — set AFTER selector authorization (a
        # rejected selector raises above, leaving the prior turn's mode intact) so
        # the reopen-resolve below and the (background-thread) drive both derive
        # the same gating set. ``None`` ⇒ host default (byte-equal); overwrites the
        # previous turn's value so the frontend selector is the per-turn source.
        self._host.note_turn_permission(task_id, permission_mode)
        note_effort = getattr(self._host, "note_turn_effort", None)
        if note_effort is not None:
            note_effort(task_id, effort)
        # Stash this turn's enabled-MCP aliases (clean names, no
        # url/token) so the reopen-resolve + drive connect the same servers. ``()``
        # ⇒ no live MCP (byte-equal). Guarded — a host without the seam is a no-op.
        note_mcp = getattr(self._host, "note_turn_mcp", None)
        if note_mcp is not None:
            note_mcp(task_id, enabled_mcp)
        # CW2: a new goal on a closed+suspended
        # conversation implicitly reopens it. Emit ``ConversationReopened`` in
        # the suspend window — BEFORE the wake — so it lands in
        # ``[TaskSuspended, TaskWoken)``, the control-event window fold folds the
        # reopen from when it reconstructs the resumed turn.
        # Only when actually closed → no event on the common (never-closed)
        # path, so old recordings drift nowhere. ``closed`` is a lifecycle flag
        # and does NOT itself imply any wake type; the suspended-on-NEXT_GOAL
        # guarantee comes solely from ``_require_human_suspend`` above, so a
        # non-next-goal wake can never reach this reopen.
        if task.governance.closed:
            engine = self._host.resolve_engine(task)
            engine.note_conversation_reopened(
                task,
                reopened_by=self._principal.identity,
                reason="new goal",
            )
        return self._seed_woken(
            task_id, handle=NEXT_GOAL_WAKE_HANDLE, prelude=prelude
        )

    def notify_background_exit(
        self,
        task_id: str,
        *,
        summary: str,
        ref: ContentRef,
        job_id: str,
    ) -> DriveOutcome:
        """Push a background-command completion notice and
        drive a turn (Mechanism C).

        The host's background-drive thread calls this when a
        ``shell_run(background=true)`` job exits while the session is idle. It
        **reuses the next-goal wake handle** (no new wake primitive — AC) to
        wake the session and inject an ``origin="system"`` notice that mirrors
        ``send_goal``'s append-message prelude. The session is driven a new turn
        with NO human input, so the agent can react to the completion on its own.

        The notice carries ONLY the one-line ``summary`` + the final
        :class:`ContentRef` (and the ``job_id`` so the model can ``shell_poll``):
        the full output never inlines — the model derefs the ref itself
        (pass a pointer, not the full text). ``origin="system"`` lets the model tell a
        background event from a user message (source label).

        Requires the conversation to be suspended on the next-goal handle (a
        normally-finishing interactive turn); a task that is mid-turn or terminal
        raises :class:`RuntimeError` from :meth:`_require_human_suspend` so the
        host's three-state handler can re-arm (mid-turn) or drop (terminal) —
        the durable ``BackgroundShellExited`` already records the fact regardless.
        """
        return self.drive_seeded(
            self.seed_notify_background_exit(
                task_id, summary=summary, ref=ref, job_id=job_id
            )
        )

    def seed_notify_background_exit(
        self,
        task_id: str,
        *,
        summary: str,
        ref: ContentRef,
        job_id: str,
    ) -> SeededTurn:
        """Validate + seed a background-completion notice turn (the seed half).

        Mirrors :meth:`seed_send_goal`: refuse a non-next-goal-suspended task
        (so a mid-turn / terminal session raises here, before any
        :class:`SeededTurn`), then wake + lease with an
        :class:`AppendMessagePrelude` carrying the source-tagged notice. Unlike
        ``send_goal`` there is no selector / reopen to authorize — a background
        notice never switches the model and never reopens a closed conversation
        (it is not a human turn).
        """
        self._require_human_suspend(task_id, NEXT_GOAL_WAKE_HANDLE)
        notice = _background_exit_notice(summary, ref, job_id)
        return self._seed_woken(
            task_id,
            handle=NEXT_GOAL_WAKE_HANDLE,
            prelude=AppendMessagePrelude(
                content=[TextBlock(text=notice)], origin="system"
            ),
        )

    def notify_background_subagent_exit(
        self,
        task_id: str,
        *,
        subtask_id: str,
        summary: str,
        ref: ContentRef,
        status: str,
    ) -> DriveOutcome:
        """Push a background SUB-AGENT completion notice and drive a turn (C).

        docs/adr/background-subagent.md. The background-subagent driver calls
        this (off a daemon thread) when a sub-agent launched with
        ``spawn_subagent(background=True)`` reaches terminal while the parent
        session is idle. It is the exact analogue of
        :meth:`notify_background_exit`: it **reuses the next-goal wake handle**
        (no new wake primitive) to wake the session and inject an
        ``origin="system"`` notice carrying a one-line ``summary`` + the
        sub-agent's inlined result text (dereferenced from the result
        :class:`ContentRef` at delivery so the model reads the real answer, not an
        opaque pointer it cannot resolve), so the agent reacts to the completion
        on its own — **proactive, no user prompt, no poll**.

        Distinct from the shell path in ONE way: it first emits a
        ``BackgroundSubagentDelivered`` on the parent stream — the exactly-once
        DELIVERY ANCHOR. fold flips the child's audit entry to its terminal
        ``status`` and records it delivered, so a crash-recovery scan never
        re-injects an already-delivered notice. The emit happens AFTER
        ``_require_human_suspend`` confirms the session is idle-suspended on the
        next-goal handle, so a mid-turn / terminal session raises before any
        anchor is written (the driver's caller swallows it; the child's own
        terminal still records the fact, and recovery re-attempts).
        """
        return self.drive_seeded(
            self.seed_notify_background_subagent_exit(
                task_id, subtask_id=subtask_id, summary=summary,
                ref=ref, status=status,
            )
        )

    def seed_notify_background_subagent_exit(
        self,
        task_id: str,
        *,
        subtask_id: str,
        summary: str,
        ref: ContentRef,
        status: str,
    ) -> SeededTurn:
        """Validate, deref the result, write the delivery anchor, seed the notice.

        Mirrors :meth:`seed_notify_background_exit`: refuse a
        non-next-goal-suspended task (so a mid-turn / terminal session raises
        here, before any anchor / SeededTurn), THEN deref + render the result body
        (which can also fault — kept before the anchor so a content fault retries
        idempotently instead of stranding a duplicate anchor), THEN emit the
        exactly-once ``BackgroundSubagentDelivered`` anchor on the parent stream
        (``system_emit`` — no lease; the parent is suspended), THEN wake + lease
        with the source-tagged notice prelude. The anchor lands before the notice
        turn's writes so fold sees ``...Started... Delivered... <notice turn>``
        in order."""
        self._require_human_suspend(task_id, NEXT_GOAL_WAKE_HANDLE)
        # Deref + render the child's result BEFORE writing the delivery anchor.
        # The anchor is non-idempotent and the driver's caller retries this whole
        # method on any exception (the host notify loop). A deref fault (content
        # GC'd / evicted / not on this host's store, or a non-canonical body)
        # raised here — before the anchor — retries idempotently, exactly like the
        # _require_human_suspend refusal above, instead of stacking duplicate
        # BackgroundSubagentDelivered anchors and then dropping the result. The
        # model sees the ACTUAL text inline (matching the foreground spawn path,
        # _deref_subagent_output); the ref is still recorded in the anchor below
        # for provenance / re-delivery.
        result_text = _render_subagent_result(self._host.content_store.get(ref))
        self._host.event_log.system_emit(
            task_id=task_id,
            type="BackgroundSubagentDelivered",
            payload=BackgroundSubagentDeliveredPayload(
                subtask_id=subtask_id,
                result_ref=ref,
                summary=summary,
                status=status,
            ),
            actor="background-subagent",
            origin="observer",
            trace_id=self._trace_id(task_id),
        )
        notice = _background_subagent_notice(summary, result_text, subtask_id, status)
        return self._seed_woken(
            task_id,
            handle=NEXT_GOAL_WAKE_HANDLE,
            prelude=AppendMessagePrelude(
                content=[TextBlock(text=notice)], origin="system"
            ),
        )

    def approve(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "driver",
    ) -> DriveOutcome:
        """Approve a pending gated tool call and resume (issue-01 prelude)."""
        return self._resolve_approval(
            task_id, call_id=call_id, approved=True, reason=reason,
            resolver=resolver,
        )

    def deny(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "driver",
    ) -> DriveOutcome:
        """Deny a pending gated tool call and resume (issue-01 prelude)."""
        return self._resolve_approval(
            task_id, call_id=call_id, approved=False, reason=reason,
            resolver=resolver,
        )

    def answer(
        self,
        task_id: str,
        *,
        question_id: str,
        answers: dict[str, Any],
        answered_by: str = "driver",
    ) -> DriveOutcome:
        """Answer a pending structured user question and resume."""
        return self.drive_seeded(
            self.seed_answer(
                task_id,
                question_id=question_id,
                answers=answers,
                answered_by=answered_by,
            )
        )

    def seed_answer(
        self,
        task_id: str,
        *,
        question_id: str,
        answers: dict[str, Any],
        answered_by: str = "driver",
    ) -> SeededTurn:
        """Validate + seed an answer-and-resume turn (async transport half)."""
        task = self._require_human_suspend(task_id, question_handle(question_id))
        pending = task.governance.pending_questions.get(question_id)
        if pending is None:
            raise RuntimeError(
                f"task {task_id!r} is suspended on question-{question_id} "
                "but has no matching pending question"
            )
        questions = load_questions_body(
            self._host.content_store, pending["questions_ref"]
        )
        normalized = normalize_answer_document({"answers": answers}, questions)
        return self._seed_woken(
            task_id,
            handle=question_handle(question_id),
            prelude=AnswerUserQuestionPrelude(
                question_id=question_id,
                answers=normalized,
                answered_by=answered_by,
            ),
        )

    def deliver_event(
        self,
        task_id: str,
        *,
        event_kind: str,
        payload: Any = None,
    ) -> DriveOutcome:
        """Deliver an external event and resume a ``wait_external`` suspend."""
        return self.drive_seeded(
            self.seed_deliver_event(
                task_id, event_kind=event_kind, payload=payload
            )
        )

    def seed_deliver_event(
        self,
        task_id: str,
        *,
        event_kind: str,
        payload: Any = None,
    ) -> SeededTurn:
        """Validate + seed an external-event resume turn (async transport half).

        The external-ingress counterpart of :meth:`seed_answer`: refuse a task
        that is not suspended on ``ExternalEvent(event_kind)`` — a mismatched
        ``event_kind``, a task not waiting at all, a terminal task, and a
        repeated delivery after the wake was consumed all raise the same typed
        :class:`NotResumableError` a repeat answer does — then wake + lease
        through the SAME dispatcher contract the internal delivery path (a
        daemon ingress calling ``dispatcher.wake``) walks. Matching projects
        on ``event_kind`` exactly as :func:`noeta.protocols.wake.matches_wake`
        defines.

        ``payload`` is the optional JSON value the external source carries.
        Per the wake domain rule (any payload belongs on the caller's own
        channel — a recorded message — never on the wake event), it does NOT
        ride the wake: when given, it is appended as an ``origin="system"``
        user message in the H2 first-consume window (the background-notice
        idiom, via :class:`AppendMessagePrelude`). ``None`` seeds no prelude,
        keeping the resumed turn byte-identical to an internal wake delivery.
        """
        self._require_external_suspend(task_id, event_kind)
        prelude: Optional[WokenPrelude] = None
        if payload is not None:
            prelude = AppendMessagePrelude(
                content=[
                    TextBlock(text=_external_event_notice(event_kind, payload))
                ],
                origin="system",
            )
        return self._seed_woken(
            task_id,
            handle=event_kind,
            prelude=prelude,
            condition=ExternalEvent(event_kind=event_kind),
        )

    def cancel(
        self, task_id: str, *, reason: str = "cancelled", cascade: bool = False
    ) -> DriveOutcome:
        """Cancel a conversation by writing the L0 ``TaskCancelled`` event.

        Forbids manufacturing a ``TaskCompleted`` terminal from the
        control plane (that would fake a policy ``Decision``). ``cancel`` is
        different: ``TaskCancelled`` is a **pre-existing L0 control event**
        (no new schema; absent from any historical recording → byte-safe),
        and fold already treats it as a terminal lifecycle event. It is
        written via ``system_emit`` (an observer-style control-plane write,
        no lease / no ``state_patch``), so it does not race the Engine's
        single ``RuntimeState`` writer.

        Refuses a task that is already terminal (nothing to cancel).
        """
        host = self._host
        task = fold(host.event_log, host.content_store, task_id)
        if task.status == "terminal":
            raise TaskAlreadyTerminalError(task_id=task_id, verb="cancel")
        trace_id = self._trace_id(task_id)
        host.event_log.system_emit(
            task_id=task_id,
            type="TaskCancelled",
            payload=TaskCancelledPayload(reason=reason, cascade=cascade),
            actor="interaction-driver",
            origin="system",
            trace_id=trace_id,
        )
        # cancel-cascade: mark the runtime registry AFTER the durable
        # TaskCancelled is written, so an in-flight delegation drain that
        # observes the cancel always folds an already-terminal root. The
        # registry lets a child mid-flight abandon its result at the next
        # turn boundary (in-process accelerator only — never resumed). Hosts
        # without the seam (some test doubles) silently skip it.
        request = getattr(host, "request_cancellation", None)
        if callable(request):
            request(task_id)
        # Human emergency-stop: a cancel must also kill
        # the session's background shell jobs (a long-running ``npm run dev`` /
        # ``make build`` outlives the task that started it, so cancelling the
        # conversation must not leave orphans). Reuses the per-job kill primitive
        # via the host seam; ``getattr`` so a host without background execution
        # (test doubles) is a clean no-op. issue 04's session-CLOSE cascade
        # reuses the SAME ``kill_background_session`` primitive.
        kill_bg = getattr(host, "kill_background_session", None)
        if callable(kill_bg):
            kill_bg(task_id)
        # background sub-agent cascade (docs/adr/background-subagent.md): the
        # ``request_cancellation`` mark above already makes each in-flight
        # background child abandon its drive at the next step boundary (the
        # ``DrainHost.cancel_check`` polls it); this just frees the registry's
        # per-session cap table. ``getattr`` so a host without it is a no-op.
        forget_bg = getattr(host, "forget_background_subagents", None)
        if callable(forget_bg):
            forget_bg(task_id)
        # Free the per-turn carrier entries (permission_mode / effort / mcp
        # aliases) so they don't outlive the conversation — otherwise a
        # long-lived server leaks one entry per carrier per task forever.
        forget_carriers = getattr(host, "forget_turn_carriers", None)
        if callable(forget_carriers):
            forget_carriers(task_id)
        return self._outcome(task_id)

    def close(
        self,
        task_id: str,
        *,
        closed_by: str = "user",
        reason: Optional[str] = None,
    ) -> DriveOutcome:
        """Close / archive a conversation by writing ``ConversationClosed``.

        Issue 08. An interactive
        conversation rests at a trailing next-goal ``suspended`` and is
        **never** forced to ``TaskCompleted`` from the control plane (that
        would manufacture a terminal not produced by any policy ``Decision``).
        "Closed" is a lifecycle dimension **orthogonal** to
        ``task.status``: this writes a *new* L0 control event (absent from any
        historical recording → byte-safe, exactly like ``ModelBound`` /
        ``TaskCancelled``), folded into ``GovernanceState.closed`` so the
        sessions-list / inspect hot path can query it **by fold, never from an
        Observer**. ``task.status`` stays ``suspended`` — the conversation is
        not terminated, only marked closed.

        Writer is the Engine (:meth:`Engine.note_conversation_closed`). The
        close is **advisory, not a lock**: a later :meth:`send_goal` on this
        same closed+suspended Task reopens it and continues as normal. Refuses
        a task already terminal (a cancelled / failed conversation is not a
        thing to "close").
        """
        host = self._host
        task = fold(host.event_log, host.content_store, task_id)
        if task.status == "terminal":
            raise TaskAlreadyTerminalError(task_id=task_id, verb="close")
        engine = host.resolve_engine(task)
        engine.note_conversation_closed(
            task, closed_by=closed_by, reason=reason
        )
        # Human stop: close must also HALT an in-flight turn (not only archive
        # the conversation). Mark the cancel registry AFTER the durable
        # ConversationClosed is written, so the top-level ReAct loop — polling
        # the registry at its next turn boundary — abandons the in-flight result
        # and lands on the next-goal suspend (reopenable; see
        # ``run_leased_task`` → ``_settle_stopped_turn``). Re-folding there sees
        # ConversationClosed but NOT terminal ⇒ the resumable path. The registry
        # is an in-process accelerator (never resumed); hosts without the seam
        # (test doubles) skip it. Mirrors ``cancel``'s mark, minus the terminal.
        request = getattr(host, "request_cancellation", None)
        if callable(request):
            request(task_id)
        # Session-CLOSE cascade: a closed conversation
        # must not leave its long-running ``shell_run(background)`` processes
        # orphaned. Reuses issue 03's per-session kill primitive via the SAME
        # host seam ``cancel`` uses (SIGTERM→SIGKILL per job; the watchers reap +
        # record ``BackgroundShellKilled`` on the session-root stream).
        # ``getattr`` so a host without background execution (test doubles) is a
        # clean no-op. ``task_id`` here is the session root the jobs are keyed by.
        kill_bg = getattr(host, "kill_background_session", None)
        if callable(kill_bg):
            kill_bg(task_id)
        # background sub-agent cascade (mirrors ``cancel``): free the registry's
        # per-session tracking; the cancel mark above aborts the in-flight drives.
        forget_bg = getattr(host, "forget_background_subagents", None)
        if callable(forget_bg):
            forget_bg(task_id)
        # Mirror ``cancel``: free the per-turn carriers. A later ``reopen`` +
        # ``send_goal`` re-notes them for its new turn, so this is safe.
        forget_carriers = getattr(host, "forget_turn_carriers", None)
        if callable(forget_carriers):
            forget_carriers(task_id)
        return self._outcome(task_id)

    def reopen(
        self,
        task_id: str,
        *,
        reopened_by: str = "user",
        reason: Optional[str] = None,
    ) -> DriveOutcome:
        """Explicitly reopen a closed conversation (audit-symmetric, issue 08).

        Reopen is **advisory**: a new goal on a closed+suspended Task already
        reopens it implicitly (CW2 — :meth:`send_goal` emits the same event),
        so this method exists only to record the reopen in the lifecycle audit
        when a surface wants an explicit "reopen" action distinct from sending
        the next goal. Writes ``ConversationReopened`` (writer Engine), folding
        ``GovernanceState.closed = False`` without touching ``task.status``.

        **Idempotent** (CW2): reopening a conversation that is not currently
        closed is a no-op — no event is written, so calling ``reopen`` twice (or
        on a never-closed conversation) cannot stack spurious audit entries.
        Refuses a terminal task.
        """
        host = self._host
        task = fold(host.event_log, host.content_store, task_id)
        if task.status == "terminal":
            raise TaskAlreadyTerminalError(task_id=task_id, verb="reopen")
        if not task.governance.closed:
            # Already open — nothing to reopen, write no audit event.
            return self._outcome(task_id)
        engine = host.resolve_engine(task)
        engine.note_conversation_reopened(
            task, reopened_by=reopened_by, reason=reason
        )
        return self._outcome(task_id)

    def rewind(self, task_id: str, *, message_seq: int) -> DriveOutcome:
        """Rewind the conversation to BEFORE the user message at ``message_seq``.

        D9 (issue 01 — conversation half only). ``message_seq`` is
        the seq of the rewound user-goal ``MessagesAppended`` event (the bubble
        the user clicked "undo" on). The conversation is re-based to where it
        rested just BEFORE that turn opened: this message, the AI output it
        triggered, and every later turn all become dead history.

        Mechanics: we compute ``keep_through`` = the seq right before the turn
        opener (the ``TaskWoken`` / ``TaskStarted`` that consumed this goal), fold
        the state THROUGH there, serialise that 4-slice body into the ContentStore,
        and **append** a ``TaskRewound{target_seq=keep_through, state_ref}`` marker
        — nothing on the stream is deleted or rewritten (append-only).
        fold then treats that marker exactly like a snapshot baseline
        (``find_latest_snapshot`` returns it), so the dead tail is never resumed
        on the accelerated path and is re-based away on the from-scratch path. The
        marker is written via ``system_emit`` (a control-plane write, no lease /
        no ``state_patch``), exactly like ``cancel`` — it never races the Engine's
        single ``RuntimeState`` writer. The resulting baseline is the prior
        ``next-goal`` suspend, so the conversation is immediately live again (a
        following ``send_goal`` drives a fresh turn from here).

        Rejects a ``message_seq`` that is not a real user-message event on this
        stream so a bad request never writes a marker pointing at nothing.
        """
        host = self._host
        events = host.event_log.read(task_id)
        if not events:
            raise RuntimeError(f"cannot rewind task {task_id!r}: no events")
        keep_through = self._rewind_keep_through(task_id, events, message_seq)
        # Fold the state as it stood THROUGH ``keep_through`` by folding a view of
        # the stream truncated at that seq (fold's own snapshot/rewound
        # acceleration still applies within the truncated window).
        bounded = _BoundedEventLog(events, keep_through)
        baseline = fold(bounded, host.content_store, task_id)
        state_ref = host.content_store.put(
            serialize_task_state(baseline), media_type=snapshot_media_type()
        )
        host.event_log.system_emit(
            task_id=task_id,
            type="TaskRewound",
            payload=TaskRewoundPayload(
                target_seq=keep_through, state_ref=state_ref
            ),
            actor="interaction-driver",
            origin="system",
            trace_id=self._trace_id(task_id),
        )
        self._restore_dispatcher_to_baseline(task_id, baseline)
        # The file half: write the dead tail's
        # baselines back to disk (live-only side-effect; never on resume).
        self._restore_files(host, events, keep_through, baseline)
        return self._outcome(task_id)

    def _restore_dispatcher_to_baseline(self, task_id: str, baseline: Task) -> bool:
        """Re-align dispatcher state after a live ``TaskRewound``.

        The EventLog marker re-bases fold to ``baseline``. Dispatcher state is
        only a lease/wake accelerator, so it must be reset to the same lifecycle
        boundary; otherwise a rewind that first cancelled an in-flight turn can
        leave the dispatcher terminal while fold says the task is suspended and
        resumable.
        """
        restore = getattr(self._host.dispatcher, "restore_task", None)
        if not callable(restore):
            return False
        status = baseline.status
        if status == "running":
            status = "ready"
        if status not in {"ready", "suspended", "terminal"}:
            return False
        restore(
            task_id,
            status=status,
            wake_on=baseline.wake_on,
            suspend_reason="rewound" if status == "suspended" else None,
        )
        return True

    @staticmethod
    def _rewind_keep_through(
        task_id: str, events: list[EventEnvelope], message_seq: int
    ) -> int:
        """The seq to fold-through for a rewind of the message at ``message_seq``.

        ``message_seq`` must be a user-goal ``MessagesAppended`` (the rewind
        anchor lives on a user bubble, D9). The keep boundary is the seq right
        before that turn's opener (the ``TaskWoken`` for a follow-up turn, the
        ``TaskStarted`` for the opening turn), which is the prior next-goal
        suspend (or, for the first turn, the pre-loop header). Folding through it
        lands the conversation back at a clean turn boundary."""
        by_seq = {e.seq: e for e in events}
        target = by_seq.get(message_seq)
        if target is None or target.type != "MessagesAppended" or not getattr(
            target.payload, "count", 0
        ):
            raise RuntimeError(
                f"cannot rewind task {task_id!r}: seq {message_seq} is not a "
                f"user message on this stream"
            )
        # Walk back to the turn opener that consumed this goal.
        opener_seq = None
        for env in reversed([e for e in events if e.seq < message_seq]):
            if env.type in ("TaskWoken", "TaskStarted"):
                opener_seq = env.seq
                break
        # Keep everything before the opener; an opening turn with no prior
        # boundary falls back to the genesis seq (re-base to an empty task).
        if opener_seq is None:
            return events[0].seq
        return opener_seq - 1

    @staticmethod
    def _restore_files(
        host: Any,
        events: list[EventEnvelope],
        keep_through: int,
        baseline_task: Any,
    ) -> None:
        """The file half of a rewind (incl. subtask cascade).

        Walk the dead tail (seq > ``keep_through``) on the parent stream AND,
        recursively, every descendant subtask stream it spawned inside that span
        (D8: subtasks share the parent's ONE workspace — so their
        edits hit the same disk and must be undone together). For each workspace path take
        its EARLIEST baseline — the first edit that pinned the file's pre-turn
        state. The shared per-turn gate (D8) stashes at most ONE baseline per path
        across the whole tree, so the union is clean. Write each back to disk —
        or, for a sandbox session (T7), back into the CONTAINER through the
        session's ExecEnv; a baseline with no ``content_ref`` means the AI created
        the file inside the rewound span, so it is DELETED. Only AI
        ``edit``/``write`` touches are covered (D4): a path the shell mutated
        never surfaced ``file_changes`` so it has no baseline here.

        Reuses the parent↔child graph (``SubtaskSpawned.subtask_id`` +
        ``TaskCreated.parent_task_id``), NOT cancel-cascade's
        ``_abort_cancelled_drain`` (which only scans in-flight, undriven children;
        rewind targets already-terminal descendants). Live-only — ``rewind`` runs
        on the real session, never on a resume (which reads recorded results
        and never calls this), so the resume-never-touches-disk invariant
        holds.
        """
        earliest: dict[str, Any] = {}
        visited: set[str] = set()

        def _collect(stream: list[EventEnvelope], *, after: int) -> None:
            # ``after``: only seq > this count is in the rewound span — the parent
            # stream is walked from ``keep_through``. A subtask's WHOLE stream
            # lives inside the rewound turn (v1 runs subtasks sequentially and the
            # parent waits until the whole delegation tree is terminal before
            # continuing, D8), so children are walked with ``after=-1`` (every event).
            for env in stream:
                if env.seq <= after:
                    continue
                if env.type == "ToolResultRecorded":
                    for baseline in (
                        getattr(env.payload, "file_baselines", None) or ()
                    ):
                        # seq-ordered + one baseline-per-path-per-tree (shared
                        # gate) → setdefault keeps that single turn-start pin.
                        earliest.setdefault(baseline.path, baseline)
                elif env.type == "SubtaskSpawned":
                    child_id = getattr(env.payload, "subtask_id", None)
                    if child_id and str(child_id) not in visited:
                        visited.add(str(child_id))
                        _collect(host.event_log.read(str(child_id)), after=-1)

        _collect(events, after=keep_through)
        if not earliest:
            return
        # T7 — a SANDBOX session's baselines live in the container, so the
        # write-back must go through the session's ExecEnv (the recorded
        # ``exec_env_ref``, T6) rooted at the container workdir, NOT the host FS.
        # ``exec_env_for_ref`` returns ``None`` for a local session (or a host
        # without a sandbox / a test double), so the local path below stays
        # byte-identical. This is live-only, exactly like the local path.
        resolve = getattr(host, "exec_env_for_ref", None)
        sandbox = (
            resolve(getattr(baseline_task.governance, "exec_env_ref", None))
            if callable(resolve)
            else None
        )
        if sandbox is not None:
            exec_env, root = sandbox
            for path, baseline in earliest.items():
                target = root / path
                if baseline.content_ref is None:
                    if exec_env.exists(target):
                        exec_env.unlink(target)
                else:
                    exec_env.mkdir(target.parent)
                    exec_env.write_bytes(
                        target, host.content_store.get(baseline.content_ref)
                    )
            return
        root = host.workspace_dir_for(baseline_task.governance.workspace)
        for path, baseline in earliest.items():
            target = root / path
            if baseline.content_ref is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(host.content_store.get(baseline.content_ref))

    # -- internals --------------------------------------------------------

    def seed_approve(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "driver",
    ) -> SeededTurn:
        """Validate + seed an approve-and-resume turn (async transport half)."""
        return self._seed_resolve_approval(
            task_id, call_id=call_id, approved=True, reason=reason,
            resolver=resolver,
        )

    def seed_deny(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "driver",
    ) -> SeededTurn:
        """Validate + seed a deny-and-resume turn (async transport half)."""
        return self._seed_resolve_approval(
            task_id, call_id=call_id, approved=False, reason=reason,
            resolver=resolver,
        )

    def _resolve_approval(
        self,
        task_id: str,
        *,
        call_id: str,
        approved: bool,
        reason: Optional[str],
        resolver: str,
    ) -> DriveOutcome:
        return self.drive_seeded(
            self._seed_resolve_approval(
                task_id, call_id=call_id, approved=approved, reason=reason,
                resolver=resolver,
            )
        )

    def _seed_resolve_approval(
        self,
        task_id: str,
        *,
        call_id: str,
        approved: bool,
        reason: Optional[str],
        resolver: str,
    ) -> SeededTurn:
        handle = f"approval-{call_id}"
        self._require_human_suspend(task_id, handle)
        return self._seed_woken(
            task_id,
            handle=handle,
            prelude=ResolveApprovalPrelude(
                call_id=call_id,
                approved=approved,
                reason=reason,
                resolver=resolver,
            ),
        )

    def _drive_woken(
        self, task_id: str, *, handle: str, prelude: WokenPrelude
    ) -> DriveOutcome:
        """Wake + lease + drive — the one-call woken seam (seed-then-drive).

        Identical machine to ``CodeSessionRunner._drive_woken_command`` — the
        woken-command-prelude seam (issue 01) so the H2 ``consumed_wake_event``
        release discipline and the ``note_woken → prelude → run_one_step``
        ordering are NOT re-inlined per surface. The split halves are
        :meth:`_seed_woken` (wake + lease) and :meth:`drive_seeded`
        (``run_leased_task`` + drain); the async transport runs the latter off
        the request thread.
        """
        return self.drive_seeded(
            self._seed_woken(task_id, handle=handle, prelude=prelude)
        )

    def _seed_woken(
        self,
        task_id: str,
        *,
        handle: str,
        prelude: Optional[WokenPrelude],
        condition: Optional[WakeCondition] = None,
    ) -> SeededTurn:
        """Wake the task on ``handle`` and take the matched lease — no drive.

        The synchronous half of the woken seam: consumes the matched wake into
        a targeted lease (H2 ``consumed_wake_event`` discipline) and packages
        it with the per-command prelude for :meth:`drive_seeded` to run.

        ``condition`` is the wake event to deliver; ``None`` (every human
        command) means ``HumanResponseReceived(handle)``. ``deliver_event``
        passes an :class:`ExternalEvent` instead — the machine is otherwise
        identical, so the H2 consume discipline is never re-inlined per wake
        variant.

        D6 (docs/adr/step-attempt-recovery.md): an append-type prelude
        (``durable_at_seed``) is applied HERE, synchronously, right after
        the lease — ``note_woken`` + the prelude's events are durable before
        the command is acked, so an acked ``send_goal`` / ``answer`` can
        never lose the user's input to a crash. The returned
        :class:`SeededTurn` then carries ``prelude=None`` and the drive runs
        the bare step (worker case 2′). The approval prelude (executes the
        approved tool) keeps the old drive-side path.
        """
        host = self._host
        expected: Optional[str] = None
        if condition is None:
            condition = HumanResponseReceived(handle=handle)
        else:
            expected = repr(condition)
        host.dispatcher.wake(task_id, condition)
        lease = host.dispatcher.lease(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            task_id=task_id,
        )
        if lease is None or lease.wake_event is None:
            # Dispatcher out of sync with the folded truth (e.g. a fresh
            # dispatcher over an existing log): restore its rows from the
            # fold and retry ONCE. The retried lease then falls through to
            # the same seed tail below — the D6 durable-prelude application
            # must not depend on which acquisition path produced the lease.
            task = fold(host.event_log, host.content_store, task_id)
            if self._restore_dispatcher_to_baseline(task_id, task):
                host.dispatcher.wake(task_id, condition)
                lease = host.dispatcher.lease(
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                    task_id=task_id,
                )
            if lease is None or lease.wake_event is None:
                task_status = getattr(host.dispatcher, "task_status", None)
                dispatcher_status = (
                    task_status(task_id) if callable(task_status) else None
                )
                raise NotResumableError(
                    task_id=task_id,
                    handle=handle,
                    status=task.status,
                    wake_on=getattr(task, "wake_on", None),
                    dispatcher_status=dispatcher_status,
                    expected=expected,
                )
        if prelude is not None and getattr(prelude, "durable_at_seed", False):
            engine = self._apply_seed_prelude(task_id, lease, prelude)
            return SeededTurn(
                task_id=task_id, lease=lease, prelude=None, engine=engine
            )
        return SeededTurn(task_id=task_id, lease=lease, prelude=prelude)

    def _apply_seed_prelude(
        self, task_id: str, lease: Any, prelude: WokenPrelude
    ) -> Any:
        """Apply an append-type prelude synchronously under the seed's lease
        (D6): ``note_woken`` + the prelude's durable events land before the
        202 ack, in exactly the order (and bytes) the drive-side path
        recorded them. Returns the Engine resolved from the PRE-prelude fold
        so the drive runs this turn on the binding in force when it started
        (a prelude-written ``ModelBound`` drives the next turn, as before);
        the drive itself is then prelude-less — its woken machine reconciles
        the already-durable ``TaskWoken`` and runs the bare step (case 2′).
        """
        host = self._host
        task = fold(host.event_log, host.content_store, task_id)
        engine = host.resolve_engine(task)
        task = engine.note_woken(
            task, lease_id=lease.lease_id, wake_event=lease.wake_event
        )
        try:
            prelude(engine, task, lease_id=lease.lease_id)
        except Exception:
            # ``TaskWoken`` is already durable; propagating with the turn
            # half-seeded would strand a ``running`` window the worker later
            # re-drives WITHOUT the command's input (a bare case 2′), and the
            # client's retry would find the task not suspended. Compensate:
            # re-suspend on the wake's own handle and release the lease, so
            # the stream reads woken→suspended and retrying the SAME command
            # (e.g. after a ``PayloadTooLarge``) works. Best-effort — if the
            # log itself is down these writes fail too and the original
            # error is still the one to surface.
            try:
                wake = lease.wake_event
                if isinstance(wake, HumanResponseReceived):
                    task = suspend_on_human_handle(
                        engine, task,
                        handle=wake.handle, lease_id=lease.lease_id,
                    )
                    host.dispatcher.release(
                        lease.lease_id,
                        next_state="suspended",
                        wake_on=task.wake_on,
                        consumed_wake_event=wake,
                    )
            except Exception:  # noqa: BLE001 — surface the original failure
                _log.exception(
                    "seed-prelude compensation failed for task %s", task_id
                )
            raise
        return engine

    def _require_human_suspend(self, task_id: str, handle: str) -> Task:
        """Refuse a command whose task is not suspended on ``handle``; return
        the folded task on success.

        Mirrors ``CodeSessionRunner.resume_with_goal`` /
        ``resolve_tool_approval`` guards: appending a goal / resolving an
        approval must not silently consume an unrelated wake condition. The
        folded task is returned so a caller (``send_goal``) can read the
        lifecycle state (CW2: is this conversation closed?) without re-folding.
        """
        host = self._host
        task = fold(host.event_log, host.content_store, task_id)
        wake_on = getattr(task, "wake_on", None)
        if (
            task.status != "suspended"
            or not isinstance(wake_on, HumanResponseReceived)
            or wake_on.handle != handle
        ):
            task_status = getattr(host.dispatcher, "task_status", None)
            dispatcher_status = (
                task_status(task_id) if callable(task_status) else None
            )
            raise NotResumableError(
                task_id=task_id,
                handle=handle,
                status=task.status,
                wake_on=wake_on,
                dispatcher_status=dispatcher_status,
            )
        return task

    def _require_external_suspend(self, task_id: str, event_kind: str) -> Task:
        """Refuse a delivery whose task is not suspended on
        ``ExternalEvent(event_kind)``; return the folded task on success.

        The :meth:`_require_human_suspend` mirror for the ``deliver_event``
        verb: a delivery must not consume an unrelated wake condition, so a
        mismatched ``event_kind`` / a task not waiting / a terminal task all
        raise the same typed :class:`NotResumableError` (code
        ``not_resumable``) the human commands do.
        """
        host = self._host
        task = fold(host.event_log, host.content_store, task_id)
        wake_on = getattr(task, "wake_on", None)
        if (
            task.status != "suspended"
            or not isinstance(wake_on, ExternalEvent)
            or wake_on.event_kind != event_kind
        ):
            task_status = getattr(host.dispatcher, "task_status", None)
            dispatcher_status = (
                task_status(task_id) if callable(task_status) else None
            )
            raise NotResumableError(
                task_id=task_id,
                handle=event_kind,
                status=task.status,
                wake_on=wake_on,
                dispatcher_status=dispatcher_status,
                expected=f"ExternalEvent(event_kind={event_kind!r})",
            )
        return task

    def _authorize_selector(self, selector: Optional[str]) -> str:
        """Validate a model selector and return the model id to bind.

        Issue 06: the selector is permitted iff it is in
        ``principal.allowed_models ∩ deployment-allowlist``. The CLI's ⊤
        principal permits any selector (still gated by the allowlist); a web
        principal's ``allowed_models`` is the session's explicit set. A
        rejected selector raises :class:`ModelSelectorError` *before* any
        durable write, so no ``ModelBound`` / Task / turn is produced.

        ``None`` (no selector — the CLI's default, or a web request that
        omits ``model``) binds the host-fixed :attr:`_default_model`: that is
        the deployment's own choice, not caller input, so it is not subject
        to the selector check. Returns the concrete model id to record in
        ``ModelBound`` (and to key the resolver Engine on).

        D-C3: the allowlist check runs on the *alias* (the selector the
        caller / principal speaks), then the alias is resolved to its real
        model-id via the sdk catalog just before binding. So ModelBound,
        ``resolver._bound_model_for``, ``req.model``, and the pricing key all
        carry the real id and never drift; a rejected selector still raises
        with the original alias before any resolution or durable write. A
        non-alias selector (already a real id, or the test-only ``stub-model``)
        passes through :func:`resolve_alias` unchanged.
        """
        if selector is None:
            return self._default_model
        allowed = self._authorized_models()
        if selector not in allowed:
            raise ModelSelectorError(
                selector=selector, allowed=sorted(allowed)
            )
        return resolve_alias(selector)

    def _authorized_models(self) -> frozenset[str]:
        """The selectors this driver may bind = principal ∩ allowlist.

        A ⊤ principal (``allows_any``) contributes no upper bound, so the
        intersection collapses to the deployment allowlist; otherwise it is
        the literal set intersection.
        """
        if self._principal.allows_any:
            return self._model_allowlist
        return self._principal.allowed_models & self._model_allowlist

    def _authorize_pair(
        self,
        provider_selector: Optional[str],
        model_selector: Optional[str],
        bound_model: str,
    ) -> Optional[str]:
        """Validate a ``(provider, model)`` selector pair.

        Returns the bound provider **name** to fold into the ``ModelBound``, or
        ``None`` when no provider was selected (provider sticks at the host
        default / current binding — byte-equal with the pre-I4 single-provider
        path). When a provider IS named the pair is legal iff:

          1. the provider name is configured on the host's registry, AND
          2. the model that will actually be bound after this turn belongs to
             that provider. This is always checked — including provider-only
             switches (no ``model_selector``) where ``bound_model`` (the
             already-resolved id of the current sticky binding) is tested
             instead. This closes three gaps in the original design:

             (a) ``seed_start`` supplied a provider but no model — the host
                 default would be bound to a provider that never declared it.
             (b) ``seed_send_goal`` switched only the model; the session had
                 never been bound to a provider — model was validated in
                 isolation without a provider constraint.
             (c) A provider-only switch carried a ``bound_model`` that was
                 validated against the *old* provider, not the new one.

        The ``provider_models`` table stores **selectors** (alias vocabulary).
        When ``model_selector`` is given it is tested directly against that
        vocabulary.  When only ``bound_model`` is available it is a
        resolved-alias id, so we expand ``declared`` with resolved ids too
        (``resolve_alias`` is idempotent on real ids) to bridge the two
        vocabularies.  Either failure raises :class:`ProviderSelectorError`
        *before* any durable write. The host exposes its registry via the
        (optional, host-tolerant) ``provider_models`` table — a single-provider
        host / fake with an empty (or absent) table never reaches a provider
        selector (the transport only sends one when ``providers`` was
        advertised), so this guards the seam without perturbing the no-provider
        path.
        """
        if not provider_selector:
            return None
        provider_models = getattr(self._host, "provider_models", {}) or {}
        if provider_selector not in provider_models:
            raise ProviderSelectorError(
                provider=provider_selector,
                model=model_selector or bound_model,
                available=sorted(provider_models),
            )
        declared = tuple(provider_models[provider_selector])
        # Validate the model that will actually be bound this turn:
        # - explicit selector → test against the declared selector vocabulary
        # - no selector      → test bound_model (resolved id) against both
        #   the selector vocabulary AND its resolved-id expansions, bridging
        #   the alias/id vocabulary gap.
        candidates = set(declared) | {resolve_alias(m) for m in declared}
        effective = model_selector if model_selector is not None else bound_model
        if effective not in candidates:
            raise ProviderSelectorError(
                provider=provider_selector,
                model=effective,
                available=sorted(declared),
            )
        return provider_selector

    def _drain_pending_subtasks(self, task_id: str) -> None:
        """S3b — drive any pending sub-agent delegation tree synchronously,
        in-request, after a driven command.

        A parent turn that ended on a delegation wake
        (``SubtaskCompleted`` / ``SubtaskGroupCompleted``) is driven to its
        resumed terminal here, so ``start`` / ``send_goal`` / ``approve`` /
        ``deny`` / ``answer`` all settle a delegation tree before returning the
        :class:`DriveOutcome` (no WorkerLoop — the SR1 drain runs on the driver).

        Host-tolerant: a resolver without ``drive_pending_subtasks`` (e.g. the
        single-agent / lifecycle resolver) is a NO-OP, and a turn that did NOT
        delegate (the common interactive path — a trailing next-goal suspend or
        a terminal) leaves the parent untouched. So this never perturbs the
        non-delegating common path.
        """
        task = fold(self._host.event_log, self._host.content_store, task_id)
        if not (
            getattr(task, "status", None) == "suspended"
            and isinstance(
                getattr(task, "wake_on", None),
                (SubtaskCompleted, SubtaskGroupCompleted),
            )
        ):
            return
        drain = getattr(self._host, "drive_pending_subtasks", None)
        if drain is not None:
            drain(task)

    def _resume_woken_ancestors(self, task_id: str) -> None:
        """Walk up the parent chain and resume any delegation-suspended
        ancestor whose wake the :class:`ChildLifecycleObserver` delivered
        out-of-band.

        The stranded-parent case: a delegated child suspended for approval
        mid-drain (``UnsupportedSubtaskSuspend``), the user later approved /
        denied / answered the CHILD (its own command turn — ``task_id`` here
        is the child), the child reached terminal, and the observer woke the
        parent — but no drain was in flight to consume that wake. Each
        ancestor that resumes all the way to terminal wakes ITS parent, so
        the walk continues until an ancestor stays suspended (its own next
        turn / another pending member) or the chain tops out.

        Host-tolerant: a host without ``resume_woken_parent`` (the
        single-agent / lifecycle resolver, test doubles) is a no-op. A
        deeper descendant hitting its own approval suspend mid-resume
        (``UnsupportedSubtaskSuspend``) leaves the tree durably consistent —
        the next resolution re-enters here.
        """
        host = self._host
        resume = getattr(host, "resume_woken_parent", None)
        if resume is None:
            return
        current = task_id
        while True:
            parent_id = self._parent_task_id(current)
            if not parent_id:
                return
            parent = fold(host.event_log, host.content_store, parent_id)
            try:
                settled = resume(parent)
            except UnsupportedSubtaskSuspend:
                return
            if settled is None or getattr(settled, "status", None) != "terminal":
                return
            current = parent_id

    def _parent_task_id(self, task_id: str) -> Optional[str]:
        """``parent_task_id`` off the genesis ``TaskCreated``, or ``None``
        for a root task / an empty stream."""
        events = self._host.event_log.read(task_id)
        if not events:
            return None
        return getattr(events[0].payload, "parent_task_id", None)

    def _outcome(self, task_id: str) -> DriveOutcome:
        host = self._host
        task = fold(host.event_log, host.content_store, task_id)
        # Per-session sandbox teardown (D4): when a ROOT task reaches a terminal,
        # its whole tree is done, so release the session's container. Guarded to
        # the root (``parent_task_id is None``) so a completed SUBTASK never reaps
        # the parent's shared container, and to ``terminal`` so an interactive
        # root resting at ``suspended`` keeps its container (it is reaped by the
        # shutdown backstop / a later close). Idempotent + ``getattr``-guarded, so
        # the local / non-sandbox path and hosts without the seam are no-ops.
        if (
            task.status == "terminal"
            and getattr(task, "parent_task_id", None) is None
        ):
            release = getattr(host, "release_exec_env", None)
            if callable(release):
                release(task_id)
        wake_on = getattr(task, "wake_on", None)
        handle = (
            wake_on.handle
            if isinstance(wake_on, HumanResponseReceived)
            else None
        )
        return DriveOutcome(
            task_id=task_id, status=task.status, wake_handle=handle
        )

    def _reset_file_checkpoint_turn(self, root_task_id: str) -> None:
        """Clear the per-turn rewind-baseline gate at a top-level
        turn boundary (a new user goal), so the next turn re-stashes a fresh
        baseline for any file it touches — what lets a rewind restore to ANY turn
        boundary ("cleared every turn"). Only the user-goal openers (``start`` /
        ``send_goal``) reset; an approval / answer / background-notice resume
        continues the SAME turn and must NOT clear its already-stashed baselines.
        Guarded with ``getattr`` so a host Protocol impl / test double without the
        seam is a clean no-op; never touched on resume (it is a live-only runtime
        accelerator, mirrored from the registry it resets)."""
        reset = getattr(self._host, "reset_file_checkpoint_turn", None)
        if callable(reset):
            reset(root_task_id)

    def _trace_id(self, task_id: str) -> str:
        events = self._host.event_log.read(task_id)
        if events:
            return events[0].trace_id
        return "trace-unknown"
