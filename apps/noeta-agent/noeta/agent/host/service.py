"""AgentService: the host and concurrency boundary of the noeta Client.

Threading model (see the implementation notes on the concurrency model):
- One dedicated worker thread serially executes every **drive seed**
  (start/send_goal/answer's seed_* + _yield_seeded_lease), aligned with
  noeta's single-session lease serialization; the HTTP side bridges through a
  job queue + Future. A seed is a lightweight operation that only writes
  metadata + a lease (no LLM runs), so serializing it is harmless; the actual
  turn driving runs on the noeta Client's resident WorkerLoop pool
  (agent_num_workers threads, SDK 0.1.10) — turns of the same session stay
  serialized by the dispatcher lease, different sessions advance concurrently.
- **Read-only** retrieval (replay / raw_events / get_content / file listing)
  does not go through that serial queue; it reads directly via the anyio
  thread pool: _submit is a global single-thread serial queue, and while seed
  jobs are short (metadata + lease only), replay is read-only and frequent
  (every reconnect hits it) — queueing it behind every session's seed jobs
  only adds head-of-line latency, no benefit. noeta's sqlite reads carry their
  own lock + check_same_thread=False, so cross-thread reads are safe;
  session.task_id is persisted at the first root TaskCreated during
  seed_start, so concurrent direct reads have no race.
- The noeta sqlite connections use check_same_thread=False and the components
  carry their own locks, so **cancel is called directly from the request
  thread** (the official design: cancel on one thread, drive on another, the
  registry locks, the engine polls at step boundaries) — verified: cancel
  takes effect between tool steps and cannot interrupt a single in-flight LLM
  call.
- Live events: client.subscribe callbacks (fired on **the thread that emits**
  — noeta worker-pool threads, the jobs-worker thread, or the request thread)
  → translate → loop.call_soon_threadsafe into each session's asyncio
  subscription queues. With multiple workers _on_envelope fires on multiple
  threads, but the binding invariants ①–④ (see _on_envelope) were written for
  multithreading from the start; raising the concurrency does not change the
  model.
- Token streaming (SDK 0.1.7+, the token-streaming-projection design): when
  the provider implements StreamingProvider, each LLM call's token deltas go
  through HostConfig.delta_sink → _on_delta to the session (synthetic frames
  with no seq, type="delta"). A delta is a transient projection — it never
  hits the EventLog, is not replayed, and is not backfilled after a
  disconnect; the durable truth remains the subsequent MessagesAppended.
  Replay reads the EventLog and naturally contains none; the SSE endpoint
  sends no id for seq=None frames, skips dedup, and yields them directly.
  Only root task deltas are forwarded (subtask streaming is left for later
  work). The mock (FakeLLMProvider) does not implement StreamingProvider and
  the runtime seam skips streaming, so the mock path has no deltas and its
  behavior is unchanged.

Session ↔ task: one session = one noeta root task. The first message calls
seed_start(workspace_dir=…) to bind the session workspace (TaskCreated is
emitted synchronously during the seed); task_id is known before seed_start
returns (seeded.task_id), and the _pending_session slot binds it on the first
root TaskCreated (the jobs-worker serialization guarantees uniqueness).

Restart crash recovery (SDK 0.1.10 step-attempt recovery + WorkerLoop
stale-lease reclaim): a root task that was mid-turn when the process crashed
has its lease reclaimed after restart by the WorkerLoop's requeue_stale and is
automatically re-driven (the abandon cap backstops crash loops). _init_client
rebuilds _task_to_session (root task_id → session) via list_all_with_task
**before** start_workers and subscribes first, so re-drive events route to the
session correctly; StepAttemptAbandoned is not in the translator /
_update_status vocabulary — a pure backstop that neither reaches the frontend
nor flips status. In-flight subtask events at crash time no longer route after
restart (_subtask_ids is memory-only), an accepted loss. Semantic effect: a
session the user last saw as running at crash time is auto-driven to
completion after restart (instead of staying stuck in running) — as intended,
the state machine stays consistent.

Delegation: client.subscribe covers all tasks (root + subtasks); a subtask's
TaskCreated carries parent_task_id — which maps the subtask task_id to the
parent session (memory-only, never persisted; the session table stores only
the root task_id, replay reads only the root stream, and in-flight subtask
events no longer route after a process restart — an accepted loss).
Cancelling the root cascades through noeta's cancellation registry to
background subtasks (each subtask writes its own TaskCancelled at a step
boundary); delete_task cascades storage deletion across the whole subtask
tree.

Continuing after cancel: send_goal raises NotResumableError for a cancelled
task (seed_send_goal's first step _require_human_suspend raises
synchronously) → degrade to seed_start of a new task on the same workspace
(files kept; event seq restarts from 0 — the old messages remain in frontend
memory but old turns no longer replay after a refresh; the risk was called
out when this was shipped).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from noeta.agent.host.providers import build_provider
from noeta.agent.host.title import generate_title
from noeta.agent.host.translator import UIEvent, is_waiting_subtask, translate
from noeta.agent.config import Settings
from noeta.agent.store.sessions import Session, SessionStore
from noeta.agent.store.skills import GLOBAL_SPACE_ID

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are noeta-agent, the data platform's event-tracking expert.

Stance: data correctness above everything else. Event names, parameter names, enum values, reporting timing, naming conventions and the like must come from an authoritative source (the data dictionary / the space knowledge base / the existing tracking catalog) and be traceable to it; when you cannot find something, say so plainly and point out where to search — never fabricate from memory or force an answer.

- Respond in the user's language.
- For key findings or key decisions, state the conclusion and its basis in one sentence; do not narrate every tool call.
- Finish with a one- or two-sentence summary of what was done.
"""

_SANDBOX_PROMPT = """\

Runtime environment (one dedicated container per session):
- You have the standard file and command tools: read / write / edit for files, shell_run for shell commands; every operation happens inside this session's container.
- The current working directory is the session workspace (/workspace inside the container); relative paths are based on it. Write deliverables here — the user sees them directly in the frontend file panel, no extra copying needed.
- For file search, run rg / find / fd through shell_run; more flexible than dedicated tools.
- This space's knowledge base lives in the session directory `knowledge/` (read-only) and is your first-hand authoritative source for verifying event names / parameters / conventions: start from `knowledge/<source name>/INDEX.md` to get oriented, and search with shell_run running rg / find.
- Read local files (including images) with the read tool; it returns images as image_block. For the static content of a single URL, running curl through shell_run is enough.
- Citing sources: when a fact in your answer comes from a file under knowledge/, mark it with a footnote — append
  [^1] at the end of the sentence, and list each source at the end of the answer as
  `[^1]: knowledge/<source name>/<file path>#<heading text>` (the anchor copies the exact heading of the cited passage's section; a whole-file citation may omit the #anchor). You must have fully read a file before citing it;
  paths must actually exist, never fabricated; content whose source you cannot pin down gets no footnote.
- If `AGENT.md` exists at the workspace root (this space's agent configuration), read it and follow its instructions before starting the task.
"""

# Delegation guidance: only appended to the sandbox prompt when the subagent
# switch is on (spawn_subagent present) — leaving it in while the switch is
# off would make the model call the nonexistent spawn_subagent / web and fail.
_SANDBOX_SUBAGENT_PROMPT = """\
- For broad searches across workspace code/files, batch the independent search targets into one spawn_subagent call to fan out explorers in parallel while you stay on the main line.
- For operating web pages (clicking, typing, multi-step browsing of http/https URLs), delegate to the web subagent and receive only its distilled conclusions.
"""

# Memory policy prompt: adapted from noeta presets/prompts/memory-policy.md.
# The SDK only splices the policy into its own preset's prompt; this app uses
# a custom system_prompt that bypasses presets, so it must splice it itself.
# Added product semantics: memory pools are isolated per space (personal
# space = private memory, team space = shared among members).
_MEMORY_PROMPT = """\

Memory: you have cross-session memory tools — memory_write / memory_read / memory_search / memory_archive;
when memories exist, their index is provided with the context. Memory persists across sessions and belongs to the current space (personal space = only this user,
team space = shared among space members). Record only what a future you would use.

Worth recording:
- The user's corrections of and feedback on how you work — so the next session does not repeat the same mistake.
- Cross-session project facts that cannot be derived from the code or its history: decisions settled in conversation, environment quirks, unwritten conventions.
- Procedural experience that took real effort to earn: commands verified to work, pitfalls hit, debugging insights.
- Pointers to hard-to-find external resources (docs, issues, dashboards).

Do not record:
- What the codebase or git history already records — code structure, file contents, commit messages.
- Details meaningful only to the current session: intermediate results, in-progress task state.
- Secrets: credentials, tokens, keys — never, in any form.

Hygiene:
- Check the memory index before writing (memory_search when unsure): memory_write overwrites on the same name;
  update the old memory instead of creating a near-duplicate new entry.
- Use stable kebab-case slugs for memory names; write a one-line description and type so the index stays scannable.
- Archive outdated or overturned memories with memory_archive; do not leave them around to mislead later sessions.
"""

_EXPLORER_PROMPT = """\
You are explorer, a read-only search subagent. Use shell_run to execute rg / find / fd
commands to locate code or files relevant to the goal in the workspace, then read the key
files with read, and finally output one concise search conclusion: list the key file paths,
the locations of the relevant snippets, and a one-sentence explanation. Make no modifications,
ask no extra questions, converge as fast as possible.
"""

# Browsing subagent prompt: adapted from noeta presets/prompts/web.md
# (observe→act loop + locating elements by index + returning only distilled
# conclusions), with webfetch swapped for shell_run curl from this app's tool
# surface.
_WEB_PROMPT = """\
You are web, the web-browsing subagent. The parent agent delegates web tasks to you; you
drive a real browser inside the session container, and once you have what the task needs you
return a distilled conclusion — never the raw page content.

Working loop (browse like a human):
1. browser_navigate opens the starting URL (the return value inlines the current page's
   numbered interactive elements, so you can act on them directly).
2. browser_extract reads the current page: it returns the page text + a numbered list of
   interactive elements (links/buttons/inputs); subsequent actions locate elements by index.
3. Act by index: browser_click clicks links/buttons; browser_type types into inputs (set
   submit: true when Enter should submit, e.g. search boxes).
4. Indexes go stale after the page changes: browser_extract again before continuing, until
   you have what the task needs.

Discipline:
- Extract before acting; never guess an index.
- Take the shortest path; leave useless pages, no wandering; quote the page verbatim when
  precise information is needed.
- browser_screenshot only when visual confirmation is needed (layout, whether the page
  rendered) — screenshots are stored for the user, not fed back to you; always read content
  with browser_extract.
- For the static content of a single URL with no interaction, shell_run curl is faster. You
  may use read / write to save conclusions or evidence in the workspace.
- Read local files with read; do not open local paths in the browser. When browser_* tools
  are unavailable, degrade to curl; when interactive browsing is impossible, say why plainly.

Return: when the task is done, output one self-contained, concise conclusion (key points +
source URLs); when it cannot be done, say why plainly. The parent agent cannot see the pages
you browsed — do not return raw HTML or full element lists.
"""


class SessionBusyError(Exception):
    """The session already has a turn driving / awaiting an answer; a
    concurrent start is not allowed."""


class _Job:
    __slots__ = ("fn", "future")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.future: concurrent.futures.Future = concurrent.futures.Future()


