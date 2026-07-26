# Sandbox execution backend: the ExecEnv seam + an AIO Sandbox backend (v1: shell + file isolation)

> **Status: Shipped** — the v1 `ExecEnv` seam landed (`noeta/tools/fs/exec_env.py`) and was
> then evolved by the per-session v2 spec. Durable decisions:
> [execution-environment-seam.md](../../adr/execution-environment-seam.md).

## Goal

Open an `ExecEnv` seam **underneath** noeta's fs/shell tools, lifting "where do
file/process side effects land" out of the tool implementations: `LocalExecEnv`
by default (today's behaviour — host subprocess plus the `WorkspaceRoot`
realpath fence, zero behaviour change), and optionally `AioSandboxExecEnv`
(routing shell/file to an AIO Sandbox container, `agent-infra/sandbox`,
Apache-2.0, a single container exposing `POST /v1/shell/exec` + `/v1/file/*`).
The tools' **model-facing contract (name / schema / description) is entirely
unchanged**; only the execution backend swaps — which satisfies the
stable-prefix KV-cache reproducibility hard constraint by construction.

Multi-machine requirement: after a worker crash / stale-reclaim, another machine
folding the task back must **reconnect to the same sandbox** (endpoint + id land
in the log; the key comes from host config and never enters the log).

## Non-goals

- **No browser / computer-use.** AIO's CDP browser, VNC and Jupyter are all out
  of scope; v1 does shell + file isolation only.
- **No fencing of cross-generation sandbox side effects in v1** (see Decisions
  D1). An orchestration-layer generation-token fence is explicitly deferred to v2.
- **No sandbox cluster orchestration.** v1 assumes a reachable provisioning
  authority (a single backend process provisions); distributed scheduling where
  "several backend hosts share a sandbox pool" is out. Cross-machine **reconnect**
  is in (via the ref in the log); cross-machine **provisioning topology** is not.
- **No background shell inside the sandbox** (`run_in_background`). AIO has no
  durable job handle, so in sandbox mode v1 returns a clear "not supported in
  sandbox (v1)" error for `run_in_background=True` (see D5).
- **No sandbox pause/snapshot or idle reuse.** AIO has no such capability; in v1
  a container's lifetime is bound to the root task, and the idle cost is recorded
  in known-limitations (see D6).
- **No change to EventLog event bytes, to the fencing ADR's D1–D3, or to the
  Dispatcher / Engine main loop.**

## Context

- **`WorkspaceRoot` (`noeta.tools.fs._workspace`) is only a path fence, not a
  sandbox.** It does `os.path.realpath` containment against the host, and its
  docstring says plainly that "a tool that invokes external processes
  (`shell_run`) can still touch the rest of the filesystem". So this seam is
  **another layer**, sitting between the tools and the real IO.
- **The word "Workspace" is already taken twice**: once as the first-class
  registered entity `{id, name, path}` (the agent layer,
  `~/.noeta/workspaces.json`, see `workspace-and-session-path.md`), and once as
  the `WorkspaceRoot` path fence. So the new seam gets its **own name,
  `ExecEnv`**, to avoid a third overload. [decided on your behalf; overridable:
  the name]
- **The session path model** (`workspace-and-session-path.md`): a session pins one
  absolute path, written into `TaskHostBoundPayload.workspace_dir`; resume reads
  only that path and never touches the registry; `provider` puts only the **name**
  in the log, never the key or the connection instance. **This design's sandbox
  ref reuses that pattern exactly** (addressing in the log, secret in host
  config).
- **The core premise of the fencing ADR** (`multi-host-lease-fencing.md`, line
  42): "there is no write in the system, outside the shared Postgres, that
  depends on lease currency for correctness" — which is why no epoch/fencing
  token is needed. **A sandbox breaks exactly that premise**: it is an external
  resource on Kleppmann's boundary, and the ADR's alternative #1 already
  forecast that "an epoch only becomes load-bearing once a write lands outside
  the shared database". That is what D1 addresses head-on.
- **A worker leases one stretch at a time** (`worker-lease-model.md`): lease →
  advance to the next suspend/terminal → release. A tool call must complete
  within one lease (heartbeat renews it, hard cap 1h). → foreground shell inside
  the sandbox is naturally bounded by this, which is also what makes it safe for
  v1 to drop background.
- **File checkpoint / rewind**
  (`conversation-rewind-and-file-checkpoint.md`): the
  `_capture_file_baselines` choke point in `ToolRuntime.invoke` reads the
  "content before the edit" into the ContentStore, and rewind writes it back to
  **disk**. Under sandbox mode "disk" = the container FS, so both the capture read
  and the rewind write must go through ExecEnv (see D7).
- **The injection point already exists**: `ToolRuntime.__init__` already injects
  `background_runner` / `file_checkpoint_registry` by the same pattern, and
  `invoke` constructs `ToolContext(artifact_store, metadata, background_runner)`.
  `ExecEnv` just follows. `HostConfig` (`noeta.client.host_config`) is a frozen
  dataclass where every field's "absent default = today's behaviour", so adding a
  sandbox config field breaks nothing.

## Decisions

### D1 [core, settled] Cross-generation sandbox side effects: v1 accepts at-least-once, does not fence, and records it in known-limitations

A zombie worker fenced out of the EventLog by D1 (the in-tx `FOR SHARE`) can
still send `exec` / `file` writes to the same sandbox — outside any Postgres
transaction, where D1 cannot reach; and AIO has no session-takeover mechanism.
v1's position:

- **A sandbox side effect is an external side effect, at-least-once, unfenced** —
  the **same category** as the existing "a crashed step's side effects are
  reported, not rolled back" (`step-attempt-recovery.md` / limitations).
- After a reclaim, the new worker reconnects to the same container and carries on.
  The window in which a **slow zombie (revived from a GC pause / SIGSTOP) pollutes
  the container** is backstopped by step-attempt re-drive plus human review, and
  is **written into known-limitations**.
- **The v2 hole (pre-announced)**: an orchestration-layer generation-token fence —
  the sandbox manager holds a per-task generation token, `exec` / `file` carry the
  token, a controlled proxy in front of AIO validates it, and stale-reclaim
  rotates it. That is precisely the moment the fencing ADR describes as "the epoch
  becomes load-bearing". **v1 does not build it, but reserves the interface** (the
  ExecEnv connection handshake carries an opaque `fence_token: str | None`,
  always `None` in v1, filled with the generation token in v2 — so v2 does not
  reshape the seam).

> This is a long-term trade-off someone will later demand a reason for ("why is
> the sandbox not fenced like every other write?") → **it should get an ADR**
> (`execution-environment-seam.md`), pinning the D1/D2 trade-offs and explicitly
> linking back to `multi-host-lease-fencing.md`'s alternative #1.

### D2 Seam shape: an `ExecEnv` Protocol, injected alongside `ToolContext`, configured through `HostConfig`

- **A new `ExecEnv` Protocol** (landing in `noeta.protocols`), a minimal deep
  interface:
  - `run_shell(command: str, *, cwd: str, timeout_s: float, env: Mapping[str,str] | None) -> ShellResult`
    (foreground; `ShellResult = {exit_code, stdout, stderr, truncated}`)
  - `read_file(path: str) -> bytes`
  - `write_file(path: str, body: bytes) -> None`
  - `resolve(path: str) -> str` (containment / normalisation, returning a path
    canonical inside the backend; an escape raises `WorkspaceEscape`)
  - `close() -> None` (lifecycle teardown)
  - `glob` / `grep` / `list_dir` **do not enter the interface** — the fs tools
    express them above ExecEnv using `run_shell` (`rg` / `find`) + `read_file`,
    keeping the interface small.
- **Injection**: `ToolRuntime.__init__` gains
  `exec_env_resolver: Callable[[task_id], ExecEnv] | None` (`None` ⇒ construct a
  `LocalExecEnv`, zero behaviour change); `invoke` puts the resolved `ExecEnv`
  into `ToolContext.exec_env`. The fs/shell tools take it from `ctx.exec_env`
  instead of touching `os` / `subprocess` directly.
- **Config**: `HostConfig` gains `exec_env: ExecEnvConfig | None = None` (absent ⇒
  Local). `ExecEnvConfig` is a **pure config structure** constructible at the sdk
  layer (e.g.
  `SandboxExecEnvConfig(base_url: str, api_key_env: str = "SANDBOX_API_KEY", provision: "eager"|"attach")`),
  and **the runtime's builder instantiates the adapter from it** — the backend
  only fills in config and never imports an adapter, so the import-linter fence
  holds (the backend may only import `noeta.sdk`).

### D3 Layering

- **The `ExecEnv` Protocol + `LocalExecEnv` + the `AioSandboxExecEnv` adapter →
  runtime** (`noeta.runtime.exec_env`). An adapter-to-external-service belongs in
  the adapter layer, exactly like `noeta.storage.postgres`.
- **Sandbox manager / provision / teardown / lifecycle → the agent layer**
  (`noeta.agent.backend`, exactly like the workspace registry's "allocation &
  management lives in the agent layer"). It passes the config in through
  `HostConfig.exec_env`, the runtime builder constructs the adapter, and the
  manager is responsible for actually starting/destroying containers and handing
  the ref to the runtime.

### D4 Sandbox binding granularity + the ref in the log [decided on your behalf]

- **One sandbox binds one root-task tree** (per the rewind ADR, subtasks share the
  parent's cwd/disk → they must share the same container). Key = the session-root
  task id (reusing `ToolRuntime._session_root`'s existing root resolution).
- **Eager provisioning**: provision when the root task starts (at the host-bind
  point), with the **ref welded into `TaskHostBoundPayload`** (a new optional
  `exec_env_ref: {base_url, sandbox_id} | None`), reusing the existing weld+fold
  path and **adding no event type**. In Local mode `exec_env_ref=None` and
  `workspace_dir` is unchanged → old recordings stay byte-identical and fold is
  unchanged.
  - Eager over lazy: a coding agent almost always shells out, which saves the
    complexity of "a lazy first provision needs a new `ExecEnvBound` event".
- **The key never lands in the log**: the log stores only `base_url + sandbox_id`
  (addressing); `SANDBOX_API_KEY` is read from host config / env at connect time,
  exactly as a provider records only its name.
- **Reconnect**: resume/reclaim reads `exec_env_ref` → `AioSandboxExecEnv`
  connects to the same `sandbox_id` with `provision="attach"`. Any host that can
  reach that URL can reconnect → satisfying "another machine folding the task back
  reconnects to the same sandbox".

### D5 Background shell [decided on your behalf: dropped in v1]

Under sandbox mode, `shell_run(run_in_background=True)` **returns a clear error**
("background shell not supported in sandbox mode (v1)"). Reason: AIO has no
durable job handle, and noeta's existing background subsystem (the host
`ProcessRegistry` + growable artifact + PID recovery, see
`shell-permission-and-background.md`) is built entirely for the host; converting
it into a durable container job is an independent v2 effort. Foreground shell is
bounded by "complete within the lease" (already true) and is enough for v1.

### D6 Idle cost [decided on your behalf: accepted in v1]

AIO has no pause/snapshot. v1: container lifetime is bound to the root task's
active lifetime, with **teardown at root-task terminal + session close** (aligned
with background-shell's session-lifetime teardown); **no idle reaper** (reaping
would lose the container FS state, which is more surprising than helpful). The
**idle container cost goes into known-limitations**, and snapshot/pause is left to
v2.

### D7 The two fences must not fight + rewind goes through ExecEnv [decided on your behalf]

- **Under sandbox mode the container is the isolation boundary**, so
  `LocalExecEnv`'s host `os.path.realpath` fence **no longer runs** inside
  `AioSandboxExecEnv` (there is no host path to realpath). It is replaced by
  **normalisation of relative paths inside the container**: `resolve` confines the
  path to the container working directory and rejects `..` escapes (tidiness —
  don't let the model wander the container's `/`). The safety guarantee shifts
  from "the realpath fence" to "container isolation". **No double fence.**
- **Rewind**: `_capture_file_baselines`' read and the file_checkpoint restore's
  write-back move to `ctx.exec_env.read_file/write_file` (today they use `os`).
  Because edit/read already pass through ExecEnv at the same choke point, rewind
  **works with no special case**; baseline content is still stored in the
  ContentStore (pulled from the container's `/v1/file/read`).

### D8 Artifacts / stdout back to the ContentStore

The stdout `run_shell` returns is offloaded into the ContentStore as it is today;
files the tool produces are pulled back through `read_file` (→ `/v1/file/read`)
and stored as Artifacts (a `ContentRef`). Pure plumbing, no new mechanism.

### D9 The MCP prototype path (optional, throwaway)

AIO exposes `/mcp`. **Optional on day 0**: mount AIO's `/mcp` through the
existing live-MCP resolver to verify "the container works end to end" — a
**one-off verification, not part of this design**. It would introduce AIO's own
MCP tool names/schemas, perturb the stable prefix, and overlap the built-in
fs/shell tools, so it cannot be the end state.

## Implementation plan

1. **Protocol + Local backend (the zero-behaviour-change baseline)**: define
   `ExecEnv` / `ShellResult` (`noeta.protocols`); implement `LocalExecEnv`
   (`noeta.runtime.exec_env`) wrapping today's `WorkspaceRoot` + `_subprocess` +
   file read/write verbatim; add the `exec_env_resolver` injection to
   `ToolRuntime` and `exec_env` to `ToolContext`; move each fs/shell tool
   (read/glob/grep/edit/write/patch/shell) to take its IO from `ctx.exec_env`.
   **Acceptance**: the entire existing test suite green (Local mode byte-identical).
2. **The AIO adapter**: `AioSandboxExecEnv` (`run_shell`→`/v1/shell/exec`,
   `read/write_file`→`/v1/file/*`, `resolve` = in-container path normalisation,
   `fence_token` a placeholder fixed at `None`); an injectable HTTP transport
   (same pattern as `mcp_http_post` / `otlp_http_post`, tests pass a fake).
3. **Config + layer wiring**: `HostConfig.exec_env: ExecEnvConfig | None` + the
   sdk-layer `SandboxExecEnvConfig`; the runtime builder constructs the adapter
   from the config; the agent layer `noeta.agent.backend` gains a sandbox manager
   (provision/attach/teardown).
4. **Ref in the log + reconnect**: the optional
   `TaskHostBoundPayload.exec_env_ref` field + weld/fold; resume/reclaim reads the
   ref → attaches to the same container.
5. **Rewind through ExecEnv**: reroute the IO in `_capture_file_baselines` and the
   file_checkpoint restore to `ctx.exec_env`.
6. **Boundary handling**: a clear error for `run_in_background=True` under sandbox
   (D5); teardown hung on root-task terminal + session close (D6).
7. **Docs + ADR**: add two known-limitations entries ("sandbox cross-generation
   side effects are not fenced", "idle container cost"); a new ADR
   `execution-environment-seam.md`; add the `ExecEnv` term to `CONTEXT.md`.

## Task breakdown

| # | Task | Parallelisable? |
|---|---|---|
| T1 | `ExecEnv`/`ShellResult` Protocol + `ToolContext.exec_env` + the `ToolRuntime.exec_env_resolver` injection | the foundation; first |
| T2 | `LocalExecEnv` wrapping today's behaviour + rerouting each fs/shell tool to `ctx.exec_env` | depends on T1 |
| T3 | The `AioSandboxExecEnv` adapter (incl. the injectable HTTP transport + a fake) | depends on T1; parallel with T2 |
| T4 | `HostConfig.exec_env` + `SandboxExecEnvConfig` + runtime-builder instantiation | depends on T1/T3 |
| T5 | The agent-layer sandbox manager (provision/attach/teardown lifecycle) | depends on T4 |
| T6 | `TaskHostBoundPayload.exec_env_ref` weld/fold + resume/reclaim attach | depends on T4; the key to multi-machine reconnect |
| T7 | Reroute rewind (file_checkpoint capture/restore) through ExecEnv | depends on T2 |
| T8 | Boundaries: the background error + teardown on the lifecycle | depends on T5 |
| T9 | Docs + ADR + CONTEXT term + known-limitations | wrap-up; depends on everything |

## Dependencies / sequencing

- **T1 → T2 is a zero-behaviour-change refactor** and must land with the full test
  suite green before the sandbox is discussed at all (this is the rollback
  insurance: at any point, a default config returns you to Local).
- **T3 can run in parallel with T2** (one wraps the old behaviour, the other
  writes the new adapter; T1 has already fixed the interface).
- **T6 is where "multi-machine reconnect" correctness lands**; it depends on T4's
  ref structure and is accepted separately.
- **T7 (rewind) depends on T2** (it comes along for free once the choke point is
  rerouted).
- T9 wraps up and needs the final shape of T1/T5/T6.

## Acceptance criteria

1. **Zero regression**: with a default `HostConfig()` (Local mode), the entire
   existing suite is green; old recordings fold/replay byte-identically
   (`exec_env_ref=None` does not change `TaskHostBound`'s bytes).
2. **Stable prefix unchanged**: before and after, the same agent's serialised tool
   schema bytes are identical (the sandbox swaps only the execution backend, never
   name/schema/description).
3. **The sandbox works**: with a `SandboxExecEnvConfig` configured, the side
   effects of `shell_run` / `read` / `write` / `edit` / `apply_patch` / `glob` /
   `grep` land inside the AIO container and the host FS is untouched; artifacts are
   stored through the ContentStore.
4. **Multi-machine reconnect (core)**: a worker starts work in sandbox-X →
   `kill -9` → another process/host folds the task back, reads `exec_env_ref` and
   **attaches back to the same sandbox-X** (same `sandbox_id`), the existing file
   state inside the container is visible, and the task continues. The integration
   test uses two dispatcher instances sharing one DSN (reusing the fencing contract
   suite's multi-machine fixture) plus a real AIO container (gated on
   `NOETA_TEST_AIO_SANDBOX_URL`).
5. **Rewind under sandbox**: rewinding a file the AI edited under sandbox mode
   writes back inside the container and restores correctly.
6. **Clear boundaries**: `run_in_background=True` under sandbox returns a clear
   not-supported error; the container is torn down after root-task terminal +
   session close.
7. **known-limitations** gains two entries (cross-generation side effects
   unfenced, idle cost); the ADR + the `CONTEXT.md` `ExecEnv` term land.

## Risks

- **R1 (known and accepted) cross-generation side effects are unfenced**: a slow
  zombie pollutes the container. Mitigation = step-attempt re-drive + human review;
  v2 adds an orchestration-layer token fence (the interface already reserves
  `fence_token`).
- **R2 AIO API contract drift**: `/v1/shell/exec` and friends are the documented
  v1 surface and their fields may evolve; an injectable HTTP transport plus one
  thin adapter isolates this, so a contract change touches only the adapter.
- **R3 Provisioning topology**: v1 provisions from a single backend; a shared pool
  across multiple backend hosts is not built — if multi-host backends really
  arrive, the provisioning authority needs its own design (already listed as a
  non-goal; do not let it spread silently).
- **R4 Idle container cost**: a long suspend (waiting hours on a human or a timer)
  holds a container. v1 records the limitation; v2 gets pause/snapshot or an idle
  reaper.
- **R5 grep/glob via `run_shell` depend on `rg` / `find` existing in the
  container**: the AIO image needs to ship them (it very likely does); the adapter
  should degrade gracefully or error clearly when it detects them missing.

## Implementation notes (2026-07-07 — T1→T2 landed)

T1→T2 (the zero-behaviour-change foundation) is implemented and verified (full
suite 3003 passed / 0 failed, import-linter 16 kept 0 broken, mypy/ruff clean,
schema snapshot unchanged). Three **implementation-detail corrections** to the
above were made while landing it (the end state is unchanged; recorded here for
whoever picks up T3):

1. **The landing spot moved from `noeta.runtime` to `noeta.tools.fs.exec_env`.**
   In the import-linter topology `noeta.tools` is in the materials band and sits
   **above** `noeta.runtime` (the kernel-services band), and a kernel may not
   import a material → `LocalExecEnv` (which wraps `WorkspaceRoot` / `run_argv`)
   can only live at their layer. D3's original "Local/AIO → runtime" is void;
   `AioSandboxExecEnv` (T3) lands in `noeta.tools.fs` too.
2. **The seam is IO-only, injected as a tool construction-time field, not via
   `ctx.exec_env`.** In the existing architecture `workspace` is already a
   construction-time `WorkspaceRoot` field on the tool
   (`build_fs_tools` / `_stage_fs_pack` construct per spec), so ExecEnv mirrors it:
   each tool gains `exec_env: ExecEnv = field(default_factory=LocalExecEnv)`, whose
   default is today's behaviour (95 `Tool(workspace=…)` test constructions need
   zero changes). **Path resolution (resolve/relative/root/skill_roots) stays in
   `WorkspaceRoot`, untouched**; only genuine IO (read/write/stat/walk/run_argv)
   goes through `self.exec_env`. `ToolRuntime` / `ToolContext` are **unchanged**. →
   T3's D7 "the container is the fence" is achieved by pointing the sandbox's
   `WorkspaceRoot` root at the container working directory (lexical
   containerisation), with IO going remote through `AioSandboxExecEnv`. → T5/T6's
   per-task sandbox binding: `_stage_fs_pack` already runs per task-spec, so at
   that point it constructs a per-task `AioSandboxExecEnv` from `exec_env_ref` and
   passes it to `build_fs_tools` (which needs an optional `exec_env` parameter;
   T1→T2 did not add one and used the default).
3. **Two IO sites stay inline for now, to be routed in T3**: (a) `apply_patch`'s
   atomic `create` (`os.open O_EXCL` / `_write_all` / `os.close`, with tiered
   open→none / write·close→delete recovery) — a genuine mismatch with AIO's
   single-shot `file/write` API, and the tests monkeypatch at the
   `os.open/write/close` level and depend on the precise reason; route it when T3
   defines the sandbox's create recovery contract (possibly introducing 3 typed
   exceptions). (b) The shell allowlist file
   (`.noeta/shell-allowlist.json`)'s `read_text` / `write_text` / `mkdir` — it is
   governance config, excluded by a non-goal; how to handle it under sandbox is
   left for later. The `ExecEnv` interface currently contains **only the methods
   actually called** (read_bytes / read_text / write_bytes / unlink / exists /
   is_file / is_dir / is_symlink / glob / rglob / run_argv);
   `create_exclusive` / `mkdir` are added when T3 needs them, to avoid dead code.

## Implementation notes (2026-07-07 — T3 landed: `AioSandboxExecEnv`)

T3 (the AIO adapter) is implemented and unit-tested (fake transport, 29 tests;
the full fs suite green, import-linter 16 kept 0 broken, mypy/ruff/naming clean).
It landed in `noeta.tools.fs.exec_env` (the same file as `LocalExecEnv`). Key
points and the implementation decisions taken against the spec:

1. **Injectable HTTP transport**:
   `AioHttpPost = Callable[[url, json_bytes, headers], bytes]` (mirroring the
   `otlp_http_post` shape), defaulting to stdlib `urllib`; tests pass a fake and
   never open a socket. **Real-container end-to-end stays gated**
   (`NOETA_TEST_AIO_SANDBOX_URL`; not runnable this round without a container) —
   the unit tests pin only "the shape of the wire contract", and a contract drift
   is a single-file change (the R2 isolation layer is this adapter).
2. **Wire mapping (all locked inside the adapter)**: `run_argv`→`/v1/shell/exec`,
   with command = `cd <shlex.quote(cwd)> && shlex.join(argv)` (cwd via a lexical
   `cd`, rather than betting on an unverified request field); AIO merges
   stdout+stderr into a single `output` → it lands in `stdout` and `stderr` is
   always empty. **Byte fidelity**: the read request uses `encoding=base64` and
   decodes; writes send base64 (edit/patch rely on bytes for their TOCTOU hash,
   which is the most contract-sensitive spot here). `glob`/`rglob` use shell
   `globstar` (`rglob(pat) = glob("**/" + pat)`, matching pathlib's definition;
   depends on the image having `bash` + globstar, R5); stat uses
   `test -e/-f/-d/-L` and reads the exit code; `unlink` uses `rm`.
3. **Error mapping**: a `success=false` response's `data.error_type` maps to a
   stdlib `OSError` subclass (not_found→`FileNotFoundError`,
   permission_denied→`PermissionError`, already_exists→`FileExistsError`,
   everything else→`AioSandboxError(OSError)`), so the tools' `except OSError`
   branches stay backend-agnostic. Transport faults normalise to
   `AioSandboxError`; `TimeoutError` passes through to `run_argv` so it can flag
   `timed_out`. A remote fault in `run_argv` is **reported as a failed run
   (returncode=-1) rather than crashing the worker**, mirroring how local
   `run_argv` never lets a spawn fault escape.
4. **patch-create is now routed** (delivering T1→T2 note #3(a)): a new
   `ExecEnv.create_exclusive(path, body)` plus 3 typed exceptions
   `ExclusiveCreate{Exists,Failed,WriteFailed}(OSError)`, each carrying `.recover`
   (`none`/`delete`) and `.reason` (the old inline wording preserved verbatim).
   `LocalExecEnv.create_exclusive` is the `os.open O_EXCL` / `_write_all` /
   `os.close` dance moved over unchanged (the `os.open/write/close` points the
   patch tests monkeypatch are unchanged; 28 tests green);
   `AioSandboxExecEnv.create_exclusive` emulates exclusivity with a
   `set -C; : > path` noclobber gate (AIO has no O_EXCL), writing the body in
   base64 once the gate is won; a body-write failure → `WriteFailed`
   (recover=delete). `patch.py`'s create branch collapses to
   `except ExclusiveCreateError → self._fail(recover=exc.recover, reason=exc.reason)`;
   `_write_all` moved into `exec_env.py` and `patch.py` dropped its `os` /
   `contextlib` imports.
5. **`timeout_s` (run_argv's tool timeout) does not hard-kill the remote in v1**:
   AIO's exec has no hard kill, so the adapter's HTTP `timeout_s` (set at
   construction) is the only boundary; the real bound is the lease heartbeat + the
   1h cap (D1/limitations). `fence_token` stays `None` as a placeholder and v1
   sends no fence header (D1; v2 rotates it at stale-reclaim).
6. **Still pending T4**: `AioSandboxExecEnv` can currently only be constructed by
   hand — there is no config entry point, and `WorkspaceRoot` is still the host
   `realpath` fence (the sandbox needs a **lexical** `WorkspaceRoot`: rooted at the
   container workdir, rejecting `..` escapes, never touching the host FS). **It
   only becomes usable once T4 is done.** The shell allowlist file (note #3(b)) is
   still inline and undecided.

## Implementation notes (2026-07-07 — T4 landed: config + tool-builder wiring)

T4 connects the seam along the "config + tool builder" line, making the sandbox
backend **reachable** (full suite 3050 passed / 0 failed, import-linter 16 kept 0
broken, no new mypy errors, ruff/naming clean, schema snapshot unchanged). **The
per-task provision/attach lifecycle is still T5/T6** — T4 only lays the reachable
seam for "given an `ExecEnv`, how does it get wired into tool assembly", not "who
provisions the container".

1. **Lexical `WorkspaceRoot.for_container(dir)`** (`_workspace.py`): a new
   `lexical: bool = False` field (default False = today's host `realpath`
   behaviour, so every existing construction is byte-equivalent). `for_container`
   does only `os.path.normpath` (never touches the host FS, never checks
   existence, requires an absolute path), and when `lexical`, `resolve` collapses
   `..` / `.` via `normpath` instead of `realpath` — the container is the isolation
   boundary (D7) and this layer is only a tidiness fence.
2. **`build_fs_tools(exec_env=None)`** (`fs/__init__.py`): `None` ⇒ build one
   shared `LocalExecEnv` (stateless, documented as shareable, byte-equivalent to
   each tool's own `default_factory`); non-`None` ⇒ the whole pack uses the
   injected backend. No tool's name/schema/description changes → the stable prefix
   is untouched.
3. **`build_session_inputs(exec_env=None)` → `_stage_fs_pack`**
   (`execution/builder.py`): mirrors how "wiring-only runtime injections" like
   `app_gateway` land (a `_BuildSpec` field, inert default, not session identity).
   `spec.exec_env is None` ⇒ `WorkspaceRoot.from_path` (host); non-`None` ⇒
   `WorkspaceRoot.for_container` (a lexical container root) — i.e. "a remote
   executor ⟺ the workspace is a container path".
   `test_default_host_byte_equal_to_direct_builder` stays green (exec_env does not
   enter the schema).
4. **`SandboxExecEnvConfig` + `HostConfig.exec_env`** (sdk `host_config.py`): pure
   addressing config (`base_url` / `api_key_env` / `provision`), with
   `resolve_api_key()` reading the env only at connect time (the key never enters
   config or the log, D5). **The adapter factory does not land in the tools layer**
   (that would violate import-linter: `noeta.tools` may not import
   `noeta.client`) — the host holding the config (`noeta.client` /
   `noeta.agent.host`, above tools) reads the env, constructs the
   `AioSandboxExecEnv`, and passes it to `build_session_inputs(exec_env=…)`. That
   step is T5/T6.
5. **Fixed one host stat T1→T2 missed**: `read` / `edit`'s existence checks go
   through the shared helpers `resolve_{readable,existing}_file`
   (`tools/_invocation.py`), whose `resolved.is_file()` previously **hit the host
   FS directly**, bypassing the seam — which would necessarily fail under sandbox.
   Both helpers gained an `exec_env` parameter (default `None` → host,
   byte-equivalent), and `read.py` / `edit.py` pass `self.exec_env`. **Audit
   residue** (grep-confirmed): `shell.py`'s allowlist-file `read_text` (note
   #3(b) — governance config, a non-goal) and `skill_script.py`'s `read_bytes`
   (skill scripts, not the core fs pack) still use the host; how to handle them
   under sandbox is left for later.
6. **Added tests**: `test_exec_env_wiring.py` (18) covering the lexical
   workspace / `build_fs_tools` injection / config / the reachable
   `build_session_inputs` seam (including a fake `RecordingExecEnv` proving a read
   genuinely flows through the backend); `test_shell_run_foreground.py` (4) really
   runs a foreground shell and asserts exit_code + stdout content (covering the
   extra action item from the handoff — mutating `run_argv.stdout` fails 3 of them,
   proving they truly bite the seam).

## Implementation notes (2026-07-07 — T5 landed: host-layer sandbox manager)

T5 connects "config → build a live backend → feed it to `build_session_inputs`",
making the sandbox backend **actually run** (full suite 3066 passed / 0 failed,
import-linter 16 kept 0 broken, no new mypy errors [the 3
`content_hashes` / `InteractionDriver` errors in host.py/client.py were confirmed
pre-existing by stash comparison, with only line-number shifts], ruff/naming
clean, schema snapshot + `default_host_byte_equal` still green). Landing spots and
implementation decisions against the spec:

1. **The manager lands in `noeta.client` (not D3's original
   `noeta.agent.backend`)**: a new `noeta/client/sandbox.py::SandboxExecEnvManager`.
   The only call site that must receive the live backend is
   `SdkHost._build_engine → build_session_inputs`, and `SdkHost` is in
   `noeta.client`; having the host hold the manager directly (like
   `_process_registry`) is cleaner than threading one more injected callable down
   from the product layer, and import-linter stays clean (`noeta.client` is above
   `noeta.tools` and may import `AioSandboxExecEnv`). This delivers T4 note #4,
   "the host holding the config constructs the adapter".
2. **[deviates from D4 — required reading, affects T6] v1 = one shared container
   per host, with no per-root keying.** D4's ideal is "one sandbox per root-task
   tree, keyed by the session-root task id, eagerly provisioned at host-bind, with
   the ref welded into `TaskHostBoundPayload`". But: (a) that per-root ref's
   weld/fold + reconnect **is itself T6**, and is also the **only** point where a
   per-root key is available — the **seed engine that writes `TaskCreated` has
   `task_id=None`**, and it **shares the engine cache** with the first driving turn
   (the key does not include task_id, see `resolver._engine_for_agent`), so
   switching the backend per root inside `_build_engine` alone would be **silently
   bypassed by the cache** (the seed builds Local, the drive reuses Local → the
   sandbox never takes effect). (b) The AIO v1 API surface in use
   (`/v1/shell/exec` + `/v1/file/*`) **has no container-creation endpoint**,
   "cluster orchestration" is a non-goal, and v1's `base_url` in practice addresses
   **one** external container. So in T5 the manager lazily builds **one** shared
   `AioSandboxExecEnv` and the seed and every driving turn get **the same one** —
   which both satisfies "subtasks share the parent's container" (everything shares)
   and eliminates the seed/drive cache collision. **The cost (recorded as a v1
   known-limitation, written in T9)**: two concurrent sessions on the same host
   share one container working directory, with no per-root isolation — per-root
   isolation arrives with T6's per-root provisioning.
3. **`SandboxExecEnvConfig` gains `workdir: str = "/workspace"`**
   (`host_config.py`): the container working directory. Under sandbox mode the
   host `workspace_dir` is meaningless inside the container, so `_build_engine`
   overrides it with `workdir`, which becomes the root of the **lexical container
   `WorkspaceRoot`** (D7). Pure addressing, the same nature as the other fields. (A
   per-session workspace subdirectory under sandbox is left to T6/v2.)
4. **`SdkHost` wiring**: a new public field
   `exec_env: Optional[SandboxExecEnvConfig]` (the Client threads it in from
   `hc.exec_env`) plus a `_sandbox` runtime accelerator (`__post_init__` builds the
   manager only when the config is non-`None`, otherwise `None` ⇒ the local path is
   byte-identical). `_build_engine`:
   `if self._sandbox: session_exec_env = self._sandbox.exec_env(); workspace_dir = Path(self._sandbox.workdir)`,
   then passes `exec_env=session_exec_env` into `build_session_inputs`.
   `_build_orchestration_engine` (the `__workflow__` child) **does not** get the
   sandbox — it has `allowed_tools=()` and no fs tools, and its workers each go
   through `_build_engine` and get the sandbox there.
5. **Teardown seam (partially delivering D6; full mounting is T8)**:
   `SandboxExecEnvManager.teardown()` (`eager` → best-effort `close()`; `attach`
   only drops the handle without closing — the container belongs to whoever
   provisioned it) + `SdkHost.teardown_exec_env()` + `Client.shutdown()` calling it
   (collecting container connections at process exit, preventing an idle leak).
   **Per-root teardown at root-task terminal = T8** (under v1's single shared
   container, tearing down per root would hit other roots, so it is correctly left
   until per-root containers exist).
6. **Added tests**: `test_sandbox_host_wiring.py` (12) — manager
   lazy/shared/idempotent-teardown/eager-vs-attach close; SdkHost's default =
   LocalExecEnv + host root (regression); with a config ⇒ the fs tools use the
   container backend and a lexical container root = `workdir`; a real
   `AioSandboxExecEnv` (no socket); a read flowing end-to-end through the fake
   backend; **the seed (`resolve_engine_for_agent`, `task_id=None`) routes to the
   same backend too** (pinning #2's cache-collision safety); the Client threading
   the config + shutdown collecting the backend. The fake factory is injected by
   monkeypatching `sandbox._default_factory`, so no socket is ever opened.

## Implementation notes (2026-07-07 — T6 landed: durable exec_env_ref + multi-machine reconnect)

T6 completes the multi-machine semantics of "the container address lands in the
log with the session → another machine folding it back reconnects to the same
container" (full suite 3070 passed / 0 failed, import-linter 16 kept 0 broken,
protocols mypy `--strict` clean, no new mypy errors in the changed files [the 9
`ResidentHost` / `EngineProtocol` errors in driver/resolver were confirmed
pre-existing by a line-normalised stash diff], ruff/naming clean, schema snapshot
+ `default_host_byte_equal` still green). The landing strictly mirrors the
existing `workspace_dir` chain — weld → fold → resolve → cache key ("prefer
existing patterns"):

1. **`exec_env_ref` is a flat `Optional[str]` (the container `base_url`), not the
   spec's `{base_url, sandbox_id}`.** [deviates from D4, continuing T5 note #2]
   Reason: T5 already settled "v1 = one container per host, addressed by
   `base_url`", and `sandbox_id` only becomes independent of `base_url` in v2 when
   real orchestration mints per-container ids; in v1, recording `base_url` **is**
   the reconnect address and the only load-bearing part. A flat `str` aligns
   perfectly with `workspace` (same type, same `__canonical_omit_none__` idiom,
   isomorphic cache key) and **avoids** the nested-dataclass canonical
   tag/register/restore machinery. `sandbox_id` can be derived from `base_url`
   when needed (for logs). Recorded as a v1 known-limitation (T9).
2. **The durable chain (mirroring `workspace_dir` point by point)**:
   `TaskHostBoundPayload.exec_env_ref` (`events.py`, added to
   `__canonical_omit_none__` → old recordings byte-identical) →
   `GovernanceState.exec_env_ref` (`task.py`) → fold `_on_task_host_bound`
   (`fold.py`) → resolver `_bound_exec_env_ref_for` +
   `resolve_engine` / `resolve_engine_for_agent` threading the parameter →
   `_engine_for_agent`'s **9th cache-key dimension** (`None` default =
   byte-equivalent for non-sandbox) → the `_build_engine` abstract signature + the
   SdkHost implementation. **Subtask inheritance**: a subtask has no
   `TaskHostBound` of its own (its `governance.exec_env_ref` folds to `None`), so
   `_build_drain_host` gains
   `inherited_exec_env_ref = _bound_exec_env_ref_for(parent)` threaded into the
   child build — the same approach as `inherited_workspace` (D4: subtasks share the
   parent's container).
3. **The weld is in `driver.seed_start`**:
   `session_exec_env_ref = getattr(host,"exec_env_ref",None)()` (double-guarded on
   the host; a test double lacking the method → `None`, so the local path is
   byte-identical) → both `resolve_engine_for_agent(exec_env_ref=…)` (so the seed
   engine agrees with the ref about to be written) and merged into
   `TaskHostBound`. The weld condition relaxes from `if workspace_dir` to
   `if workspace_dir or session_exec_env_ref` (so the ref is recorded even when the
   sandbox has no per-session workspace). `SdkHost.exec_env_ref()` →
   `_sandbox.current_ref()` (= `config.base_url`) or `None`.
4. **The reconnect is in `SdkHost._build_engine`**:
   `session_exec_env = self._sandbox.exec_env(base_url=exec_env_ref)` — a non-`None`
   `exec_env_ref` (the recorded address read back on resume/reclaim) ⇒ connect to
   **that** base_url; `None` (seed / non-session) ⇒ the config default. **The key
   always comes from this machine's config env (D5), never from the ref.** The
   manager is upgraded from a single instance to a **per-base_url cache**
   (`_by_url`): `exec_env(base_url=None)` uses the config default, and reconnect
   passes the recorded ref; across hosts the recorded ref may differ from this
   machine's config, so a new adapter is built with
   `dataclasses.replace(config, base_url=ref)`. `teardown` closes every cached one.
5. **Reclaim needs zero changes**: a stale lease is requeued → any worker does
   `fold` + `resolve_engine(task)` (the same path that reads
   `governance.exec_env_ref`), the recorded ref is rebuilt automatically →
   transparent reconnect. **Nothing beyond cross-`base_url` isolation was added to
   the cache key** — v1 is one host with one config base_url, so two sessions on a
   host have identical refs; the only collision is "one machine folding two refs
   from different deployments" (an extreme case, recorded as a limitation).
6. **Added tests**: `test_sandbox_exec_env_ref.py` (4) — weld+fold (`seed_start`
   writes `TaskHostBound.exec_env_ref` and it folds into governance); a
   non-sandbox session records no ref (byte-equivalence); the **multi-machine
   reconnect acceptance** (host A bound to `http://A:1111` starts a session → host
   B [SAME event log, config `http://B:2222`] folds + `resolve_engine` → the fs
   backend connects to **A**, not B, proven by the fake factory recording the
   base_url); `exec_env_ref` is a cache dimension (same ref reuses the engine,
   different refs split). A real container is still gated
   (`NOETA_TEST_AIO_SANDBOX_URL`).

## Implementation notes (2026-07-07 — T7 landed: rewind restore routes through ExecEnv)

T7 makes rewind under sandbox write baselines back to the **container** rather
than the host (full suite 3076 passed / 0 failed, import-linter 16 kept 0 broken,
no new mypy errors in the changed files [line-normalised stash diff = ∅],
ruff/naming clean, the existing `test_rewind_fold.py` regression green).

1. **[blocker dissolved — differs from the handoff's assumption] the capture side
   already goes through exec_env, so `ToolRuntime` needs no change.** The handoff
   recorded "T7 blocker: exec_env is a per-tool field and the runtime choke point
   `_capture_file_baselines` cannot reach it". Reading the code: 
   `_capture_file_baselines` **does not read from disk** — it reads
   `result.file_changes[*]["before"]`, and those pre-edit bytes were **read and put
   there by the writing tool** (edit/write) **using its own `self.exec_env`**
   (rerouted in T2). So capture is **already correct** under sandbox,
   `ToolRuntime` needs zero changes, and `ctx.exec_env` is still unnecessary (the
   rejection in T1→T2 note #2 continues to hold). The only side that genuinely
   needed rerouting is **restore**.
2. **The restore side = `driver._restore_files`** (the only place hitting host
   pathlib directly): it used to do `root = host.workspace_dir_for(gov.workspace)`
   plus `target.exists()/unlink()/parent.mkdir()/write_bytes()`. The rerouting
   source is not a per-tool field, nor ctx — it is **T6's ref**:
   `host.exec_env_for_ref(gov.exec_env_ref) -> (ExecEnv, container_root) | None`.
   Non-`None` (a sandbox session) ⇒ write back using the container backend + the
   container workdir; `None` (local / no sandbox / a test double) ⇒ **the original
   pathlib branch is preserved verbatim** (zero regression — rewind is a delicate
   area, so the two branches are not merged; only a bypass is added). The key still
   comes from this machine's config env (D5).
3. **`ExecEnv` gains `mkdir(path)`** (with `parents=True` / `exist_ok=True`
   semantics): restore needs it for "recreate a directory the rewound span
   deleted", delivering T1→T2 note #3's "add `mkdir` when it is needed".
   `LocalExecEnv.mkdir` = `Path.mkdir(parents, exist_ok)`;
   `AioSandboxExecEnv.mkdir` = `mkdir -p` (via `_shell`, checking the exit code,
   like `unlink`). `ExecEnv` is a tools-layer Protocol (not in
   `noeta.protocols`), and every isinstance check is against a concrete class
   rather than the Protocol, so adding a method breaks no existing fake.
4. **Added tests**: `test_sandbox_rewind.py` (6) — `exec_env_for_ref`
   (sandbox+ref → (backend, container root); local / ref=None / no sandbox →
   `None`); `_restore_files` called directly: a content_ref baseline → container
   `write_bytes` + `mkdir(parent)`, host untouched; content_ref=None → container
   `unlink`; **local (ref=None) rewind still writes to the host FS and never
   touches the container backend** (the regression guard). Real-container
   write-back is still gated.

## Implementation notes (2026-07-07 — T8 landed: boundaries — background refuse + teardown)

T8 closes the boundaries (full suite 3079 passed / 0 failed, import-linter 16 kept
0 broken, ruff/naming clean, the background-shell regression green).

1. **A clear error for `run_in_background=True` under sandbox (D5)**: background
   goes through the host `ProcessRegistry` (spawning a detached host subprocess) —
   it cannot reach the container, and AIO has no durable job handle (v2). `ExecEnv`
   gains a `supports_background` property (`LocalExecEnv` = True,
   `AioSandboxExecEnv` = False); before the background branch, `shell.py` checks
   `getattr(self.exec_env, "supports_background", True)` (default True → the local
   / older-backend paths are unchanged), and otherwise returns
   `_err("run_in_background is not supported in sandbox mode (v1)…")` **without
   spawning**. `background_status` / `background_kill` need no change (under
   sandbox no job is ever created, so both poll and kill land on "unknown job").
2. **Teardown (D6) is host-level; per-conversation is deliberately not done**: v1
   shares one container per host, so tearing down when one session closes would
   **hit other running sessions on the same host**. So teardown hangs only off
   `Client.shutdown → SdkHost.teardown_exec_env` (wired in T5; it collects every
   container connection at process exit), and `ConversationClosed` / root-task
   terminal do **not** tear down. Per-container teardown arrives with v2's per-root
   containers. This is **semantically** aligned with background-shell's
   "session-lifetime teardown" (both collect resources as the session/process winds
   down); v1's resource boundary is simply the host rather than the conversation.
3. **Added tests**: `test_sandbox_background_shell.py` (3) — concrete-class
   capability (Local True / AIO False); a sandbox backend's
   `shell_run(run_in_background=True)` → a clear failure containing "not supported
   in sandbox mode"; **foreground shell still works** (only background is blocked).

## Implementation notes (2026-07-07 — T9 landed: docs + ADR + CONTEXT + known-limitations) — **initiative complete**

T9 wraps up the documentation (full suite 3079 passed, `test_docs_codeblocks`
green, lint-naming clean). **T1→T9 have all landed**; the `feat/exec-env-sandbox`
branch is ready and unmerged.

1. **A new ADR, `docs/adr/execution-environment-seam.md`**: written to ADR
   discipline (present tense, no T1–T9 process numbering, why-not-how), pinning D1
   (unfenced across generations, with an explicit link back to
   `multi-host-lease-fencing.md`'s alternative #1), D2 (the seam shape +
   addressing-in-config / secret-in-env), D3 (the layering), plus v1's one
   container per host, the `exec_env_ref` reconnect and the background refusal.
   ADRs are excluded from the site by VitePress `srcExclude` (`**/adr/**`), so it
   references other ADRs in prose (matching the existing style) with no dead-link
   risk.
2. **`CONTEXT.md` gains the `ExecEnv` term** (in the Execution model section): a
   deep seam, the Local/AIO backends, a per-tool construction field that does not
   enter the schema (stable prefix), the `exec_env_ref` reconnect, and the key
   never landing in the log; `_Avoid_` pins "Sandbox (that is a backend, not the
   seam) / Workspace (already taken) / Executor (an Engine sense)".
3. **`docs/operations/limitations.md` gains two entries** (a published site page,
   referencing the ADR in prose only, with no new markdown link → no dead link):
   (a) "Sandbox side effects are not fenced across worker generations" (D1/R1);
   (b) "One sandbox container per host; idle containers stay billed" (combining
   v1's single container with no per-session isolation, the idle cost, the fact
   that `exec_env_ref` records only `base_url`, and that teardown is host-level).

**Against the spec's acceptance criteria**: 1 zero regression ✅
(`default_host_byte_equal` + the full suite green); 2 stable prefix unchanged ✅
(schema snapshot green); 3 the sandbox works ✅ (fake transport; a real container
is gated); 4 multi-machine reconnect ✅ (the cross-host case in
`test_sandbox_exec_env_ref.py`); 5 rewind under sandbox ✅
(`test_sandbox_rewind.py`); 6 clear boundaries ✅ (background refusal +
host-shutdown teardown); 7 known-limitations + ADR + CONTEXT ✅. **The one gated
item**: real AIO container e2e (`NOETA_TEST_AIO_SANDBOX_URL`) — until that runs,
do not claim the adapter contract (R2) as "verified".

## Files / areas to inspect

- `packages/noeta-runtime/noeta/tools/fs/` — `read.py` / `edit.py` / `write.py` /
  `patch.py` / `shell.py` / `_subprocess.py` / `_workspace.py` (rerouted to
  `ctx.exec_env`).
- `packages/noeta-runtime/noeta/runtime/tool.py` — `ToolRuntime.__init__` (adding
  `exec_env_resolver`) / `invoke` (constructing `ToolContext.exec_env`) /
  `_capture_file_baselines` (the rewind read rerouting).
- `packages/noeta-runtime/noeta/runtime/file_checkpoint.py` — the restore
  write-back rerouting.
- `packages/noeta-runtime/noeta/runtime/exec_env.py` (**new**) — `LocalExecEnv` /
  `AioSandboxExecEnv`.
- `packages/noeta-runtime/noeta/protocols/` — `tool.py`
  (`ToolContext.exec_env`), the new `exec_env.py` (`ExecEnv` / `ShellResult`),
  `events.py` (`TaskHostBoundPayload.exec_env_ref`).
- `packages/noeta-sdk/noeta/client/host_config.py` — the `exec_env` field +
  `SandboxExecEnvConfig`; `host.py`'s builder instantiating the adapter.
- `apps/noeta-agent/**` (`noeta.agent.backend`) — the sandbox manager
  (provision/attach/teardown) + lifecycle mounting.
- `packages/noeta-runtime/noeta/execution/` — `driver` / `resolver` (the
  `exec_env_ref` weld/fold; aligned with the existing `workspace_dir` treatment).
- Reference ADRs: `workspace-and-session-path.md` (the ref-in-the-log pattern),
  `multi-host-lease-fencing.md` (D1 and the external-resource boundary),
  `shell-permission-and-background.md` (why background is dropped in v1),
  `conversation-rewind-and-file-checkpoint.md` (the rewind choke point),
  `step-attempt-recovery.md` (the stance on side-effect fallbacks).
- New ADR: `docs/adr/execution-environment-seam.md`; add the `ExecEnv` term to
  `CONTEXT.md`; add two entries to `docs/operations/limitations`.