class AgentService:
    def __init__(self, settings: Settings, store: SessionStore) -> None:
        self._settings = settings
        self._store = store
        self._jobs: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Any = None
        self._provider_name = ""
        self._capabilities: dict[str, bool] = {}
        # session_id → set of subscription queues; accessed from both worker
        # and request threads.
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._subs_lock = threading.Lock()
        self._task_to_session: dict[str, str] = {}
        # Tasks that reached a terminal state: _update_status uses this to
        # ignore late events (see the ordering note there). Grows only,
        # following the lifecycle of _task_to_session; cleaned up together
        # when a session is deleted. The lock-free safety premise matches the
        # binding section: single set reads/writes are atomic under the GIL
        # and the terminal state is written once — reading a stale value at
        # worst lets one late event slip through, same as before the fix
        # (never worse).
        self._terminal_tasks: set[str] = set()
        # __consolidation__ curation task → explicit memory root
        # (run_consolidation's on_seeded registers it before the lease is
        # handed back, see _consolidation_pass). A curation task belongs to no
        # session; the resolver relies on this. Lost on process restart (an
        # in-flight curation re-driven by requeue lands in quarantine —
        # harmless, that one consolidation is just a no-op).
        self._task_memory_root: dict[str, Path] = {}
        # Feedback analysis runs: __feedback_analysis__ task → run context
        # (registered after seed, before the lease is handed back; tools
        # resolve it through ctx.metadata task_id). Memory-only, discarded on
        # process restart — leftover running runs are finalized by
        # FeedbackStore.reset_stale_running.
        self._feedback_runs: dict[str, Any] = {}
        self._feedback_store: Any = None
        # session_id → platform user: the identity seam for host-side
        # integrations that need the acting user of a session (multi-user
        # deployments). Accessed by the worker (binding) and emit threads
        # (resolve_context reads); single-key reads/writes are atomic under
        # the GIL, and a stale read is at most one beat late — same policy as
        # _task_to_session.
        self._session_to_user: dict[str, str] = {}
        # Set of subtask task_ids: routed to the parent session but never
        # changing session status; the translator uses the narrow subtask
        # vocabulary. Memory-only (discarded on restart, see module
        # docstring).
        self._subtask_ids: set[str] = set()
        # Subtask → owning root task: used to tag events (workflow per-tab SSE
        # filtering; subtask events belong to their root task's tab).
        # Memory-only, cleaned up with session deletion.
        self._subtask_root: dict[str, str] = {}
        self._pending_session: Optional[str] = None
        # Sessions whose title generation already failed once in this process:
        # consecutive failures are not retried endlessly (saves LLM calls);
        # with title_generated unset, a restart retries once — trade-off in
        # _maybe_generate_title.
        self._title_failed: set[str] = set()
        # In-flight title generation set: the LLM call takes seconds, and a
        # re-trigger during that window (TaskSuspended right behind
        # TaskCompleted, etc.) would start another thread and pay another
        # call; the placeholder blocks it before the thread starts.
        self._generating_titles: set[str] = set()
        # sandbox_enabled (noeta per-session containers): assembled in
        # _init_client. The file surface reads the host-side session directory
        # (workspaces/<session_id>, bind-mounted into the container at
        # /workspace).
        self._sandbox_enabled: bool = False
        # Sandbox provider reference (force_release containers by session id
        # when deleting a session).
        self._sandbox_provider: Any = None
        # Sandbox idle-reclaim reaper (daemon thread) + stop signal: started
        # in _init_client only when the sandbox is enabled; shutdown sets the
        # stop flag.
        self._sandbox_reaper_stop = threading.Event()
        self._sandbox_reaper_thread: Optional[threading.Thread] = None
        # Live sandbox preview (browser/terminal/code panels): gateway =
        # token registry + reverse proxy; the server runs on its own port
        # (origin isolation). Assembled only when the sandbox is enabled.
        self._preview_gateway: Any = None
        self._preview_server: Any = None
        # Channel service (ownership resolution and read surface for the
        # channel_read_* tools); attached by the lifespan. Tools reference it
        # through a zero-arg getter at registration in _init_client and fetch
        # it at invoke time — the attach only has to precede the first tool
        # call, not startup.
        self._channel_service: Any = None

    # ------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._worker_main, name="noeta-worker", daemon=True
        )
        self._thread.start()
        await self._submit(self._init_client)

    def attach_knowledge_store(self, store) -> None:
        """Attach the knowledge_store (used by _space_has_ready_knowledge to
        check source status)."""
        self._knowledge_store = store

    def attach_skill_store(self, store) -> None:
        """Attach the skill registry (workspace_for reads it during assembly
        to pick symlink targets: global builtins ∪ space-enabled); must be
        called before startup."""
        self._skill_store = store

    def attach_agent_config_store(self, store) -> None:
        """Attach the space agent configuration (workspace_for writes the
        AGENT.md persona during assembly + knowledge mounts filter by the
        selection); must be called before startup."""
        self._agent_config_store = store

    def attach_channel_service(self, service) -> None:
        """Attach the channel service (the read surface for the
        channel_read_history / channel_read_topic tools); must be attached
        before the first channel-topic tool call."""
        self._channel_service = service

    def session_id_for_task(self, task_id: str) -> Optional[str]:
        """root/sub task → owning session (in-memory mapping, rebuilt after
        restart). Used by the board tools' space-ownership resolution
        (task → session → space)."""
        return self._task_to_session.get(task_id)

    def attach_board_store(self, store) -> None:
        """Attach the board store (the board_* tool surface); must precede the
        first tool call."""
        self._board_store = store

    def _space_id_for_task(self, task_id: str) -> Optional[str]:
        """task → team space id (board tool ownership; personal spaces have no
        board → None)."""
        sid = self._task_to_session.get(task_id)
        session = self._store.get(sid) if sid else None
        if session is None or not session.space_id:
            return None
        spaces = getattr(self, "_space_store", None)
        if spaces is not None:
            space = spaces.get_space(session.space_id)
            if space is None or space.get("is_personal"):
                return None
        return session.space_id

    def attach_space_store(self, store) -> None:
        """Attach the space store (board tools exclude personal spaces); must
        precede the first tool call."""
        self._space_store = store

    def attach_feedback_store(self, store) -> None:
        """Attach the feedback store (persisting and finalizing analysis-run
        suggestions); must be called before startup."""
        self._feedback_store = store

    async def shutdown(self) -> None:
        # Stop the sandbox idle-reclaim reaper first (daemon thread; setting
        # the flag is enough, no waiting).
        self._sandbox_reaper_stop.set()
        # Stop the preview server first (daemon thread serve_forever; shutdown
        # blocks until it exits). Running WS pump threads are daemons too and
        # die with the process; no per-thread waits.
        if self._preview_server is not None:
            try:
                self._preview_server.shutdown()
                self._preview_server.server_close()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                logger.debug("preview server shutdown failed", exc_info=True)
        # client.shutdown() already calls stop_workers internally (SDK 0.1.10
        # client.py) — no extra explicit call, simplicity first. Still goes
        # through the serial queue to stay ordered with in-flight seed jobs.
        if self._client is not None:
            try:
                await self._submit(self._client.shutdown)
            except Exception:  # noqa: BLE001 - best-effort shutdown
                logger.exception("client shutdown failed")
        self._jobs.put(None)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _worker_main(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            # The waiter may disconnect midway (request cancellation →
            # wrap_future cancels the underlying future too, and that future
            # never went through set_running, so it can always be cancelled):
            # set_result / set_exception raising InvalidStateError is then a
            # normal case — ignore the result and keep consuming. Otherwise
            # the worker thread dies and every subsequent job hangs.
            try:
                result = job.fn()
            except BaseException as exc:  # noqa: BLE001 - delivered via the Future
                try:
                    job.future.set_exception(exc)
                except concurrent.futures.InvalidStateError:
                    pass
                continue
            try:
                job.future.set_result(result)
            except concurrent.futures.InvalidStateError:
                pass

    def _submit(self, fn: Callable[[], Any]) -> "asyncio.Future":
        job = _Job(fn)
        self._jobs.put(job)
        return asyncio.wrap_future(job.future)

    def _submit_nowait(self, fn: Callable[[], Any]) -> None:
        """Queue a background drive job without awaiting the result (fn is
        responsible for its own exception handling)."""
        self._jobs.put(_Job(fn))

    # ------------------------------------------------------------- init
    def _init_client(self) -> None:
        from noeta.sdk import Capabilities
        from noeta.sdk import (
            AgentDefinition,
            Client,
            HostConfig,
            Options,
            OtlpTraceConfig,
        )
        from noeta.storage.sqlite import (
            SqliteContentStore,
            SqliteDispatcher,
            SqliteEventLog,
        )

        from noeta.agent.models_config import get_default_model, get_models

        s = self._settings
        s.workspaces_path.mkdir(parents=True, exist_ok=True)

        db = s.noeta_db_path
        dispatcher = SqliteDispatcher(db)
        event_log = SqliteEventLog(db, lease_validator=dispatcher)
        content_store = SqliteContentStore(db)

        provider, self._provider_name = build_provider(s)

        default_model = get_default_model(s)
        allowed_models = [m.id for m in get_models(s)]

        capabilities = Capabilities(
            ask_user_question=True,
            skill_invocation=True,
            todo_write=True,
            # Memory is isolated per space (noeta 0.2.4 multi-tenant seam):
            # the memory root resolves through _memory_root_for_task to
            # DATA_DIR/memories/<space_id>; when resolution fails it falls
            # back to the empty isolation directory of global_memory_dir and
            # never lands in another space. Global tool-surface switch
            # (temporary, see config.py): off = excluded from the compiled
            # main spec.
            memory=s.memory_tools_enabled,
            delegation=s.subagent_enabled,
        )
        # Read-only snapshot for the /capabilities endpoint: the API layer
        # does not depend back on noeta internal types.
        self._capabilities = {
            "ask_user_question": capabilities.ask_user_question,
            "skill_invocation": capabilities.skill_invocation,
            "todo_write": capabilities.todo_write,
            "memory": capabilities.memory,
            "delegation": capabilities.delegation,
            "mcp": capabilities.mcp,
        }

        # Sandbox on: register the standard noeta fs/shell tools, with side
        # effects routed through the per-session container's ExecEnv
        # (HostConfig.sandbox_provider). Sandbox off: pure conversation mode,
        # empty allowed_tools → no file tools registered, no containers.
        system_prompt = _SYSTEM_PROMPT
        allowed_tools: tuple[Any, ...] = ()
        sandbox_provider: Any = None
        sandbox_spec: Any = None
        sandbox_exec_preamble: Any = None
        sandbox_backend_factory: Any = None
        sandbox_browser_factory: Any = None
        if s.sandbox_enabled:
            from noeta.sdk import (
                BoundPreamble,
                BrowserBackend,
                ExecEnv,
                SandboxHandle,
                SandboxSpec,
            )

            from noeta.agent.host.sandbox_provider import KnowledgeMountSandboxProvider
            from noeta.agent.host.sdk_browser_backend import SdkBrowserBackend
            from noeta.agent.host.sdk_sandbox_exec_env import SdkSandboxExecEnv

            self._sandbox_enabled = True
            # Provider: starts/tears down per-session containers + at allocate
            # time ro-mounts knowledge and every enabled skill into the
            # container (unified mount policy, zero copy).
            # resolve_container_id: containers are named and shared per
            # session — multiple root tasks of one session (workflow
            # multi-node) land in the same container.
            sandbox_provider = KnowledgeMountSandboxProvider(
                knowledge_root=s.knowledge_path,
                workspaces_root=s.workspaces_path,
                resolve_space=self._space_of_session,
                space_has_knowledge=self._space_has_ready_knowledge,
                list_knowledge_mounts=self._knowledge_mounts_for_space,
                builtin_skills_root=s.builtin_skills_path,
                space_skills_root=s.space_skills_path,
                list_builtin_skill_names=self._builtin_skill_names,
                list_space_skill_names=self._space_skill_names,
                image=s.sandbox_image,
                api_key_env=s.sandbox_api_key_env,
                memory=s.sandbox_memory,
                cpus=s.sandbox_cpus,
                resolve_container_id=self._container_id_for_task,
            )
            self._sandbox_provider = sandbox_provider
            # Per-session mounts are appended by the provider at allocate time
            # (knowledge + skills); the /workspace rw mount is appended
            # automatically by the noeta manager.
            sandbox_spec = SandboxSpec(
                image=s.sandbox_image,
                resources={"memory": s.sandbox_memory, "cpus": s.sandbox_cpus},
            )
            # Per-exec preamble seam (BoundPreamble): the runtime splices the
            # returned string into `cd <cwd> && <preamble><argv>` for every
            # exec in the container, fetched fresh per exec. Deployments that
            # need per-session identity or environment injection bind a
            # callable `(exec_env_ref, argv) -> str` here (the exec_env_ref,
            # `base_url#sandbox_id`, resolves back to the container id and
            # thence to the session user via _resolve_context / the
            # session-naming convention). The returned string comes from the
            # deployment, never from model-controlled input — the same trust
            # level as setting container env. This deployment has no per-exec
            # setup left, so the hook stays None.
            sandbox_exec_preamble = None

            # The fs/shell/browser wire goes through the official
            # agent-sandbox SDK (noeta 0.2.3 factory seam, matching the
            # upstream lifecycle.py wiring): the adapters implement the same
            # ExecEnv / BrowserBackend surface as the handwritten defaults,
            # keeping tool schemas and the stable prefix unchanged — only the
            # transport layer is swapped — and fix at the root the image's
            # /v1/file/read ignoring encoding=base64 (replacing the old
            # _patch_aio_read_bytes monkey-patch).
            def _sdk_backend_factory(
                handle: SandboxHandle, preamble: Optional[BoundPreamble] = None
            ) -> ExecEnv:
                return SdkSandboxExecEnv(
                    base_url=handle.base_url,
                    auth_headers=handle.auth.connect_headers,
                    preamble=preamble,
                )

            def _sdk_browser_factory(handle: SandboxHandle) -> BrowserBackend:
                return SdkBrowserBackend(
                    base_url=handle.base_url,
                    auth_headers=handle.auth.connect_headers,
                )

            sandbox_backend_factory = _sdk_backend_factory
            sandbox_browser_factory = _sdk_browser_factory
            # Live sandbox preview: gateway (token registry + HTTP/WS reverse
            # proxy) + a server on its own port (origin isolation, see the
            # sandbox_preview.py module docstring). serve_forever runs on a
            # daemon thread with zero interaction with the FastAPI event loop.
            from noeta.agent.host.sandbox_preview import (
                SandboxPreviewGateway,
                make_preview_server,
            )

            self._preview_gateway = SandboxPreviewGateway()
            try:
                self._preview_server = make_preview_server(
                    self._preview_gateway,
                    host=s.host,
                    port=s.sandbox_preview_port,
                )
                threading.Thread(
                    target=self._preview_server.serve_forever,
                    name="sandbox-preview",
                    daemon=True,
                ).start()
                logger.info(
                    "sandbox preview server on %s:%s",
                    s.host,
                    self._preview_server.server_address[1],
                )
            except OSError:
                # Bind failure (port taken, etc.): the preview surface is
                # unavailable (the discovery endpoint has no port ⇒ the
                # frontend hides the panels); the main agent path is not
                # blocked.
                logger.warning("sandbox preview server bind failed", exc_info=True)
                self._preview_server = None
            # Standard fs/shell tools: shell_run is automatically upgraded to
            # ARBITRARY within the container boundary, and
            # permission_mode=bypassPermissions removes gating (see the noeta
            # host build). glob/grep are not registered: running rg/find via
            # shell_run is more flexible and covers the search needs.
            allowed_tools = ("read", "write", "edit", "shell_run")
            system_prompt += _SANDBOX_PROMPT
            # Delegation guidance rides along only when spawn_subagent is
            # present (otherwise the model would call nonexistent subagents).
            if s.subagent_enabled:
                system_prompt += _SANDBOX_SUBAGENT_PROMPT
        # memory=True registers the memory tools, so the policy prompt must be
        # present alongside them (when to record / not record / dedup); with
        # memory globally off, no tools are registered and no policy prompt is
        # carried.
        if s.memory_tools_enabled:
            system_prompt += _MEMORY_PROMPT

        # Channel-read tools + board tools: host-side sqlite reads/writes,
        # mixed into allowed_tools (noeta mixed-entry: builtin name strings +
        # DecoratedTool side by side). Registered globally, the descriptions
        # scope applicability; calls from non-applicable tasks return failure
        # hints. Dependencies are fetched through zero-arg getters (a lifespan
        # attach later than _init_client is fine). With collab globally off
        # the whole group stays unregistered (channel_read_* + board_*).
        if s.collab_tools_enabled:
            from noeta.agent.host.board_tools import build_board_tools
            from noeta.agent.host.channel_tools import build_channel_tools

            allowed_tools = (
                *allowed_tools,
                *build_channel_tools(lambda: self._channel_service),
                *build_board_tools(
                    lambda: getattr(self, "_board_store", None),
                    self._space_id_for_task,
                    lambda task_id: (
                        self._channel_service.topic_link_for_task(task_id)
                        if self._channel_service is not None
                        else None
                    ),
                ),
            )

        # Delegation subagents (explorer/web): registered only when the
        # subagent global switch is on — with delegation=False, spawn_subagent
        # never enters main's compiled spec anyway, so these definitions would
        # be dead entries; skip the whole group. feedback_analysis /
        # consolidation are internal agents seeded by name, not via
        # delegation, and are always registered (see below).
        agents: dict[str, Any] = {}
        if s.subagent_enabled:
            # Search subagent: read-only + shell_run for command-driven search
            # (rg/find beat dedicated tools), all capabilities False (a leaf
            # agent — no follow-up questions, no further delegation). The name
            # is the dict key; spawnable is merged in automatically by the
            # SDK. explorer is an internal subagent not exposed directly in
            # the user prompt; its risk surface is lower than the main agent's
            # writable tools. The main agent's file surface exists only in the
            # sandbox.
            explorer_tools = ("read", "shell_run")
            agents["explorer"] = AgentDefinition(
                description=(
                    "Read-only search subagent: locates code/files across the "
                    "workspace in parallel and reports search conclusions"
                ),
                prompt=_EXPLORER_PROMPT,
                tools=explorer_tools,
                capabilities=Capabilities(),
            )
            # web browsing subagent (sandbox mode only): the browser tool
            # surface is flag-gated — when the session has a container and
            # Capabilities.browser=True, the SDK vends an AioBrowserBackend
            # from the same sandbox handle and merges the browser_* tools
            # (noeta 0.2.0). main keeps browser=False: browsing happens in the
            # subagent's separate context and the main line receives only
            # distilled conclusions, isolating token bloat (matching the noeta
            # upstream main / web split). Without the sandbox there is no
            # container and thus no browser; not registered.
            if s.sandbox_enabled:
                agents["web"] = AgentDefinition(
                    description=(
                        "Web-browsing subagent: drives a real browser inside "
                        "the session container for open/click/type/multi-step "
                        "browsing tasks and returns distilled conclusions"
                    ),
                    prompt=_WEB_PROMPT,
                    tools=("read", "write", "shell_run"),
                    capabilities=Capabilities(browser=True),
                )

        # Feedback-analysis internal agent: the __-prefixed name stays out of
        # the spawnable roster and can only be seeded by name through
        # start_feedback_analysis (same wiring as consolidation). The tool
        # surface is all host-side closures; per-run context resolves through
        # the ctx task_id.
        from noeta.agent.host.feedback_analysis import (
            FEEDBACK_ANALYSIS_AGENT_NAME,
            build_feedback_analysis_agent,
        )

        agents[FEEDBACK_ANALYSIS_AGENT_NAME] = build_feedback_analysis_agent(
            resolve_run=self._feedback_run_for_ctx,
            replay_events=lambda tid: self._replay_single(tid, None),
            read_reference=self._read_feedback_reference,
            skill_roots=lambda space_id: [
                s.space_skills_path / space_id,
                s.builtin_skills_path,
            ],
            memory_root=lambda space_id: s.memories_path / space_id,
            create_suggestion=self._create_feedback_suggestion,
            create_report=self._create_feedback_report,
        )

        options = Options(
            system_prompt=system_prompt,
            name="main",
            capabilities=capabilities,
            permission_mode="bypassPermissions",
            allowed_tools=allowed_tools,
            agents=agents,
        )
        if s.memory_tools_enabled and s.memory_consolidation:
            # Register the __consolidation__ internal agent (English preset,
            # memory tool surface only): run_consolidation seeds it by name,
            # the double-underscore name stays out of the spawnable roster,
            # and main's compiled spec is byte-identical. Triggering happens
            # in _maybe_consolidate_memory. With memory globally off there is
            # nothing to consolidate; skip the whole group.
            from noeta.presets import with_consolidation_agent

            options = with_consolidation_agent(options)
        self._client = Client(
            options,
            provider=provider,
            workspace_dir=s.workspaces_path,
            model=default_model.id,
            allowed_models=allowed_models,
            host_config=HostConfig(
                event_log=event_log,
                content_store=content_store,
                dispatcher=dispatcher,
                write_mode="apply",
                # Some gateways use a stable per-task session id for
                # prompt-cache affinity: pinning every turn of a task to the
                # same backend account lets its KV cache actually be reused
                # (and avoids invalid_encrypted_content on long sessions).
                # Only wired for the openai (Responses) provider; mock gets
                # none.
                provider_headers=(
                    (lambda ctx: {"x-session-id": ctx.task_id})
                    if self._provider_name == "openai"
                    else None
                ),
                # Token streaming (SDK 0.1.7+, token-streaming projection):
                # when the provider implements StreamingProvider (the openai
                # OpenAIResponsesProvider does), each LLM call's token deltas
                # are pushed to the session through this sink. A delta is a
                # transient projection — never hits the EventLog, not
                # replayed, not backfilled after a disconnect; the durable
                # truth remains the subsequent MessagesAppended. The mock
                # (FakeLLMProvider) does not implement StreamingProvider and
                # the runtime seam skips streaming, so mock / test paths have
                # no deltas (behavior unchanged). Compaction summarize calls
                # complete(allow_stream=False); the runtime guarantees no
                # streaming there. See _on_delta.
                delta_sink=self._on_delta,
                # Per-session container seam: the provider starts/tears down
                # containers, the spec fixes image+mounts, the preamble is a
                # per-exec injection hook (None here, see above). All three
                # are non-None only when the sandbox is enabled.
                sandbox_provider=sandbox_provider,
                sandbox_spec=sandbox_spec,
                sandbox_exec_preamble=sandbox_exec_preamble,
                sandbox_backend_factory=sandbox_backend_factory,
                sandbox_browser_factory=sandbox_browser_factory,
                # Space memory (noeta 0.2.4 multi-tenant seam): the resolver
                # maps a task to its memory root at
                # DATA_DIR/memories/<space_id>; global_memory_dir is the
                # isolation fallback when resolution fails (kept empty — it
                # only prevents cross-space leaks, never a real store).
                memory_root_resolver=self._memory_root_for_task,
                global_memory_dir=s.memories_path / "_quarantine",
                # OTLP trace export (opt-in): the Client constructs the
                # trace-export observer only when OTLP_ENDPOINT is set;
                # headers ride on every export request (hosted-collector
                # auth) and never enable anything by themselves. The ambient
                # OTel-standard endpoint env is deliberately not honored as
                # an enable switch (see config.py).
                otlp_traces=(
                    OtlpTraceConfig(
                        endpoint=s.otlp_endpoint,
                        headers=s.otlp_header_items,
                    )
                    if s.otlp_endpoint
                    else None
                ),
            ),
        )
        self._client.subscribe(self._on_envelope)
        # Preview mounts follow the container lifecycle (SDK 0.2.0 product
        # wiring hooks): mount on allocate, decrement the refcount on release
        # (multiple root tasks of one session share the container, see the
        # gateway comments).
        if self._preview_gateway is not None:
            self._client.add_sandbox_lifecycle_listener(
                self._preview_on_allocate, self._preview_on_release
            )
        # Rebuild the root task_id → session mapping: must happen before
        # start_workers, otherwise when the WorkerLoop's requeue_stale
        # auto-re-drives crash-leftover tasks after a restart, their events
        # cannot route to a session (see the module docstring on restart crash
        # recovery).
        for session in self._store.list_all_with_task():
            if session.task_id:
                self._task_to_session[session.task_id] = session.id
                self._session_to_user[session.id] = session.user
        # All node tasks of workflow sessions (session.task_id is only the
        # latest one): routing / container resolution / follow-up messages all
        # need to find the older node tasks.
        # Variable naming note: must not use ``s`` — ``s = self._settings``
        # above is still read by the whole tail of this function
        # (start_workers / logging); shadowing it would hand start_workers a
        # Session.
        for t in self._store.list_all_session_tasks():
            tid = t["task_id"]
            if tid and tid not in self._task_to_session:
                owner = self._store.get(t["session_id"])
                if owner is not None:
                    self._task_to_session[tid] = owner.id
                    self._session_to_user[owner.id] = owner.user
        # Start the resident worker pool: start/send_goal/answer now go
        # through seed_* + _yield_seeded_lease to hand the lease back to the
        # pool, and N workers drive different sessions' turns concurrently
        # (same-session turns stay serialized by the dispatcher lease). The
        # mock / test path is equally safe (FakeLLM is concurrency-safe).
        # num_workers=1 degrades to a single worker. Callable only once
        # (guarded by the SDK).
        self._client.start_workers(s.agent_num_workers)
        # Sandbox idle reclamation: start the daemon patrol thread only when
        # the sandbox is enabled and at least one level is configured (does
        # not block startup; exceptions are self-contained). Both levels off =
        # containers run until session deletion / process exit.
        if s.sandbox_enabled and (
            s.sandbox_idle_stop_hours > 0 or s.sandbox_idle_remove_hours > 0
        ):
            self._sandbox_reaper_stop.clear()
            self._sandbox_reaper_thread = threading.Thread(
                target=self._sandbox_reaper_loop,
                name="sandbox-idle-reaper",
                daemon=True,
            )
            self._sandbox_reaper_thread.start()
            logger.info(
                "sandbox idle reaper started: stop=%.1fh remove=%.1fh interval=%.1fh",
                s.sandbox_idle_stop_hours,
                s.sandbox_idle_remove_hours,
                max(s.sandbox_idle_check_interval_hours, 1 / 60),
            )
        logger.info(
            "AgentService ready: provider=%s model=%s builtin_skills=%s sandbox=%s workers=%s",
            self._provider_name, default_model.id, s.builtin_skills_path,
            s.sandbox_image if s.sandbox_enabled else "off",
            s.agent_num_workers,
        )

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def capabilities(self) -> dict[str, bool]:
        """Snapshot of the current agent capability switches (for the
        /capabilities endpoint; exposes no noeta types)."""
        return dict(self._capabilities)

    def _resolve_context(self, task_id: str) -> tuple[Optional[str], Optional[str]]:
        """task_id → (session_id, user_id): the identity-resolution seam for
        host-side integrations that act on behalf of the session user (e.g. a
        per-exec preamble binding, see _init_client).

        A task_id not yet bound to a session (e.g. the startup window) returns
        (None, None); a missing user (legacy sessions with no user recorded)
        returns (session_id, None) — callers degrade to acting without an
        identity.
        """
        session_id = self._task_to_session.get(task_id)
        if session_id is None:
            return None, None
        return session_id, self._session_to_user.get(session_id)

    def _container_id_for_task(self, task_id: str) -> Optional[str]:
        """root task id → session id (sandbox containers are named and shared
        per session).

        allocate happens inside seed_start (before the TaskCreated binding),
        when the mapping table does not have the task yet — fall back to
        _pending_session (_start_fresh sets it before seed_start; the
        jobs-worker serialization prevents cross-talk).
        """
        return self._task_to_session.get(task_id) or self._pending_session

    # ------------------------------------------------- sandbox idle reaper
    def _sandbox_reaper_loop(self) -> None:
        """Background daemon patrol: reclaim idle per-session containers (two
        levels, see Settings).

        Criteria (see ``_reap_idle_sandboxes``): session.status == 'idle' and
        ``now - updated_at`` beyond each level's threshold. idle = the turn
        ended, no task running, no question pending — the container is
        spinning idle. waiting (awaiting an answer) / running (subtask
        barrier) are never reclaimed, otherwise answering would wait for the
        container to come back up.

        The interval is clamped to a one-minute floor to avoid busy-spinning
        on tiny configurations.
        """
        s = self._settings
        stop_after_s = s.sandbox_idle_stop_hours * 3600.0
        remove_after_s = s.sandbox_idle_remove_hours * 3600.0
        interval_s = max(s.sandbox_idle_check_interval_hours * 3600.0, 60.0)
        while not self._sandbox_reaper_stop.wait(interval_s):
            try:
                self._reap_idle_sandboxes(stop_after_s, remove_after_s)
            except Exception:  # noqa: BLE001 - a patrol failure never kills the thread; next tick retries
                logger.debug("sandbox idle reaper tick failed", exc_info=True)

    def _reap_idle_sandboxes(
        self, stop_after_s: float, remove_after_s: float
    ) -> None:
        """Run one reclamation sweep. A standalone method so unit tests can
        drive it directly (no thread, no interval wait).

        Level one ``stop_idle``: stop the container but keep it; when the user
        continues the conversation, attach ``docker start`` brings it back
        as-is (continuation goes resume→attach, never re-allocate — so this
        level must **not** remove the container, see the docker_sandbox module
        docstring). Level two ``force_release``: actually tear down,
        reclaiming disk; after that the session cannot attach back. Both
        levels are idempotent (incomplete refcounts after a process restart,
        or a container already removed by the session-deletion path, are both
        safe); a level with threshold <= 0 is disabled.
        """
        provider = self._sandbox_provider
        if provider is None:
            return
        now = time.time()
        # Only sessions with a task (no task = no container ever started);
        # status/updated_at come from the store, independent of the in-memory
        # mappings (_task_to_session etc.), so they stay accurate after a
        # process restart.
        for session in self._store.list_all_with_task():
            if session.status != "idle":
                continue
            idle_s = now - session.updated_at
            try:
                if remove_after_s > 0 and idle_s > remove_after_s:
                    provider.force_release(session.id)
                    logger.info(
                        "removed long-idle sandbox: session=%s idle_for=%.0fm",
                        session.id, idle_s / 60.0,
                    )
                elif stop_after_s > 0 and idle_s > stop_after_s:
                    # Already-stopped containers return False — no duplicate
                    # logging.
                    if provider.stop_idle(session.id):
                        logger.info(
                            "stopped idle sandbox: session=%s idle_for=%.0fm",
                            session.id, idle_s / 60.0,
                        )
            except Exception:  # noqa: BLE001 - best-effort reclamation; never blocks other sessions
                logger.debug(
                    "sandbox reap failed (continuing): %s", session.id,
                    exc_info=True,
                )

    def _memory_root_for_task(self, task_id: str) -> Optional[Path]:
        """task id → space memory root (DATA_DIR/memories/<space_id>; memory
        is isolated per space).

        SDK memory_root_resolver seam (noeta 0.2.4): the engine-built memory
        tools, goal-time recall, and Client.memory_root all resolve through
        it. The mapping reuses the container scheme (_task_to_session +
        _pending_session covering the seed window, see
        _container_id_for_task); when unresolvable (startup window, legacy
        sessions without a space) it returns None → the SDK falls back to the
        empty isolation directory of global_memory_dir — better no recall than
        cross-space leakage.
        """
        explicit = self._task_memory_root.get(task_id)
        if explicit is not None:
            return explicit
        session_id = self._task_to_session.get(task_id) or self._pending_session
        if session_id is None:
            return None
        space_id = self._space_of_session(session_id)
        if not space_id:
            return None
        return self._settings.memories_path / space_id

    # ------------------------------------------------ memory consolidation
    def _maybe_consolidate_memory(self, session_id: str) -> None:
        """Turn-boundary tap (emit thread): after the switch checks, post the
        consolidation pass to the serial jobs worker.

        Must go through the jobs worker rather than a separate thread:
        run_consolidation's internal seed_start emits a root TaskCreated, and
        if that ran concurrently with _start_fresh's seed window, the curation
        task would wrongly consume the _pending_session slot and splice
        consolidation events into a user session (breaking _on_envelope
        binding invariant ④). The jobs worker's serialization makes the two
        naturally mutually exclusive. Debouncing keeps the cost of a no-op
        pass at one marker read, affordable once per turn."""
        if not (
            self._settings.memory_tools_enabled
            and self._settings.memory_consolidation
        ):
            return
        self._submit_nowait(lambda: self._consolidation_pass(session_id))

    def _consolidation_pass(self, session_id: str) -> None:
        """One debounced consolidation attempt (runs serially on the jobs
        worker, independently per space).

        The digest is fed only this space's sessions (include_task judges
        ownership via task→session→space; tasks owned by no session — the
        curation task itself among them — never enter the digest); both the
        memory_root and the debounce marker live in this space's memory
        directory, so spaces debounce independently; on_seeded registers the
        curation task into _task_memory_root before the lease is handed back,
        so the consolidation agent's memory tools resolve back to the same
        space. Failures only log — consolidation is best-effort background
        behavior and must never affect the user path."""
        try:
            from noeta.sdk import consolidation_due, run_consolidation

            space_id = self._space_of_session(session_id)
            if not space_id:
                return
            root = self._settings.memories_path / space_id
            now = datetime.now(timezone.utc)
            hours = self._settings.memory_consolidation_debounce_hours
            if not consolidation_due(root, now=now, debounce_hours=hours):
                return

            space_cache: dict[str, Optional[str]] = {}

            def _in_space(task_id: str) -> bool:
                sid = self._task_to_session.get(task_id)
                if sid is None:
                    return False
                if sid not in space_cache:
                    space_cache[sid] = self._space_of_session(sid)
                return space_cache[sid] == space_id

            run_consolidation(
                self._client,
                memory_root=root,
                now=now,
                debounce_hours=hours,
                include_task=_in_space,
                on_seeded=lambda tid: self._task_memory_root.__setitem__(tid, root),
            )
        except Exception:  # noqa: BLE001 - background consolidation failures stay off the user path
            logger.exception(
                "memory consolidation pass failed: session=%s", session_id
            )

    # ------------------------------------------------ feedback analysis
    def start_feedback_analysis(
        self,
        space_id: str,
        space_name: str,
        run_id: str,
        feedback_items: list[dict],
    ) -> None:
        """Seed one feedback-analysis run (the API layer already created the
        run row and did the concurrency check).

        Must go through the jobs worker: seed_start emits a root TaskCreated,
        and running concurrently with _start_fresh's seed window would wrongly
        consume the _pending_session slot (the same constraint as
        consolidation, see the _maybe_consolidate_memory docstring)."""
        self._submit_nowait(
            lambda: self._feedback_analysis_seed(
                space_id, space_name, run_id, feedback_items
            )
        )

    def _feedback_analysis_seed(
        self,
        space_id: str,
        space_name: str,
        run_id: str,
        feedback_items: list[dict],
    ) -> None:
        """Assemble the goal + seed the __feedback_analysis__ root task (runs
        serially on the jobs worker).

        The run context is registered before the lease is handed back (the
        same ordering guarantee as consolidation's on_seeded: by the time a
        worker picks up the task, the tools can resolve the context). A seed
        failure finalizes the run as failed — no eternally-running zombie
        rows."""
        store = self._feedback_store
        try:
            from noeta.sdk import MemoryStore

            from noeta.agent.host.feedback_analysis import (
                FEEDBACK_ANALYSIS_AGENT_NAME,
                FeedbackRunContext,
                build_analysis_goal,
            )

            config_store = getattr(self, "_agent_config_store", None)
            persona = config_store.get(space_id)["prompt"] if config_store else ""
            goal = build_analysis_goal(
                feedback_items,
                space_name,
                skill_names=(
                    self._builtin_skill_names() + self._space_skill_names(space_id)
                ),
                persona=persona,
                memory_index=list(
                    MemoryStore(self._settings.memories_path / space_id).entries()
                ),
            )
            seeded = self._client.seed_start(
                goal=goal, agent=FEEDBACK_ANALYSIS_AGENT_NAME
            )
            self._feedback_runs[seeded.task_id] = FeedbackRunContext(
                run_id=run_id,
                space_id=space_id,
                feedback={fb["id"]: fb for fb in feedback_items},
                allowed_task_ids={
                    fb["task_id"] for fb in feedback_items if fb.get("task_id")
                },
            )
            if store is not None:
                store.set_run_task(run_id, seeded.task_id)
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001 - background drive path (same as consolidation)
        except Exception as e:  # noqa: BLE001 - a seed failure finalizes the run, off the user path
            logger.exception("feedback analysis seed failed: run=%s", run_id)
            if store is not None:
                try:
                    store.finish_run(run_id, "failed", error=str(e)[:500])
                except Exception:  # noqa: BLE001
                    logger.exception("feedback run finalize failed: %s", run_id)

    def _feedback_run_for_ctx(self, ctx: Any) -> Any:
        """Tool callback → owning analysis-run context (ToolContext.metadata
        carries the task_id)."""
        task_id = (getattr(ctx, "metadata", None) or {}).get("task_id")
        return self._feedback_runs.get(task_id) if task_id else None

    def _read_feedback_reference(
        self, space_id: str, feedback_id: str
    ) -> Optional[str]:
        """Read the reference snapshot (path derived by convention, see
        services/feedback_reference.py)."""
        from noeta.agent.services.feedback_reference import reference_path

        try:
            return reference_path(
                self._settings, space_id, feedback_id
            ).read_text(encoding="utf-8")
        except OSError:
            return None

    def _create_feedback_suggestion(self, **kwargs) -> dict:
        if self._feedback_store is None:
            raise ValueError("feedback store not attached")
        return self._feedback_store.create_suggestion(**kwargs)

    def _create_feedback_report(self, **kwargs) -> dict:
        if self._feedback_store is None:
            raise ValueError("feedback store not attached")
        return self._feedback_store.create_report(**kwargs)

    def start_feedback_report(
        self,
        space_id: str,
        space_name: str,
        run_id: str,
        triggered_by: str,
        suggestions: list[dict],
        feedback_map: dict[str, dict],
    ) -> None:
        """Seed one report-aggregation run (report mode; the same concurrency
        constraint as start_feedback_analysis)."""
        self._submit_nowait(
            lambda: self._feedback_report_seed(
                space_id, space_name, run_id, triggered_by,
                suggestions, feedback_map,
            )
        )

    def _feedback_report_seed(
        self,
        space_id: str,
        space_name: str,
        run_id: str,
        triggered_by: str,
        suggestions: list[dict],
        feedback_map: dict[str, dict],
    ) -> None:
        store = self._feedback_store
        try:
            from noeta.agent.host.feedback_analysis import (
                FEEDBACK_ANALYSIS_AGENT_NAME,
                FeedbackRunContext,
                build_report_goal,
            )

            goal = build_report_goal(suggestions, feedback_map, space_name)
            seeded = self._client.seed_start(
                goal=goal, agent=FEEDBACK_ANALYSIS_AGENT_NAME
            )
            self._feedback_runs[seeded.task_id] = FeedbackRunContext(
                run_id=run_id,
                space_id=space_id,
                feedback=dict(feedback_map),
                allowed_task_ids={
                    fb["task_id"]
                    for fb in feedback_map.values()
                    if fb.get("task_id")
                },
                kind="report",
                triggered_by=triggered_by,
            )
            if store is not None:
                store.set_run_task(run_id, seeded.task_id)
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001 - background drive path (same as consolidation)
        except Exception as e:  # noqa: BLE001 - a seed failure finalizes the run
            logger.exception("feedback report seed failed: run=%s", run_id)
            if store is not None:
                try:
                    store.finish_run(run_id, "failed", error=str(e)[:500])
                except Exception:  # noqa: BLE001
                    logger.exception("feedback run finalize failed: %s", run_id)

    def _finalize_feedback_run(self, task_id: str, etype: str) -> None:
        """Analysis task terminal state → run finalization (emit thread; the
        FeedbackStore carries its own lock).

        Background-seeded root tasks go through the interactive drive path, so
        the normal ending is the trailing next-goal TaskSuspended (not
        TaskCompleted, see the SDK multi_turn_policy_wrapper); with all
        capabilities off there are no question-style suspensions, so
        TaskSuspended is uniformly treated as a successful end."""
        if etype in ("TaskSuspended", "TaskCompleted"):
            status, error = "done", None
        elif etype in ("TaskFailed", "TaskCancelled"):
            status, error = "failed", f"analysis task ended abnormally ({etype})"
        else:
            return
        run_ctx = self._feedback_runs.pop(task_id, None)
        if run_ctx is None or self._feedback_store is None:
            return
        try:
            if status == "done":
                if run_ctx.kind == "report" and not run_ctx.report_created:
                    # A report run that finished without calling submit_report
                    # is not a success — prevents the "done but no report in
                    # the list" false success that leaves the owner waiting.
                    status, error = "failed", "the analysis agent produced no report"
                elif run_ctx.kind == "analysis":
                    self._feedback_store.mark_analyzed(
                        list(run_ctx.feedback.keys()), run_ctx.run_id
                    )
            self._feedback_store.finish_run(run_ctx.run_id, status, error=error)
        except Exception:  # noqa: BLE001 - finalize failures only log
            logger.exception("feedback run finalize failed: %s", run_ctx.run_id)

    # ------------------------------------------------------- sandbox preview
    def _preview_on_allocate(self, root_task_id: str, handle: Any) -> None:
        """Container allocate → preview mount (sandbox lifecycle listener,
        worker thread)."""
        session_id = self._container_id_for_task(root_task_id) or root_task_id
        self._preview_gateway.mount_root(
            root_task_id, session_id, handle.base_url, handle.auth.connect_headers
        )

    def _preview_on_release(self, root_task_id: str) -> None:
        """Root task terminal state → preview refcount decrement (only the
        last root actually unmounts)."""
        session_id = self._container_id_for_task(root_task_id)
        self._preview_gateway.release_root(root_task_id, session_id=session_id)

    def sandbox_preview_info(self, session_id: str) -> Optional[dict[str, Any]]:
        """Discovery payload ``{token, port, panels}``; returns None without a
        sandbox / live container.

        Lazy backstop on a registry miss: after a process restart, requeued
        tasks go through the attach path and never fire the allocate listener,
        but the container is still running — look up the live handle from the
        provider and mount it back.
        """
        if self._preview_gateway is None or self._preview_server is None:
            return None
        info = self._preview_gateway.preview_info(session_id)
        if info is not None:
            return info
        provider = self._sandbox_provider
        if provider is None:
            return None
        try:
            handle = provider.live_handle(session_id)
        except Exception:  # noqa: BLE001 - a docker query failure counts as no container
            logger.debug("preview live_handle failed: %s", session_id, exc_info=True)
            return None
        if handle is None:
            return None
        self._preview_gateway.mount_session(
            session_id, handle.base_url, handle.auth.connect_headers
        )
        return self._preview_gateway.preview_info(session_id)

    # ------------------------------ knowledge mount resolution (provider)
    def _space_of_session(self, session_id: str) -> Optional[str]:
        """The session's owning space (lets the provider resolve
        session→space for per-session knowledge mounts)."""
        session = self._store.get(session_id)
        return session.space_id if session else None

    def _knowledge_mounts_for_space(
        self, space_id: str
    ) -> Optional[list[tuple[str, str]]]:
        """The space's mount list [(mount-point name, host directory)],
        filtered by the agent configuration's selection.

        No selection configured = every ready source; [] = none participate;
        None is returned only when the stores are unavailable (the provider
        falls back to mounting the whole directory). Always mounting per
        source keeps only knowledge/<source name>/ paths visible inside the
        container — a whole-directory mount would also expose the
        materialization id directories, and after the agent searches with
        rg/find (which does not follow the name symlinks) it would cite the id
        paths. The source name is the in-container mount point
        (knowledge/<source name>/, matching the skill reference contract); CJK
        names as directory names are only a problem for the host-side
        safe_username paths, never for container mount points.

        The space-level derived layer `_derived/` (the unified tracking-point
        view) is mounted extra at the end. It sits beside the source
        directories and is not a row in knowledge_sources, so the per-source
        loop cannot reach it — left unmounted, the skill contract's
        `knowledge/_derived/point_library.ndjson` would never exist inside the
        container and retrieval would degrade to full-corpus grep. Mounted
        only when the selection has not been narrowed: the view is a
        cross-source join (the tracking-point master table × the code event
        catalog), and with a source deselected, mounting it would leak that
        source's content back into the container.
        """
        config_store = getattr(self, "_agent_config_store", None)
        knowledge_store = getattr(self, "_knowledge_store", None)
        if config_store is None or knowledge_store is None:
            return None
        selected = config_store.get(space_id).get("knowledge_sources")
        chosen = set(selected) if selected is not None else None
        mounts: list[tuple[str, str]] = []
        root = self._settings.knowledge_path / space_id
        narrowed = False
        for src in knowledge_store.list_sources(space_id):
            if src.get("status") != "ready":
                continue
            if chosen is not None and src["id"] not in chosen:
                narrowed = True
                continue
            src_dir = root / src["id"]
            if src_dir.is_dir():
                mounts.append((src["name"], str(src_dir)))
        derived_dir = root / "_derived"
        if mounts and not narrowed and derived_dir.is_dir():
            mounts.append(("_derived", str(derived_dir)))
        return mounts

    def _space_has_ready_knowledge(self, space_id: str) -> bool:
        """Whether the space has any ready knowledge source (none = do not
        mount knowledge, avoiding an empty mount)."""
        knowledge_store = getattr(self, "_knowledge_store", None)
        if knowledge_store is None:
            return False
        try:
            return any(
                src.get("status") == "ready"
                for src in knowledge_store.list_sources(space_id)
            )
        except Exception:  # noqa: BLE001 - a store query failure counts as no knowledge
            logger.warning(
                "failed to check space knowledge status: %s", space_id, exc_info=True
            )
            return False

    # ---------------------------------- skill mount resolution (provider)
    def _builtin_skill_names(self) -> list[str]:
        """Enabled global builtin skill names (for the provider's per-session
        skill mounts)."""
        skill_store = getattr(self, "_skill_store", None)
        if skill_store is None:
            return []
        try:
            return sorted(skill_store.builtin_enabled_names())
        except Exception:  # noqa: BLE001
            logger.warning("failed to list builtin skills", exc_info=True)
            return []

    def _space_skill_names(self, space_id: str) -> list[str]:
        """This space's enabled skill names (for the provider's per-session
        skill mounts)."""
        skill_store = getattr(self, "_skill_store", None)
        if skill_store is None:
            return []
        try:
            return sorted(skill_store.enabled_names(space_id))
        except Exception:  # noqa: BLE001
            logger.warning(
                "failed to list space skills space=%s", space_id, exc_info=True
            )
            return []

    # ------------------------------------------------------- event bridge
    def _deref(self, ref: Any) -> Optional[bytes]:
        try:
            return self._client.get_content(ref.hash)
        except Exception:  # noqa: BLE001 - unretrievable content counts as missing
            logger.exception("content deref failed")
            return None

    def _on_envelope(self, env: Any) -> None:
        """noeta post-commit subscription callback (fires on the emit thread).

        Binding rules: an existing mapping routes directly (fast path); an
        unknown task is only considered at TaskCreated — one carrying
        parent_task_id is a subtask, mapped to the parent task's session
        (memory only); one without is a root task, going through the
        _pending_session slot and persisted.
        """
        task_id = env.task_id
        # Feedback-analysis tasks: they belong to no session and their events
        # never route to the frontend; only finalize the run at a terminal
        # state and return (must be intercepted before the TaskCreated binding
        # block, to keep them from wrongly consuming _pending_session).
        if task_id in self._feedback_runs:
            self._finalize_feedback_run(task_id, env.type)
            return
        if task_id not in self._task_to_session:
            if env.type != "TaskCreated":
                return
            # The lock-free safety premise of the cross-thread binding block
            # (invariants; breaking any one requires re-reviewing with a lock):
            # ① each task_id's TaskCreated is emitted exactly once
            #    (post-commit single emission, no retry/re-emit) — there is no
            #    concurrent same-key write race;
            # ② root and child write different keys into _task_to_session and
            #    never overwrite each other;
            # ③ a single dict/set read/write is atomic under the GIL; a stale
            #    read at worst routes one beat late;
            # ④ _pending_session is set/cleared only inside _start_fresh (the
            #    worker is serial, no reentry), and only the root branch
            #    (empty parent_id) consumes it — a subtask's TaskCreated
            #    (non-empty parent_id) cannot wrongly eat the slot.
            parent_id = getattr(env.payload, "parent_task_id", None)
            if parent_id:
                parent_session = self._task_to_session.get(parent_id)
                if parent_session is None:
                    # Orphan subtask: the parent task is not yet / no longer in
                    # the mapping (e.g. an in-flight subtask across a restart,
                    # or nested delegation breaking the "parent binds first"
                    # assumption) — expose it explicitly
                    logger.warning(
                        "subtask %s created before parent %s mapped; dropping",
                        task_id, parent_id,
                    )
                    return
                self._task_to_session[task_id] = parent_session
                self._subtask_ids.add(task_id)
                # For event tagging: subtask events belong to their root
                # task's tab (under the current delegation design the parent
                # is always the root; nested delegation does not exist)
                self._subtask_root[task_id] = parent_id
            elif self._pending_session is not None:
                self._task_to_session[task_id] = self._pending_session
                # Persist immediately: only then can an SSE connection that
                # joins mid-first-turn replay the leading events by task_id
                self._store.update(self._pending_session, task_id=task_id)
            else:
                return
        session_id = self._task_to_session[task_id]
        is_subtask = task_id in self._subtask_ids
        if not is_subtask:
            # Session status follows only the root task: subtask lifecycles
            # never touch it
            self._update_status(env, session_id, task_id)
        try:
            events = translate(
                env, self._deref, subtask_id=task_id if is_subtask else None
            )
        except Exception:  # noqa: BLE001 - a translation failure never blocks the engine
            logger.exception("translate failed for envelope %s", env.type)
            return
        if events:
            # Tag events with their task: workflow sessions' per-tab SSE
            # filters on `_task` (subtask events belong to their root task's
            # tab). Regular sessions' frontend ignores the field — zero
            # impact.
            root_id = self._subtask_root.get(task_id, task_id)
            for ev in events:
                ev.data.setdefault("_task", root_id)
            self._push(session_id, events)

    def _on_delta(self, ctx: Any, call_id: str, delta: Any) -> None:
        """delta_sink callback (fired on WorkerLoop pool threads): token
        streaming increments.

        A delta is a transient projection (the token-streaming-projection
        design): it never hits the EventLog, is not replayed, and is not
        backfilled after a disconnect; the durable truth remains the
        subsequent MessagesAppended. This only wraps it into a seq-less
        synthetic frame pushed to the session subscription queues — the SSE
        endpoint sends no id for seq=None frames, skips dedup, and yields them
        directly, orthogonal to the replay machinery (replay reads the
        EventLog, which naturally contains no deltas).

        Only root task deltas are forwarded: streaming display for subtasks
        (task_id in _subtask_ids) is left for later work. The callback fires
        on WorkerLoop pool threads; _push's internal call_soon_threadsafe is
        cross-thread safe (the same path as _on_envelope). Sink exceptions are
        already swallowed by the runtime (an observational channel), so no try
        here.
        """
        task_id = ctx.task_id
        if task_id in self._subtask_ids:
            return
        session_id = self._task_to_session.get(task_id)
        if session_id is None:
            return
        self._push(session_id, [UIEvent(
            seq=None,
            type="delta",
            data={
                "call_id": call_id,
                "kind": delta.kind,
                "text": delta.text,
                "index": delta.index,
                "_task": task_id,
            },
        )])

    def _update_status(self, env: Any, session_id: str, task_id: str) -> None:
        etype = env.type
        # A terminal state is the task's absorbing state: after it, no event
        # of that task changes status again. _on_envelope runs on the emit
        # thread, while cancel and the turn itself emit from two different
        # threads — driver.cancel() system_emits TaskCancelled directly on the
        # request thread without waiting for a step boundary, while that
        # turn's TaskSuspended is emitted by the worker thread at its own
        # pace. Their writes to session.status have no ordering guarantee, and
        # a late TaskSuspended(question-*) would flip an already-terminated
        # turn back to waiting, wedging the session permanently: a new message
        # is judged busy (409), yet there is no real question to answer.
        # Tracking per task rather than globally means cancelling an old task
        # cannot freeze the same session's new task (continuing after cancel
        # goes exactly through a new task).
        if task_id in self._terminal_tasks:
            return
        if etype in ("TaskCancelled", "TaskFailed", "TaskCompleted"):
            self._terminal_tasks.add(task_id)
        if etype in ("TaskStarted", "TaskWoken"):
            self._set_task_status(session_id, task_id, "running")
        elif etype == "UserQuestionRequested":
            # Status flips to waiting before the question event is pushed: the
            # client can answer the moment it receives the question, without
            # hitting the "TaskSuspended not yet arrived" 409 race.
            self._set_task_status(session_id, task_id, "waiting")
        elif etype == "TaskSuspended":
            wake_on = getattr(env.payload, "wake_on", None)
            if is_waiting_subtask(wake_on):
                # The root is parked on a subtask barrier (foreground
                # fan-out): subtasks are running, the session stays running,
                # and this does not count as the end of a turn — do not set
                # idle, do not unlock the composer, do not trigger title
                # generation. Once the barrier is satisfied the root is woken
                # by TaskWoken and continues. Wrongly setting idle would let
                # the user inject messages while subtasks run and pollute the
                # conversation (the session would look ready for input while
                # the subagent executes). Same criterion as the translator
                # (is_waiting_subtask).
                return
            handle = getattr(wake_on, "handle", "")
            status = (
                "waiting"
                if isinstance(handle, str) and handle.startswith("question-")
                else "idle"
            )
            self._set_task_status(session_id, task_id, status)
            # First turn over (suspended awaiting input / wrap-up): try async
            # LLM title generation.
            self._maybe_generate_title(session_id)
            # Turn boundary = the consolidation trigger seam (next-goal
            # wrap-up only; waiting on a question does not count as turn end);
            # debouncing is checked inside the pass, zero IO here.
            if status == "idle":
                self._maybe_consolidate_memory(session_id)
        elif etype in ("TaskCancelled", "TaskFailed", "TaskCompleted"):
            self._set_task_status(session_id, task_id, "idle")
            # TaskCompleted also counts as first-turn end (TaskFailed /
            # Cancelled generally carry no usable conversation;
            # _maybe_generate_title skips them for lack of a first message or
            # a recorded failure).
            if etype == "TaskCompleted":
                self._maybe_generate_title(session_id)
                self._maybe_consolidate_memory(session_id)

    def _set_task_status(self, session_id: str, task_id: str, status: str) -> None:
        """Persist status: a workflow node task writes its per-task row +
        aggregates the session; a regular session writes the session directly
        (original semantics unchanged). A workflow per-task change pushes a
        workflow_update frame on the way out (the tab bar's status source)."""
        wf_task = self._store.get_session_task_by_task_id(task_id)
        if wf_task is None:
            self._store.update(session_id, status=status)
            return
        self._store.update_session_task_status(task_id, status)
        self._aggregate_workflow_status(session_id)
        self._push_workflow_update(session_id)

    def _aggregate_workflow_status(self, session_id: str) -> None:
        """session.status = the aggregation of the node tasks' statuses
        (running > waiting > idle), keeping the sidebar display compatible
        with the old semantics."""
        statuses = {
            t["status"]
            for t in self._store.list_session_tasks(session_id)
            if t["task_id"]
        }
        agg = (
            "running" if "running" in statuses
            else "waiting" if "waiting" in statuses
            else "idle"
        )
        self._store.update(session_id, status=agg)

    def _push_workflow_update(self, session_id: str) -> None:
        """Push one workflow_update frame to subscribers (a full idempotent
        snapshot; the data source for the tab bar / advance button)."""
        from noeta.agent.workflow.service import workflow_view

        session = self._store.get(session_id)
        if session is None:
            return
        snapshot = session.workflow
        if snapshot is None:
            return
        tasks = self._store.list_session_tasks(session_id)
        self._push(
            session_id, [UIEvent(None, "workflow_update", workflow_view(snapshot, tasks))]
        )

    def _push(self, session_id: str, events: list[UIEvent]) -> None:
        with self._subs_lock:
            queues = list(self._subs.get(session_id, ()))
        if not queues or self._loop is None:
            return
        for q in queues:
            for ev in events:
                self._loop.call_soon_threadsafe(q.put_nowait, ev)

    # ---------------------------------------------------- title generation
    def _maybe_generate_title(self, session_id: str) -> None:
        """First-turn end triggers LLM title generation. Called on the emit
        callback thread; it only does the checks + starts a dedicated daemon
        thread for the actual LLM call — occupying neither the jobs-worker nor
        the noeta WorkerLoop.

        Skip conditions (any one returns without starting a thread): already
        generated (title_generated), already failed once in this process
        (_title_failed), generation in flight (_generating_titles), no task_id
        (the first turn has not started a task yet). The first user message is
        fetched from the event stream in the daemon thread
        (_first_user_message); when unavailable, a failure is recorded — no
        dependence on in-process memory state, so old sessions recovered after
        a restart / interruption get back-filled too. Under the mock provider
        generate_title returns None internally, taking the failure branch
        without persisting.
        """
        session = self._store.get(session_id)
        if session is None or session.title_generated:
            return
        if session_id in self._title_failed:
            return
        if session_id in self._generating_titles:
            return
        task_id = session.task_id or ""
        if not task_id:
            return

        self._generating_titles.add(session_id)
        t = threading.Thread(
            target=self._run_title_generation,
            args=(session_id, task_id),
            name=f"title-gen-{session_id[:8]}",
            daemon=True,
        )
        t.start()

    def _run_title_generation(self, session_id: str, task_id: str) -> None:
        """Daemon thread body: call the LLM for a title; on success persist +
        push a session_meta frame.

        Retry semantics: success → title_generated=True, never generated
        again; failure → recorded in the in-process _title_failed (no more
        retries this process, saving LLM calls), but title_generated stays
        unset, so after a process restart the memory is clear and the next
        turn end retries once — the "persistent failures self-heal, transient
        failures do not spam" trade-off.

        All the material comes from the event stream (no dependence on memory
        state): the first user message + the assistant reply. Anything missing
        only degrades, never blocks.
        """
        try:
            first_message = self._first_user_message(task_id)
            if not first_message:
                self._title_failed.add(session_id)
                return
            assistant_reply = self._latest_assistant_reply(task_id)
            try:
                title = generate_title(
                    self._settings,
                    first_message,
                    assistant_reply,
                    task_id,
                )
            except Exception:  # noqa: BLE001 - backstop: any exception counts as failure
                logger.exception("title generation crashed for %s", session_id)
                title = None
            if not title:
                self._title_failed.add(session_id)
                return
            # Re-check before persisting: the session may have been deleted
            # (get returns None) → abandon.
            if self._store.get(session_id) is None:
                return
            self._store.update(session_id, title=title, title_generated=1)
            self._push(session_id, [UIEvent(None, "session_meta", {"title": title})])
        finally:
            # Release the in-flight placeholder: on the success path
            # title_generated blocks later triggers; on the failure path
            # _title_failed does. The release itself only avoids leaving a
            # dangling placeholder.
            self._generating_titles.discard(session_id)

    def _first_user_message(self, task_id: str) -> Optional[str]:
        """Fetch the task's first user-message text from the event stream (for
        title generation).

        Same mechanism as _latest_assistant_reply (events_after + translate),
        taking the first user_message instead of the last assistant_text.

        Returns None when unavailable (no client / no events / no user
        message) — the caller records a failure. No provider short-circuit:
        under mock, tests monkeypatch generate_title to return a fixed title
        and still need the first message fetched to reach it; under openai it
        is a real generation. Reading the event stream is read-only and runs
        on a dedicated daemon thread — no concurrent side effects.
        """
        if self._client is None or not task_id:
            return None
        try:
            for env in self._client.events_after(task_id, after_seq=None):
                for ev in translate(env, self._deref):
                    if ev.type == "user_message":
                        text = ev.data.get("content")
                        if isinstance(text, str) and text.strip():
                            return text.strip()[:600]
            return None
        except Exception:  # noqa: BLE001 - a fetch failure counts as no first message
            logger.debug("first user message fetch failed", exc_info=True)
            return None

    def _latest_assistant_reply(self, task_id: str) -> Optional[str]:
        """Best-effort fetch of the task's latest assistant body text (first
        300 chars) as reference for the title prompt.

        Returns None when unavailable (no client / no events / all tool
        turns) — the title is then generated from the first user message
        alone; this is not a failure. Read-only on the event stream,
        cross-thread safe (the same direct-read mode as replay).

        Reads only under openai (the provider that actually generates
        titles): under the mock provider generate_title always returns None,
        so reading the reply would be pure waste and would start a concurrent
        events_after read on every mock turn — short-circuit directly to keep
        pointless concurrency out of the all-mock test suite.
        """
        if self._provider_name != "openai":
            return None
        if self._client is None or not task_id:
            return None
        try:
            latest: Optional[str] = None
            for env in self._client.events_after(task_id, after_seq=None):
                for ev in translate(env, self._deref):
                    if ev.type == "assistant_text":
                        text = ev.data.get("text")
                        if isinstance(text, str) and text.strip():
                            latest = text
            return latest[:300] if latest else None
        except Exception:  # noqa: BLE001 - a fetch failure never blocks title generation
            logger.debug("latest assistant reply fetch failed", exc_info=True)
            return None

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._subs_lock:
            self._subs.setdefault(session_id, set()).add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        with self._subs_lock:
            queues = self._subs.get(session_id)
            if queues is not None:
                queues.discard(q)
                if not queues:
                    self._subs.pop(session_id, None)

    # ------------------------------------------------------------ workspace
    def workspace_for(self, session_id: str) -> Path:
        """Session workspace `workspaces/<session_id>`: in sandbox mode it is
        bind-mounted into the container at /workspace and agent output lands
        here; it also holds the noeta runtime's `.noeta` metadata.

        Skill assembly:
        - **Sandbox mode**: skills are per-session ro bind-mounted by
          KnowledgeMountSandboxProvider at allocate time into the container at
          `/workspace/.noeta/skills/<name>/`; here we only create the empty
          directory as a mount point, no copy (zero copy, same policy as
          knowledge).
        - **Non-sandbox mode**: pure conversation mode; the agent discovers
          skills on the host, so symlinks point at the source directories
          (host discovery follows symlinks).

        Sources uniformly read the `skills` table: global builtins
        (`space_id="*"` and enabled) ∪ enabled in this space.
        """
        ws = self._settings.workspaces_path / session_id
        noeta_skills = ws / ".noeta" / "skills"
        sandbox = self._sandbox_enabled

        # Per-session containers run as non-root (uid 1000 "gem"): the session
        # directory is bind-mounted into the container at /workspace and must
        # be writable by the container user (cd / writing output). Local
        # single-user machine — the files are the user's own.
        if sandbox:
            ws.mkdir(parents=True, exist_ok=True)
            ws.chmod(0o777)

        # Clean up the legacy symlink (migration: it used to be a symlink
        # pointing at the global directory)
        if noeta_skills.is_symlink():
            noeta_skills.unlink()
        if not noeta_skills.parent.exists():
            noeta_skills.parent.mkdir(parents=True, exist_ok=True)
        if not noeta_skills.is_dir():
            noeta_skills.mkdir(parents=True)

        # The space agent configuration's persona section → workspace AGENT.md
        # (written when present, deleted when cleared; the agent reads and
        # follows it per the _SANDBOX_PROMPT convention)
        self._write_agent_md(ws, session_id)

        if sandbox:
            # Sandbox mode: clear old copy/symlink leftovers and leave an
            # empty directory as the skill mount point
            for existing in noeta_skills.iterdir():
                if existing.is_symlink() or existing.is_file():
                    existing.unlink()
                else:
                    shutil.rmtree(existing, ignore_errors=True)
            return ws

        # Non-sandbox mode: symlink assembly (host discovery follows symlinks)
        session = self._store.get(session_id)
        skill_store = getattr(self, "_skill_store", None)
        if skill_store is None:
            return ws

        # Clear old assembled entries and rebuild to reflect additions /
        # removals
        for existing in noeta_skills.iterdir():
            if existing.is_symlink() or existing.is_file():
                existing.unlink()
            else:
                shutil.rmtree(existing, ignore_errors=True)

        def _install(source_dir: Path, name: str, kind: str, space_id: str) -> None:
            skill_dir = source_dir / name
            if not (skill_dir / "SKILL.md").is_file():
                logger.warning(
                    "%s skill registry has a row but the directory is missing, "
                    "skipping assembly: space=%s name=%s",
                    kind, space_id, name,
                )
                return
            dest = noeta_skills / name
            # First one wins on a name collision (builtins install first, a
            # same-named space skill is skipped; the API already blocks
            # uploading / installing duplicates)
            if dest.exists():
                return
            dest.symlink_to(skill_dir)

        # Global builtins: apply to every session
        builtin_root = self._settings.builtin_skills_path
        for name in skill_store.builtin_enabled_names():
            _install(builtin_root, name, "builtin", GLOBAL_SPACE_ID)

        # Skills enabled in this space
        if session and session.space_id:
            space_root = self._settings.space_skills_path / session.space_id
            for name in skill_store.enabled_names(session.space_id):
                _install(space_root, name, "space", session.space_id)

        return ws

    def _write_agent_md(self, ws: Path, session_id: str) -> None:
        """The space configuration's append-style prompt lands in the
        workspace `AGENT.md` (idempotent: clearing the configuration deletes
        it).

        Why a file instead of the system prompt: under 0.2.1 the agent
        definitions are registered statically at Client init and per-space
        customization has no runtime seam; a workspace file follows the same
        "wayfinding" convention as the knowledge INDEX.md and can be smoothly
        replaced by per-space AgentDefinitions after an SDK upgrade."""
        config_store = getattr(self, "_agent_config_store", None)
        if config_store is None:
            return
        try:
            session = self._store.get(session_id)
            prompt = ""
            if session and session.space_id:
                prompt = (config_store.get(session.space_id).get("prompt") or "").strip()
            target = ws / "AGENT.md"
            if prompt:
                content = f"# Space agent configuration\n\n{prompt}\n"
                if not target.exists() or target.read_text("utf-8") != content:
                    target.write_text(content, "utf-8")
            elif target.exists():
                target.unlink()
        except Exception:  # noqa: BLE001 - a persona write failure never blocks assembly
            logger.warning("AGENT.md assembly failed: session=%s", session_id, exc_info=True)

    # ---------------------------------------------------------- file surface
    @property
    def sandbox_available(self) -> bool:
        """Whether the sandbox is enabled (the file surface's single gate).
        When off, pure conversation mode — no file surface."""
        return self._sandbox_enabled

    def session_workspace_path(self, session_id: str) -> Path:
        """The session workspace's host directory (= the bind-mount source of
        the container's /workspace). The file surface reads it."""
        return self._settings.workspaces_path / session_id

    async def sandbox_list_files(self, session_id: str):
        """Session workspace file listing (the frontend file API's data
        source), read directly from the host directory.

        Not via the _submit serial queue: the file panel is read-only
        high-frequency reading, queueing has no benefit; os.walk blocks, so it
        goes through the anyio thread pool (the same cross-thread read mode as
        get_content_by_hash). Excludes the top-level knowledge/ (a symlink to
        the mounted knowledge base, potentially hundreds of thousands of
        files; os.walk does not descend into symlinks anyway).
        """
        import anyio

        from noeta.agent.host.workspace_files import list_files

        if not self._sandbox_enabled:
            return []
        return await anyio.to_thread.run_sync(
            list_files, self.session_workspace_path(session_id), {"knowledge"}
        )

    async def sandbox_read_file(self, session_id: str, path: str) -> Optional[str]:
        """Read a text file inside the session workspace; returns None when
        disabled / missing / unreadable / escaping."""
        import anyio

        from noeta.agent.host.workspace_files import resolve_within

        if not self._sandbox_enabled:
            return None
        root = self.session_workspace_path(session_id)

        def _read() -> Optional[str]:
            resolved = resolve_within(root, path)
            if resolved is None or not resolved.is_file():
                return None
            try:
                return resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

        return await anyio.to_thread.run_sync(_read)

    # --------------------------------------------------------- drive entries
    def send_message(
        self,
        session: Session,
        content: str,
        model: Optional[str],
        effort: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> None:
        """Post one turn of conversation driving (202 semantics: return
        immediately, progress flows over SSE).

        Workflow sessions: the message routes to the target node task
        (task_id defaulting to the most recently started node), and the busy
        check follows **that task's** status — other nodes running
        concurrently do not block this node's message. Regular-session
        behavior is unchanged (task_id ignored).
        """
        if session.workflow is not None:
            self._send_workflow_message(session, content, model, effort, task_id)
            return
        if session.status != "idle":
            raise SessionBusyError(session.status)
        chosen = model or session.model
        updates: dict[str, Any] = {"status": "running", "model": chosen}
        if session.task_id is None and session.title == "New session":
            updates["title"] = content.strip().splitlines()[0][:40] or "New session"
        self._store.update(session.id, **updates)
        # Bind session → user: the identity seam host-side integrations
        # resolve through (_resolve_context). Done before the drive is posted,
        # guaranteeing the mapping is ready by tool-invoke time (covering both
        # the new-session and the continued-conversation path).
        self._session_to_user[session.id] = session.user
        ws = self.workspace_for(session.id)
        sid, cur_task = session.id, session.task_id
        goal = content

        def job() -> None:
            self._drive(sid, cur_task, ws, goal, chosen, effort)

        self._submit_nowait(job)
        # Instant UI feedback: seed_start blocks synchronously on sandbox
        # allocate (docker run + health check, 2-10s), during which neither
        # TaskCreated nor user_message is emitted. Push a synthetic
        # turn_started frame first so the frontend flips running=true and
        # shows the indicator immediately. Takes effect instantly for
        # continued sessions whose SSE is already connected; a new session's
        # first message is covered by the frontend's optimisticSend (the SSE
        # connection is established only after sendMessage returns).
        self._push(sid, [UIEvent(None, "turn_started", {})])

    def _send_workflow_message(
        self,
        session: Session,
        content: str,
        model: Optional[str],
        effort: Optional[str],
        task_id: Optional[str],
    ) -> None:
        """Workflow-session message driving: route to the target node task,
        per-task busy check."""
        tasks = [
            t for t in self._store.list_session_tasks(session.id) if t["task_id"]
        ]
        if task_id:
            target = next((t for t in tasks if t["task_id"] == task_id), None)
        else:
            target = tasks[-1] if tasks else None
        if target is None:
            raise SessionBusyError("no-task")
        if target["status"] != "idle":
            raise SessionBusyError(target["status"])
        chosen = model or session.model
        self._store.update(session.id, model=chosen)
        self._store.update_session_task_status(target["task_id"], "running")
        self._aggregate_workflow_status(session.id)
        self._session_to_user[session.id] = session.user
        ws = self.workspace_for(session.id)
        sid, tid, node_index = session.id, target["task_id"], target["node_index"]

        def job() -> None:
            self._drive(
                sid, tid, ws, content, chosen, effort, node_index=node_index
            )

        self._submit_nowait(job)
        self._push(sid, [UIEvent(None, "turn_started", {"_task": tid})])
        self._push_workflow_update(sid)

    def start_workflow_node(
        self,
        session: Session,
        node_index: int,
        goal: str,
        params: Optional[dict] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> None:
        """Start one workflow node task (the first node at session creation /
        the next node on advance confirm).

        "Node already started" (a task exists for the same node_index) is
        validated at the API layer; this only drives. The task_id is recorded
        into session_tasks by _start_fresh after seed_start returns.
        """
        chosen = model or session.model
        self._store.update(session.id, model=chosen, status="running")
        self._session_to_user[session.id] = session.user
        ws = self.workspace_for(session.id)
        sid = session.id
        node_params = dict(params or {})

        def job() -> None:
            self._drive(
                sid, None, ws, goal, chosen, effort,
                node_index=node_index, node_params=node_params,
            )

        self._submit_nowait(job)

    def _drive(
        self,
        session_id: str,
        task_id: Optional[str],
        ws: Path,
        goal: str,
        model: str,
        effort: Optional[str] = None,
        node_index: Optional[int] = None,
        node_params: Optional[dict] = None,
    ) -> None:
        try:
            if task_id is None:
                self._start_fresh(
                    session_id, ws, goal, model, effort,
                    node_index=node_index, node_params=node_params,
                )
            else:
                try:
                    # seed+yield: seed_send_goal validates + writes metadata +
                    # the lease synchronously on this thread
                    # (NotResumableError is raised synchronously by the first
                    # step _require_human_suspend), then _yield_seeded_lease
                    # hands the lease back to the pool for a worker to drive
                    # asynchronously. Mirrors the upstream engine_room.py
                    # seed/yield pattern (including the noqa: SLF001).
                    seeded = self._client.seed_send_goal(
                        task_id, goal=goal, model_selector=model, effort=effort
                    )
                    self._client._yield_seeded_lease(seeded)  # noqa: SLF001 — SDK surface
                except Exception as exc:  # noqa: BLE001
                    if type(exc).__name__ != "NotResumableError":
                        raise
                    # Cancelled / non-resumable task: start a new task on the
                    # same workspace (files kept; the old event stream no
                    # longer replays — see the module docstring's risk note).
                    # Workflow node tasks reopen the same way: node_index
                    # passes through and the new task replaces the old row.
                    logger.info("task %s not resumable, starting fresh", task_id)
                    self._start_fresh(
                        session_id, ws, goal, model, effort,
                        node_index=node_index,
                    )
        except Exception as exc:  # noqa: BLE001 - backstop: errors are delivered over SSE
            self._handle_drive_failure(session_id, exc, task_id=task_id)

    def _start_fresh(
        self,
        session_id: str,
        ws: Path,
        goal: str,
        model: str,
        effort: Optional[str] = None,
        node_index: Optional[int] = None,
        node_params: Optional[dict] = None,
    ) -> None:
        self._pending_session = session_id
        # Knowledge is per-session bind-mounted by the provider into the
        # container at /workspace/knowledge (see
        # KnowledgeMountSandboxProvider); no more symlinks.
        try:
            # seed+yield: seed_start synchronously writes TaskCreated +
            # metadata + the lease on this thread (TaskCreated is emitted on
            # the same thread during this window; _on_envelope consumes the
            # _pending_session binding) and runs no LLM; take seeded.task_id
            # for binding/persistence, and finally _yield_seeded_lease hands
            # the lease back to the pool for a worker to drive asynchronously.
            # Mirrors the upstream engine_room.py background_drive=True branch
            # (including the noqa: SLF001).
            seeded = self._client.seed_start(
                goal=goal,
                workspace_dir=str(ws),
                model_selector=model,
                effort=effort,
            )
        finally:
            self._pending_session = None
        task_id = seeded.task_id
        # seed_start has written the lease; see the finally comment for the
        # ordering trade-off between bookkeeping and yield.
        try:
            self._task_to_session[task_id] = session_id
            # session.task_id is updated to the current active-task snapshot
            # (already updated at _on_envelope binding; this is the backstop).
            self._store.update(session_id, task_id=task_id)
            if node_index is not None:
                # Workflow node: record the task into session_tasks (reopening
                # the same node overwrites the old row) and push
                # workflow_update immediately so the tab bar gets the new
                # task_id.
                self._store.add_session_task(
                    session_id, node_index, task_id,
                    params=node_params or {}, status="running",
                )
                self._aggregate_workflow_status(session_id)
                self._push_workflow_update(session_id)
        finally:
            # The yield must happen: seed_start has already persisted the
            # lease, and if the bookkeeping above threw without yielding, that
            # lease would stall until the WorkerLoop stale-sweep (default
            # lease_seconds=600s) reclaims it — the session would sit wedged
            # with nobody driving. So the finally backstops the yield; the
            # exception still propagates outward for _handle_drive_failure to
            # deliver the error. Better "error notice and driving coexist"
            # than a stalled lease. Bookkeeping failures are extremely rare
            # (DB writes) and their side effects are far lighter than a lease
            # stall.
            self._client._yield_seeded_lease(seeded)  # noqa: SLF001 — SDK surface

    def answer(
        self,
        session: Session,
        question_id: str,
        answers: dict,
        task_id: Optional[str] = None,
    ) -> None:
        """Answer a question. Workflow sessions route by target task (default
        = the most recent waiting node)."""
        target_task: Optional[str]
        if session.workflow is not None:
            tasks = [
                t for t in self._store.list_session_tasks(session.id)
                if t["task_id"]
            ]
            if task_id:
                target = next((t for t in tasks if t["task_id"] == task_id), None)
            else:
                waiting = [t for t in tasks if t["status"] == "waiting"]
                target = waiting[-1] if waiting else None
            if target is None or target["status"] != "waiting":
                raise SessionBusyError("no-waiting-task")
            target_task = target["task_id"]
            self._store.update_session_task_status(target_task, "running")
            self._aggregate_workflow_status(session.id)
            self._push_workflow_update(session.id)
        else:
            if session.status != "waiting":
                raise SessionBusyError(session.status)
            if not session.task_id:
                raise SessionBusyError("no-task")
            self._store.update(session.id, status="running")
            target_task = session.task_id
        sid, tid = session.id, target_task
        is_workflow = session.workflow is not None

        def job() -> None:
            try:
                # seed+yield: seed_answer validates the answers + writes
                # metadata + the lease synchronously on this thread
                # (validation failures raise synchronously), then
                # _yield_seeded_lease hands it to the pool for async driving.
                # Mirrors the upstream engine_room.py (including the noqa:
                # SLF001).
                seeded = self._client.seed_answer(
                    tid, question_id=question_id, answers=answers,
                    answered_by="user",
                )
                self._client._yield_seeded_lease(seeded)  # noqa: SLF001 — SDK surface
            except Exception as exc:  # noqa: BLE001
                # Answer validation failure etc.: back to waiting so the user
                # can re-answer
                logger.exception("answer failed")
                if is_workflow:
                    self._store.update_session_task_status(tid, "waiting")
                    self._aggregate_workflow_status(sid)
                    self._push_workflow_update(sid)
                else:
                    self._store.update(sid, status="waiting")
                self._push(sid, [UIEvent(
                    None, "error",
                    {"message": f"answer was not accepted: {exc}", "_task": tid}
                )])

        self._submit_nowait(job)

    # --------------------------------------- channel-entry per-task driving
    # A channel session = a regular session + multiple root tasks (thread =
    # task, reusing the session_tasks / container-sharing scheme), but not a
    # workflow (workflow_json is NULL, so the frontend renders no tab bar).
    # send_message / answer's non-workflow paths route by session-level
    # status, which does not fit multi-task sessions — hence these entry
    # points that route explicitly by task (used by the channel routing
    # layer).

    def send_task_message(
        self,
        session: Session,
        task_id: str,
        content: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> None:
        """Drive one message turn on the given task with a per-task busy check
        (isomorphic to _send_workflow_message, without requiring the session
        to be a workflow)."""
        row = self._store.get_session_task_by_task_id(task_id)
        if row is None or row["session_id"] != session.id:
            raise SessionBusyError("no-task")
        if row["status"] != "idle":
            raise SessionBusyError(row["status"])
        chosen = model or session.model
        self._store.update_session_task_status(task_id, "running")
        self._aggregate_workflow_status(session.id)
        self._session_to_user[session.id] = session.user
        ws = self.workspace_for(session.id)
        sid, tid, node_index = session.id, task_id, row["node_index"]

        def job() -> None:
            self._drive(sid, tid, ws, content, chosen, effort, node_index=node_index)

        self._submit_nowait(job)
        self._push(sid, [UIEvent(None, "turn_started", {"_task": tid})])

    def answer_task(
        self,
        session: Session,
        task_id: str,
        question_id: str,
        answers: dict,
    ) -> None:
        """Answer a question on the given task with a per-task waiting check
        (used by channel threads)."""
        row = self._store.get_session_task_by_task_id(task_id)
        if row is None or row["session_id"] != session.id:
            raise SessionBusyError("no-task")
        if row["status"] != "waiting":
            raise SessionBusyError(row["status"])
        self._store.update_session_task_status(task_id, "running")
        self._aggregate_workflow_status(session.id)
        sid, tid = session.id, task_id

        def job() -> None:
            try:
                seeded = self._client.seed_answer(
                    tid, question_id=question_id, answers=answers,
                    answered_by="user",
                )
                self._client._yield_seeded_lease(seeded)  # noqa: SLF001 — SDK surface
            except Exception as exc:  # noqa: BLE001 - validation failure: back to waiting for a re-answer
                logger.exception("answer_task failed")
                self._store.update_session_task_status(tid, "waiting")
                self._aggregate_workflow_status(sid)
                self._push(sid, [UIEvent(
                    None, "error",
                    {"message": f"answer was not accepted: {exc}", "_task": tid}
                )])

        self._submit_nowait(job)

    def latest_assistant_reply(self, task_id: str) -> Optional[str]:
        """The task's most recent assistant body text (the channel entry's
        reply outlet). noeta sqlite reads carry their own lock; cross-thread
        direct reads are safe (same usage as _run_title_generation)."""
        return self._latest_assistant_reply(task_id)

    def cancel(self, session: Session, task_id: Optional[str] = None) -> None:
        """Cancel directly from the request thread (the official cross-thread
        design); takes effect at step boundaries.

        Only the root task is cancelled: noeta's cancellation registry
        cascades to background subtasks (each subtask abandons driving at its
        next step boundary and writes its own TaskCancelled, which
        _on_envelope routes into a subtask_finished{cancelled} closing the
        frontend card), so no per-subtask cancel loop is needed here. Workflow
        sessions target a node via task_id (default = session.task_id, the
        latest node).
        """
        target = task_id or session.task_id
        if not target:
            self._store.update(session.id, status="idle")
            return
        try:
            self._client.cancel(target, reason="user_cancelled")
        except Exception:  # noqa: BLE001 - cancel of an already-terminated task is treated as idempotent
            logger.exception("cancel failed (treated as idempotent)")
            self._set_task_status(session.id, target, "idle")

    def _handle_drive_failure(
        self, session_id: str, exc: Exception, task_id: Optional[str] = None
    ) -> None:
        session = self._store.get(session_id)
        # Cancel race: the TaskCancelled envelope has already set status idle
        # and sent turn_finished{cancelled}; an exception thrown by the drive
        # thread at that point is only logged.
        if session is not None and session.status == "running":
            logger.exception("drive failed for session %s", session_id)
            # Workflow node task: reset the row's status (otherwise it stays
            # running forever and blocks later messages)
            wf_task = (
                self._store.get_session_task_by_task_id(task_id) if task_id else None
            )
            if wf_task is not None:
                self._set_task_status(session_id, task_id, "idle")
            else:
                self._store.update(session_id, status="idle")
            extra = {"_task": task_id} if task_id else {}
            self._push(
                session_id,
                [
                    UIEvent(None, "error", {"message": str(exc)[:500], **extra}),
                    UIEvent(None, "turn_finished", {"status": "failed", **extra}),
                ],
            )
        else:
            logger.info("drive aborted for session %s: %s", session_id, exc)

    # ------------------------------------------------------------ read paths
    async def replay(
        self,
        session: Session,
        since_seq: Optional[int],
        task_id: Optional[str] = None,
    ) -> list[UIEvent]:
        """Replay the event stream. With task_id, replay that task (workflow
        per-tab); otherwise replay session.task_id (regular sessions / the
        workflow's latest node). Task ownership is validated at the API
        layer."""
        target = task_id or session.task_id
        if not target:
            return []
        # Not via the _submit serial queue. Replay is a read-only event-stream
        # read, yet it decides when the frontend's loading skeleton ends (SSE
        # sends replay_done only after it returns). After seed+yield, _submit
        # only carries short seed jobs (seed_* validates + writes metadata +
        # the lease synchronously and immediately _yield_seeded_lease's to the
        # pool; the long turn runs in the WorkerLoop, not this queue) — but
        # _submit is still a global single-thread serial queue: queueing
        # replay behind it would pointlessly put it after every session's seed
        # jobs (latency accumulates under bursts of new sessions), with no
        # benefit for a read-only, high-frequency read. session.task_id is
        # persisted by _on_envelope at TaskCreated during seed_start (the
        # earliest event of a start), before status=waiting, so a concurrent
        # direct read sees it; event_log.read is read-only with its own lock +
        # check_same_thread=False, cross-thread safe (same pattern as
        # get_content_by_hash / sandbox_list_files).
        import anyio

        if self._client is None:
            return []  # startup window (HTTP requests actually all come after lifespan startup; unreachable in practice)
        return await anyio.to_thread.run_sync(self._replay_single, target, since_seq)

    def _replay_single(
        self, task_id: str, since_seq: Optional[int]
    ) -> list[UIEvent]:
        """Single-task replay."""
        events: list[UIEvent] = []
        subtask_ids: list[str] = []
        for env in self._client.events_after(task_id, after_seq=since_seq):
            events.extend(translate(env, self._deref))
            if env.type in ("BackgroundSubagentStarted", "SubtaskSpawned"):
                subtask_ids.append(env.payload.subtask_id)
        # Subtask steps ride along only on a full replay (first connect /
        # refresh, since_seq None or 0), appended directly after the root
        # events (the frontend groups by subtask_id; subtask_started already
        # created the card earlier). Reconnects (since_seq > 0) do not resend:
        # subtask events are synthetic frames (no seq) the frontend cannot
        # dedup, and resending would duplicate the steps.
        if since_seq:
            return events
        for sub_id in subtask_ids:
            try:
                for env in self._client.events_after(sub_id, after_seq=None):
                    events.extend(translate(env, self._deref, subtask_id=sub_id))
            except Exception:  # noqa: BLE001 - one missing subtask stream does not affect the overall replay
                logger.exception("subtask replay failed: %s", sub_id)
        return events

    async def get_content_by_hash(self, content_hash: str) -> Optional[bytes]:
        """Fetch raw ContentStore bytes by hash (the Trace page derefs
        ContentRefs with it).

        Not via the _submit serial queue: _submit is a global single-thread
        serial queue (after seed+yield it only carries short seed jobs), and a
        content fetch is a small read-only operation — queueing has no
        benefit. This is the same cross-thread read mode as _deref calling
        client.get_content on the callback thread, here via the anyio thread
        pool: the sqlite connection uses check_same_thread=False, and
        SqliteContentStore.get is a single SELECT holding its own
        threading.Lock — cross-thread safe.

        media_type is not returned here: the noeta ContentStore is a
        hash→bytes retrieval interface, and the caller (the frontend) already
        holds ContentRef.media_type and interprets the bytes by it.
        """
        import anyio

        if self._client is None:
            # Off the queue we lose the "ordered after _init_client"
            # guarantee; during the startup window treat it as missing
            return None
        return await anyio.to_thread.run_sync(self._client.get_content, content_hash)

    async def raw_events(
        self, session: Session, cursor: Optional[dict[str, int]]
    ) -> dict:
        """The raw envelope stream of the session's noeta task subtree (root +
        each subtask, not folded through the translator).

        For the Trace page: whole envelopes are serialized to JSON structures
        via envelope_to_dict; ContentRefs in payloads are kept as-is (with
        __canonical_tag__), not dereferenced.

        Each task stream counts seq independently, so a single since_seq
        cannot express subtree progress — the incremental cursor is a
        {task_id: last_seq} map (isomorphic to the stream-level cursor of
        noeta's official /stream), echoed back in the response for the caller
        to return next time; None/empty means full. Subtask ids are not
        scanned from the whole DB: they come from those already in the cursor
        ∪ the SubtaskSpawned / BackgroundSubagentStarted markers in this
        round's root increment.

        Skips the _submit serial queue for the same reason as replay: a
        read-only event-stream read; after seed+yield _submit only carries
        short seed jobs (the long turn is in the WorkerLoop), but the global
        single-thread serialization would still pointlessly park the Trace
        page behind every session's seed jobs; session.task_id binds early
        (see replay), so a concurrent direct read is safe.
        """
        marks: dict[str, int] = dict(cursor or {})
        if not session.task_id:
            return {"events": [], "cursor": marks}
        import anyio

        if self._client is None:
            return {"events": [], "cursor": marks}
        root_id = session.task_id

        def job() -> dict:
            from noeta.sdk import envelope_to_dict

            events: list[dict] = []

            def pull(task_id: str) -> list:
                envs = []
                for env in self._client.events_after(
                    task_id, after_seq=marks.get(task_id)
                ):
                    envs.append(env)
                    marks[task_id] = env.seq
                    try:
                        events.append(envelope_to_dict(env))
                    except Exception:  # noqa: BLE001 - one bad row must not take down the whole page
                        logger.exception(
                            "raw envelope serialize failed: %s",
                            getattr(env, "type", "?"),
                        )
                return envs

            subtask_ids = [tid for tid in marks if tid != root_id]
            for env in pull(root_id):
                if env.type in ("BackgroundSubagentStarted", "SubtaskSpawned"):
                    if env.payload.subtask_id not in subtask_ids:
                        subtask_ids.append(env.payload.subtask_id)
            for sub_id in subtask_ids:
                try:
                    pull(sub_id)
                except Exception:  # noqa: BLE001 - one missing subtask stream does not affect the whole
                    logger.exception("subtask raw events failed: %s", sub_id)
            return {"events": events, "cursor": marks}

        return await anyio.to_thread.run_sync(job)

    def session_task_ids(self, session: Session) -> list[str]:
        """All root task ids under the session (workflow sessions have many; a
        regular session at most one)."""
        ids = [
            t["task_id"]
            for t in self._store.list_session_tasks(session.id)
            if t["task_id"]
        ]
        if session.task_id and session.task_id not in ids:
            ids.append(session.task_id)
        return ids

    async def task_transcript(
        self, task_id: str, include_tools: bool = False
    ) -> str:
        """The task's conversation transcript (material for handoff
        generation); returns an empty string when unavailable.

        include_tools=True appends tool-call summaries (for generating a
        complete handoff document).
        """
        import anyio

        return await anyio.to_thread.run_sync(
            self.task_transcript_sync, task_id, include_tools
        )

    def task_transcript_sync(
        self, task_id: str, include_tools: bool = False
    ) -> str:
        """The synchronous form of task_transcript (the channel_read_topic
        tool calls it on a worker thread)."""
        from noeta.agent.workflow.transcript import build_transcript

        if self._client is None or not task_id:
            return ""
        return build_transcript(
            self._replay_single(task_id, None), include_tools=include_tools
        )

    async def delete_session(self, session: Session) -> None:
        """Lightweight deletion: remove only the session record (it disappears
        from the sidebar), keeping the noeta trace data.

        Differences from the old version:
        - **Does not call** ``client.delete_task``: noeta EventLog /
          ContentStore / Dispatcher state is fully preserved; the admin Trace
          page can still inspect the complete execution detail by task_id.
        - Container release + workspace cleanup go through the anyio thread
          pool (not the ``_submit`` serial queue), blocking neither the event
          loop nor getting stuck behind other seed jobs.
        - In-memory mappings (_task_to_session / _subtask_ids etc.) are
          cleaned immediately to prevent leaks.
        """
        import anyio

        sid = session.id
        # 0. Collect the task_ids to clean first (store.delete also removes
        #    the session_tasks rows, so this must run before the DB delete;
        #    the noeta host side accounts exec envs by root task)
        task_ids: list[str] = [t for t in self.session_task_ids(session) if t]

        # 1. Delete the session record immediately: the sidebar / list API
        #    stop returning it and the user perceives it as deleted
        self._store.delete(sid)

        # 2. Clean the in-memory mappings (root + subtasks)
        for tid, mapped_sid in list(self._task_to_session.items()):
            if mapped_sid == sid:
                self._task_to_session.pop(tid, None)
                self._subtask_ids.discard(tid)
                self._subtask_root.pop(tid, None)
                self._terminal_tasks.discard(tid)
        self._session_to_user.pop(sid, None)
        self._title_failed.discard(sid)

        def _cleanup_artifacts() -> None:
            # Release the per-session container; the trace in the noeta DB is
            # unaffected. At shutdown client.teardown_exec_env backstops
            # everything.
            if self._sandbox_enabled:
                for tid in task_ids:
                    try:
                        self._client._host.release_exec_env(tid)  # noqa: SLF001
                    except Exception:  # noqa: BLE001 - already released / missing is idempotent
                        logger.debug(
                            "release_exec_env failed (continuing): %s", tid
                        )
                # Backstop: containers are named and shared per session, and
                # the per-task release refcounts can be incomplete after a
                # process restart — tear down directly by session id; with the
                # session deleted the container must go (force_release is
                # idempotent).
                provider = getattr(self, "_sandbox_provider", None)
                if provider is not None:
                    try:
                        provider.force_release(sid)
                    except Exception:  # noqa: BLE001 - best effort
                        logger.debug("force_release failed: %s", sid)
                # The preview mount goes down with the container (its tokens
                # invalidate immediately)
                if self._preview_gateway is not None:
                    self._preview_gateway.unmount_session(sid)
            # Delete the workspace directory (agent output files, not the
            # trace)
            ws = self._settings.workspaces_path / sid
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)

        # Run via the anyio thread pool: neither blocks the event loop nor
        # goes through the _submit serial queue (the old implementation's
        # await self._submit(job) could get stuck behind other sessions' seed
        # jobs).
        await anyio.to_thread.run_sync(_cleanup_artifacts)
