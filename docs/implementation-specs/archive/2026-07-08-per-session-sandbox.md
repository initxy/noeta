# Per-session Sandbox: one container per session + every tool/Skill inside the sandbox (Tier 2, Docker-first)

> **Status: Shipped** — S1–S11 all landed; the `SandboxProvider` per-session path is live.
> Real-container e2e stays gated on `NOETA_TEST_AIO_SANDBOX_URL` / a local Docker.
> Durable decisions: [execution-environment-seam.md](../../adr/execution-environment-seam.md).

## Goal

Upgrade v1's "one shared container per host, routing fs/shell only, not activated
in the product" into:

1. **One Sandbox container per session** (per root-task tree, with real
   provision / teardown rather than attaching to a shared container).
2. **Tier 2: every tool inside the sandbox** — fs + foreground shell + skill
   loading (the indexer) + skill scripts (`run_skill_script`) + the workspace
   config loaders (instructions/environment/shell-allowlist) + the **execution** of
   web fetch/search all land inside the session's own container.
3. **Product activation**: `apps/noeta-agent` genuinely wires the sandbox up, and
   it can be on by default.
4. **Clear agent / SDK layering**: provisioning and lifecycle belong to the agent
   layer; the SDK only defines and consumes a `SandboxProvider` seam.

## Non-goals

- **The memory tools do not go into the container**: `memory_read/write` is global
  cross-session user memory (a fixed host directory, not workspace-scoped) and
  stays on the host. Putting it in a per-session ephemeral container would destroy
  memories when the session ends.
- **MCP does not go into the container** (this round): stdio server subprocesses
  and HTTP MCP still run on the worker side (the host). Tier 3 (MCP into the
  container) is left for later — it would add MCP methods to the seam and could
  perturb the MCP tool schema (breaking the stable prefix).
- **No K8s / internal allocation-service backend** (this round): only a Docker
  backend; the `SandboxProvider` seam reserves the extension point.
- **No warm pool / pause / snapshot**: a per-session container is bound to the
  session lifetime, and the idle cost is recorded in known-limitations.
- **No change to tools' model-facing contracts** (name/schema/description) → the
  stable-prefix KV cache is unchanged (a hard constraint, see `CONTEXT.md` Stable
  Prefix).
- **No change to EventLog event byte semantics, to the fencing ADR's D1–D3, or to
  the Dispatcher / Engine main loop.**

## Context

- **The three-layer topology and the import-linter bands**: `noeta.tools`
  (materials) > `noeta.runtime` (kernel-services) > `noeta.execution`; the SDK
  `noeta.client` sits above tools (and may import `AioSandboxExecEnv`);
  `apps/noeta-agent` (`noeta.agent`) is on top. The original ADR principle:
  **"allocation/management" belongs to the agent layer (matching the workspace
  registry), "mechanism" belongs to the runtime, and config carries addressing but
  never keys.** This spec follows it.
- **v1 mechanisms already in place** (reused / evolved directly):
  - The `ExecEnv` Protocol + `LocalExecEnv` + `AioSandboxExecEnv`
    (`packages/noeta-runtime/noeta/tools/fs/exec_env.py`) — the IO + process
    interface, with the AIO wire contract (`/v1/shell/exec`, `/v1/file/*`) locked
    in one adapter.
  - `WorkspaceRoot.for_container` (the lexical container root,
    `tools/fs/_workspace.py`).
  - `build_fs_tools(exec_env=)` (`tools/fs/__init__.py`), one backend shared by the
    whole fs/shell pack.
  - The full durable chain for `exec_env_ref`:
    `TaskHostBoundPayload.exec_env_ref` (events) → `GovernanceState.exec_env_ref`
    (task) → fold → resolver `_bound_exec_env_ref_for` + the engine cache key's 9th
    dimension + subtask inheritance → `SdkHost._build_engine` reconnect
    (`packages/noeta-sdk/noeta/client/host.py`). The weld is in
    `driver.seed_start`.
  - `SandboxExecEnvConfig` + `HostConfig.exec_env`
    (`packages/noeta-sdk/noeta/client/host_config.py`); `SandboxExecEnvManager`
    (`noeta/client/sandbox.py`), which in v1 caches one backend per base_url.
- **Why v1 was a shared container (the point this spec overturns)**: T5 note #2
  records it — the AIO API surface in use has no "create container" call, and
  `base_url` addresses one external container; per-root keying was bypassed by the
  seed engine (`task_id=None`) sharing an engine cache entry with the first driving
  turn. This spec solves it by introducing real provisioning (the provider mints an
  independent container per root task) plus an `exec_env_ref` that carries a
  `sandbox_id`.
- **Facts about the agent-sandbox SDK (research)**: `agent-sandbox` (PyPI) /
  `@agent-infra/sandbox` (npm) are **pure HTTP clients** and **do not provision
  containers**. "One container per session" means starting a container per session
  yourself (Docker/K8s). A single AIO container fronts every service on port 8080:
  shell/bash, file, jupyter/nodejs, browser+CDP, `/mcp`, port-proxy, VNC/VSCode.
  Auth: `SANDBOX_API_KEY` via `X-AIO-API-Key` / `Authorization: Bearer` /
  `?api_key=`. Image `ghcr.io/agent-infra/sandbox:latest` (the CN mirror is
  `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:<ver>`).
  ⚠️ Search results mentioning `SandboxClaim` / `WarmPool` are a **different**
  project, kubernetes-sigs/agent-sandbox, not agent-infra.
- **The host touchpoints Tier 2 involves (audited; all must be rerouted)**:
  - The skill indexer's bare `Path` use:
    `context/skills/indexer.py:155,163,170,176,180,191,276,277`;
    `execution/skills.py:136,166,368,369`.
  - `run_skill_script`'s bare FS + bare `run_argv` (no exec_env field):
    `tools/fs/skill_script.py:163,164,169,175,182`.
  - web's bare httpx: `tools/web/fetch.py:229`, `tools/web/search.py:199`.
  - The workspace config loaders, which read container paths but hit the host FS
    (**already broken** under v1 sandbox): `load_project_shell_allowlist`
    (`host.py:1383` / `shell.py:334-377`), `load_environment` (`builder.py:524`),
    `load_instructions` (`builder.py:509`).
  - `ToolContext` (`protocols/tool.py:107`) deliberately carries neither exec_env
    nor workspace; the per-call assembly point is `runtime/tool.py:104`.

## Decisions

### D1 [confirmed] The three-layer division of responsibility

| Concern | Layer | Contents |
|---|---|---|
| The sandbox mechanism (talking to the container) | runtime | The `ExecEnv` seam; this spec widens its coverage from fs/shell to skill loading / scripts / workspace loaders / web |
| Sandbox binding (which session uses which container, durable, reconnect, key from env) | SDK | The full `exec_env_ref` chain (existing, evolved to carry sandbox_id); defining and calling the `SandboxProvider` Protocol; feeding the live backend into `_build_engine` |
| Sandbox provisioning + lifecycle (really creating/destroying containers, mounts, pooling) | agent | The concrete `SandboxProvider` implementations (the Local / Distributed families); when to allocate/release |

> Terminology: this spec has no fourth "orchestration layer". What is called
> provisioning / orchestration *is* "the code that actually calls
> `docker run` / `docker rm` (or the K8s API) to create and destroy containers" =
> the `SandboxProvider` implementation itself, which lands in the **agent layer**.

### D2 [confirmed] The `SandboxProvider` seam: defined by the SDK, implemented by the agent (Local / Distributed families)

- **The Protocol** (landing in the SDK, `noeta.client`):
  ```python
  class SandboxAuth(Protocol):    # [reserved for TAE] auth is a policy, not a static key
      def connect_headers(self) -> dict[str, str]: ...   # generate auth headers at connect time
  # v1 implementation StaticApiKeyAuth(env_name) → {"X-AIO-API-Key": os.environ[env_name]}
  # TAE implementation JwtBearerAuth(signer)    → {"Authorization": f"Bearer {signer.mint()}"} (a short-lived JWT)

  class SandboxHandle:            # the addressing part is serialisable into the log; auth is a live object and is not serialised
      base_url: str               # a full URL, which must support a gateway path prefix (https://gateway/<prefix>), not just host:port
      sandbox_id: str
      workdir: str                # the workspace root inside the container, default /workspace
      auth: SandboxAuth           # NOT serialized; rebuilt from this machine's config on reconnect (keys / private keys never enter the log)

  class MountSpec:                # one mount (configurable at the storage layer, see D5)
      source: str                 # host path / NAS subdirectory / volume name / PVC name
      target: str                 # the path inside the container (kept identical for Local and Distributed → no path translation)
      mode: str                   # "rw" | "ro"
      kind: str                   # "local-path" | "nas" | "volume" | "pvc"

  class SandboxSpec:              # the allocate input: everything needed to create a container
      image: str
      mounts: list[MountSpec]     # a configurable mount list (workspace + skills + arbitrary extensions)
      resources: dict             # memory / cpus and other limits
      env: dict                   # extra env injected into the container

  class SandboxProvider(Protocol):
      def allocate(self, session_root_id: str, spec: SandboxSpec) -> SandboxHandle: ...
      def release(self, session_root_id: str) -> None: ...
      def attach(self, handle_ref) -> SandboxHandle: ...   # reconnect: rejoin by the recorded ref, never create
  ```
- **Two implementation families** (the real distinguishing axis is "where the
  container runs", not "Docker versus K8s"):
  | | **Local Sandbox** | **Distributed Sandbox** |
  |---|---|---|
  | Container location | The same machine as the worker (the local Docker daemon) | A remote node / cluster (a K8s Pod, remote Docker, an internal allocation service) |
  | addressing | `127.0.0.1:<port>` | A gateway address + a path prefix, `https://gateway/<prefix>` |
  | auth | A static `X-AIO-API-Key` (`StaticApiKeyAuth`) | A short-lived JWT Bearer (`JwtBearerAuth`, verified with `JWT_PUBLIC_KEY`) |
  | reconnect | Same-machine attach; lost on a host restart or across machines | Rejoin by session_id, with NAS surviving → **natively reachable across machines** |
  | Mount source | A local host path (`kind=local-path`, `-v`) | NAS (`kind=nas` → TAE `fuse_mount_params`) / PVC / NFS |
  | Implementation | **`LocalDockerSandboxProvider` (this round)** | **`TaeSandboxProvider`** (later, plugging into the seam with zero changes) / a K8s provider |
- **`LocalDockerSandboxProvider`** (`apps/noeta-agent`, `noeta.agent`), what its
  three methods actually do:
  - `allocate` = pick the image / container name (`noeta-sbx-<session_root_id>`) /
    a free host port → assemble `docker run -d`
    (`-p 127.0.0.1:<port>:8080` + `-e SANDBOX_API_KEY` + the `-v mounts` + resource
    limits + `--security-opt seccomp=unconfined`) → start the container → health
    probe (poll `GET /v1/sandbox` with the key until ready or timeout) → return a
    `SandboxHandle`.
  - `release` = `docker rm -f noeta-sbx-<id>`.
  - `attach` = if the container still exists, reassemble `base_url` from the ref
    (looking up the port); if it is gone (host restart / a different machine),
    raise a clear error — an inherent limitation of local Docker, solved by the
    Distributed/NAS backend.
- **SDK consumption**: `SandboxExecEnvManager` is refactored from "cache one static
  backend per base_url" into "hold a `SandboxProvider` and
  allocate/cache/release per `session_root_id`". `SdkHost` injects the provider
  through `HostConfig` (default `None` ⇒ `LocalExecEnv`, a byte-equivalent
  fallback). `SandboxSpec.mounts` is assembled by SdkHost from the session's
  workspace_dir + noeta's own skills directory + the user's global skills directory
  (extensible via config).

### D3 [confirmed] Scope = Tier 2

Into the container (execution lands in the container):
`read/glob/grep/edit/write/apply_patch`, foreground `shell_run`, the skill indexer,
`run_skill_script`, the workspace loaders (instructions/environment/shell-allowlist),
`webfetch` / `web_search`.

Staying on the host: `memory_*` (global memory), MCP (stdio/HTTP, Tier 3), shell's
background/poll/kill (AIO has no durable job, so v1's refusal stands), and
`open_app` (the host preview gateway).

### D4 [confirmed + evolved] Per-session containers + lifecycle + a ref carrying sandbox_id

- **Binding granularity**: one container per root-task tree (subtasks share the
  parent's container, matching the rewind ADR's "subtasks share the parent's
  cwd/disk"). Key = the session-root task id.
- **Eager provisioning**: call `provider.allocate(session_root_id)` in
  `driver.seed_start` (at the root task's host-bind point) and weld the returned
  `SandboxHandle` into `TaskHostBoundPayload`.
- **`exec_env_ref` widens from a flat `str` (base_url only) to carrying
  `sandbox_id`** (delivering what v1's D4 deferred). Two possible landings
  [decided on your behalf, overridable]: **recommended** — keep the flat `str` but
  encode it as `"{base_url}#{sandbox_id}"` (avoiding the nested-dataclass canonical
  tag/register machinery and fully reusing `workspace_dir`'s existing
  `__canonical_omit_none__` idiom), with the adapter splitting it. Alternative:
  upgrade to a `{base_url, sandbox_id}` structure (cleaner but requiring changes to
  canonical serialisation).
- **Teardown**: a per-session container **can** be `provider.release()`d at
  root-task terminal + session close (no longer restricted, as in v1, to
  host-shutdown teardown because of sharing). Hook points: the fold where the root
  task reaches terminal + `Client.shutdown` as a backstop.
- **Reconnect**: resume/reclaim reads `exec_env_ref` → `provider.attach(ref)`
  rejoins the same `sandbox_id`; the key still comes from this machine's env
  (reusing D5). Cross-host reclaim: as long as that host can docker-attach to the
  container (same machine) or the container is reachable; local Docker cannot do it
  across machines → recorded as a limitation (only a K8s / internal-service backend
  solves cross-machine).

### D5 [confirmed] Docker mount seeding + "execution still goes entirely through the seam"

- **The provider's `docker run`** (mounting happens at `docker run` time and AIO
  itself is unaware; each entry comes from `SandboxSpec.mounts`):
  ```
  docker run -d --name noeta-sbx-<id> \
    -p 127.0.0.1:<port>:8080 -e SANDBOX_API_KEY=<key> \
    -v <host_workspace>:/workspace \                        # project files, rw
    -v <builtin_skills>:/opt/noeta/skills/builtin:ro \      # noeta's own skills
    -v <global_skills>:/opt/noeta/skills/global:ro \        # the user's global skills
    --memory 2g --cpus 2 --security-opt seccomp=unconfined \
    ghcr.io/agent-infra/sandbox:latest
  ```
- **Configurable at the storage layer** (not hard-coded): the three mounts above
  are the default set, and the actual list is driven by
  `SandboxSpec.mounts: list[MountSpec]` — shared data / cache directories can be
  added, and `kind` can change (Local = `local-path`, Distributed = `nas` / `pvc`).
  **The same-path principle**: `target` stays identical across both families (Local
  swaps `source` for a host path, Distributed for a NAS subdirectory), so the
  layers above — exec_env / the workspace root — need no changes.
- **Mounts only seed and persist**: project files get in, built-in/global skills are
  visible inside the container, and workspace changes land back on the host through
  the mount.
- **All tool execution still goes entirely through the seam into the container
  (confirmed 2026-07-08, settled and not overridable)** (honouring "every tool call
  executes inside the Sandbox"): fs IO goes through `AioSandboxExecEnv`
  (`/v1/file/*`) and processes through `/v1/shell/exec`. Mounts / shared storage
  make the bytes shared, and routing over HTTP costs latency — with shared storage,
  fs-over-HTTP is redundant — but the maintainer explicitly chose **semantic
  uniformity**: everything the agent's tools execute (including fs) goes only
  through the container, never a direct host-side connection. **Rejected
  alternatives** (considered and declined): fs IO connecting directly to the host
  mount path, or only processes/network entering the container (a hybrid ExecEnv) —
  faster, but breaking the uniform "everything executes in the sandbox" semantics.
- **Isolation level**: processes live in the container namespace, and the container
  FS can only touch the directories mounted in (this is not full FS isolation).
  Written into known-limitations.
- **The storage layer is orthogonal to the execution layer**: mounts (a local
  Docker volume) are the **storage-layer** seeding/persistence mechanism, and
  **NAS shared storage is its production generalisation** — the host and the Sandbox
  mount the same network storage underneath (visible host-side, durable, reachable
  across machines), as a storage/seeding backend behind `SandboxProvider` (see
  D5-NAS). The execution layer is unchanged (everything through the container).
  **Precondition**: the shared storage is mounted at the **same path** on the host
  and in the container (otherwise path translation is needed); the provider is
  responsible for ensuring this.
- **Skills directory resolution**: under sandbox mode, built-in/global point at the
  container mount points (`/opt/noeta/skills/*`) and the workspace layer is
  `<workdir>/.noeta/skills`; the three-layer merge logic is unchanged, only the root
  paths become container paths.

### D5-NAS [direction; a non-goal this round, with the seam reserved] NAS shared storage + a TAE managed backend

The maintainer's target state: **the host and the Sandbox share one NAS underneath,
with execution still entirely inside the Sandbox**, later switching to a **TAE
managed** deployment. This is the production generalisation of D5's local Docker
volume, landing behind `SandboxProvider`.

**TAE is the managed implementation of that target state** (confirmed by the AIO
internal docs' `mount` / `provider` surfaces): the TAE platform **provides a
control plane the OSS version lacks** — "create a Session dynamically via the API"
plus per-session `fuse_mount_params` mounting NAS. Two mount scopes:

- **Static mounts** (PSM level, applying to every session of that PSM): NAS
  configured when creating/editing the Sandbox in TAE — mapping our
  provider-level default mounts.
- **Dynamic mounts** (session level, `fuse_mount_params`): NAS mounted on demand
  when dynamically creating a session — mapping our **per-session** mounts
  (`MountSpec{kind=nas}`).

- **The provider variant** `TaeSandboxProvider` (the Distributed family):
  `allocate` = call TAE's "create Session dynamically" API (with
  `fuse_mount_params` generated from the `kind=nas` entries in
  `SandboxSpec.mounts`); `release` = destroy the session; `attach` = rejoin by
  `sandbox_id` (= the session id) — the TAE gateway + NAS are reachable across
  machines, with none of Docker-local's machine binding.
- **The execution layer is unchanged**: every tool still goes through the container
  (D5); NAS only changes "where the storage is and who can see it", not "where
  execution happens".
- **A side benefit**: NAS being reachable across machines **resolves R2's
  "cross-machine Docker reconnect does not work"**.
- **A non-goal this round**: only the local Docker volume (D5) is built; the
  TAE/NAS provider is left for later. But the seam must already satisfy three
  "zero-rework" preconditions (which this spec has incorporated): (a)
  `SandboxHandle.auth` is a policy (`StaticApiKeyAuth` locally, `JwtBearerAuth` for
  TAE), not a static env name; (b) `SandboxHandle.base_url` supports a gateway path
  prefix, and `AioSandboxExecEnv` builds URLs as `base_url + "/v1/..."` without
  assuming `host:port`; (c) the `MountSpec{kind=nas}` abstraction already assumes no
  local volume, and TAE translates it into `fuse_mount_params`.

### D6 [decided on your behalf, overridable] Widening the seam: keep construction-time field injection, do not put it on `ToolContext`

- v1 established "exec_env is a tool construction-time field and does not enter
  `ToolContext`" (keeping call sites unchanged and not touching the stable prefix).
  Tier 2's new tools/loaders **follow the same approach** and do not change
  `ToolContext`:
  - `run_skill_script`: `build_skill_script_wiring` gains an `exec_env` parameter
    and the tool gains an `exec_env` field; the FS read goes through
    `exec_env.read_bytes` and execution through `exec_env.run_argv` (replacing the
    directly imported `_subprocess.run_argv`).
  - The web pack: `build_web_tools(exec_env=)`; under sandbox mode
    `webfetch` / `web_search` egress through the container — **the mechanism
    [decided on your behalf, overridable]**: v1 issues the request inside the
    container with `exec_env.run_argv(["curl", ...])` (reusing the existing IO +
    process interface rather than adding a network method to the seam), parsing the
    response with the original logic; Local mode keeps the httpx path. Alternatives:
    add an `http_fetch` method to the seam, or use the AIO browser — left for later.
  - The skill indexer: `SkillIndexer` / `resolve_skill_*` / `skill_content_hash`
    accept an IO abstraction (exec_env or a read/stat/walk subset of it), and under
    sandbox mode **read SKILL.md through the container (confirmed, see D6-Skills)**.
  - The workspace loaders (instructions/environment/shell-allowlist): gain an
    `exec_env` parameter and read/write through the container under sandbox mode,
    fixing v1's broken "reads a container path but hits the host FS".
- The `ExecEnv` Protocol gains methods as needed (e.g. the `iterdir` / `stat` the
  indexer needs), staying a deep module (anything expressible with `run_argv` +
  `read_bytes` does not enter the interface).

### D6-Skills [confirmed] Skill loading/scripts into the container: keep noeta's SkillIndexer, do not use AIO's native skills API

Current state: main's skill loading is 100% host-side (`SkillIndexer` reads host
`Path`s directly), which is **already broken** under sandbox mode (it takes a
container path `/workspace` and reads the host FS). Goal: both reading and
executing skills land in the container.

- **Do not use AIO's native skills API**
  (`/v1/skills/register|metadatas|{name}/content`) — even though AIO skills and
  noeta skills share the **same format** (`SKILL.md` + frontmatter + `scripts/`, the
  Anthropic Agent Skills convention). **Reason for exclusion** (the same as the
  ADR's alt #5, "do not mount AIO's `/mcp`"): it would introduce AIO's skill
  names/metadata/rendering, **perturbing the stable prefix**, and would duplicate
  noeta's three-layer merge + event-sourced activation. noeta keeps its own
  `SkillIndexer` and only moves the IO into the container.
- **Skill directories (inside the container, mounted at provision time)**:
  built-in→`/opt/noeta/skills/builtin` (RO), global→`/opt/noeta/skills/global`
  (RO), workspace→`<workdir>/.noeta/skills` (following the workspace mount). The
  three-layer merge is unchanged, only the roots differ.
- **The ref (the `Base directory for this skill: <path>` rendered to the model) is
  a container path** (e.g. `/opt/noeta/skills/builtin/foo`), because the model then
  resolves that path inside the container with `read` / `run_skill_script`;
  rendering a host path would point at something that does not exist in the
  container.
- **Scripts (`run_skill_script`, S7)**: gain an `exec_env` field — the script hash
  check reads through `exec_env.read_bytes` (the container) and execution goes
  through `exec_env.run_argv` (inside the container, cwd = the container workdir),
  replacing the directly imported host `_subprocess.run_argv`. The script files
  themselves are in the container (mounted in).
- **[confirmed 2026-07-08: read through the container]** The indexer reads
  SKILL.md's bytes through the container (`exec_env` → `/v1/file/*`), consistent
  with "everything through the container"; paths are naturally containerised and the
  ref lines up directly. Accepted cost: one HTTP read per skill at session start
  (a few dozen ≈ a few hundred ms). **Rejected**: reading the mount source
  host-side plus path translation (faster, but introducing host/container path
  translation and breaking the uniform model). The ref and script execution must be
  container-side anyway, so this decision unifies the index read with them.

### D7 Fencing: per-session shrinks the blast radius, still unfenced (v1's position stands)

Per-session containers confine "slow-zombie pollution" to **that session's own
container** (in v1 it polluted the host's shared container, affecting every
session) → the blast radius shrinks substantially. Cross-generation writes are
still unfenced, with `fence_token` a permanent `None` placeholder (filled in when
the v2 orchestration-layer generation-token fence arrives). Recorded in
known-limitations (updating v1's entry).

### D8 Keys / addressing / the auth policy (reusing v1's D5 + generalising for TAE)

- **Only addressing lands in the log**: `base_url + sandbox_id` (+ `workdir`);
  durable and reconnectable.
- **auth is a live policy — never logged, never serialised**
  (`SandboxHandle.auth: SandboxAuth`): `auth.connect_headers()` generates the auth
  headers at connect time. v1 `StaticApiKeyAuth` (read the key from env →
  `X-AIO-API-Key`); TAE `JwtBearerAuth` (mint a short-lived JWT with the local
  private key → `Authorization: Bearer`, verified sandbox-side with
  `JWT_PUBLIC_KEY`). On reconnect (including across machines) auth is **rebuilt from
  this machine's config**, and keys / private keys never enter config, the log, or an
  event.
- **The gateway path prefix is already supported naturally**
  (`exec_env.py:412/444` already does `base_url.rstrip("/") + path`) →
  `https://gateway/<prefix>` works with no change. **The one thing that must
  change**: `AioSandboxExecEnv` currently fixes its auth headers at construction
  time (fine for a static key), whereas TAE's short-lived JWT requires fetching
  `auth.connect_headers()` **per call**.

## Implementation plan

1. **The `SandboxProvider` seam (SDK)**: define the Protocol +
   `SandboxHandle`/`SandboxSpec`; refactor `SandboxExecEnvManager` to hold a
   provider and allocate/cache/release/attach per session_root_id; inject the
   provider through `HostConfig` (default `None` ⇒ the Local fallback).
2. **`DockerSandboxProvider` (agent)**: `docker run` (mounts + api-key + port +
   resource limits) / `docker rm` / attach; a health probe (wait for
   `/v1/sandbox` ready).
3. **Per-session provisioning wiring**: `seed_start` eagerly allocates and welds
   the handle into `TaskHostBound`; `exec_env_ref` carries `sandbox_id`;
   `_build_engine` uses the handle to build an `AioSandboxExecEnv`.
4. **Lifecycle**: call `provider.release` at the root-task terminal fold +
   `Client.shutdown`; reconnect through `provider.attach`.
5. **Widening the seam (the Tier 2 tools)**: reroute the skill indexer /
   `run_skill_script` / the workspace loaders / the web pack through exec_env (D6).
   Fix v1's broken "the loaders hit the host FS".
6. **Containerise the skills directories**: built-in/global point at the container
   mount points; the three-layer merge uses container paths.
7. **Product activation**: `apps/noeta-agent` gains configurable sandbox support by
   default (provider + image + mount policy); document how to turn it on.
8. **Docs + ADR + CONTEXT**: update the ADR (v1→v2: per-container,
   SandboxProvider, Tier 2); update known-limitations (the mount isolation level,
   idle cost, cross-machine Docker reconnect not working); add the
   `SandboxProvider` term to CONTEXT.

## Task breakdown

| # | Task | Layer | Depends / parallel |
|---|---|---|---|
| S1 | The `SandboxProvider` / `SandboxAuth` Protocols + `SandboxHandle` / `SandboxSpec` / `MountSpec` + the manager refactor; change `AioSandboxExecEnv`'s headers from construction-time-fixed to **per-call** `auth.connect_headers()` (a precondition for TAE's short-lived JWT). Note: the URL prefix is already fine (`exec_env.py:412/444` already does `base_url.rstrip + path`) and needs no change | SDK | the foundation, first |
| S2 | `LocalDockerSandboxProvider` (run/rm/attach/health + configurable mounts) | agent | depends on S1 |
| S3 | `exec_env_ref` carrying sandbox_id (evolving the whole weld/fold/resolve/cache chain) | SDK/runtime | depends on S1; mirrors the existing v1 chain |
| S4 | seed_start eager provisioning + `_build_engine` using the handle | SDK | depends on S1/S3 |
| S5 | Teardown (root-terminal release + shutdown backstop) + attach reconnect | SDK/agent | depends on S2/S4 |
| S6 | Reroute the skill indexer through exec_env + containerise the skills directories | runtime | depends on the v1 seam; parallel with S2 |
| S7 | Reroute `run_skill_script` through exec_env (FS + run_argv) | runtime | parallel with S6 |
| S8 | Reroute the workspace loaders (instructions/environment/allowlist) through exec_env (fixing v1's break) | runtime/SDK | parallel with S6 |
| S9 | Reroute the web pack (sandbox egress through the container via curl/run_argv) | runtime | parallel with S6 |
| S10 | Product activation (apps/noeta-agent config + defaults) | agent | depends on S1–S5 |
| S11 | Docs + ADR + CONTEXT + known-limitations | — | wrap-up; depends on everything |

## Dependencies / sequencing

- **S1 is the seam and lands first**; S3 (the ref carrying sandbox_id) strictly
  mirrors v1's existing `exec_env_ref` chain and is accepted separately for its
  multi-machine semantics.
- **S6/S7/S8/S9 are runtime-side seam widening and are mutually parallel**,
  depending only on the existing v1 seam, so they can proceed alongside the
  agent-side S2.
- **S4/S5 are where per-session lifecycle correctness lands**, depending on the
  provider (S2) and the ref (S3).
- **S10 depends on the whole chain**; S11 wraps up.
- Every step preserves "a default config ⇒ a byte-equivalent Local fallback", so it
  can be turned off at any time.

## Acceptance criteria

1. **Zero regression**: with a default `HostConfig()` (no provider), the full
   existing suite is green; old recordings fold/replay byte-identically (a default
   `exec_env_ref` does not change `TaskHostBound`'s bytes).
2. **Stable prefix unchanged**: before and after, the same agent's serialised tool
   schema bytes are identical.
3. **Per-session isolation**: two concurrent sessions each get an **independent
   container** (different `sandbox_id`s), and one session's in-container file /
   process side effects are invisible to the other.
4. **Tier 2, every tool in the container**: fs / shell / skill loading / skill
   scripts / the workspace loaders / web all execute inside the session's
   container; memory stays on the host; MCP stays on the host.
5. **Skills work inside the container**: built-in/global (mounted RO) plus the
   workspace-layer skills are all indexed, and `run_skill_script` executes inside
   the container with cwd = the container workdir.
6. **Multi-machine / reconnect**: a worker starts work in sandbox-X → `kill -9` →
   another process folds it back, reads `exec_env_ref` → `attach`es back to the same
   `sandbox_id`, sees the in-container file state, and the task continues
   (Docker-local: same-machine reconnect; cross-machine recorded as a limitation).
7. **Lifecycle**: the container is `release`d (`docker rm`) after root-task terminal
   + session close, with a process-exit release as the backstop.
8. **Product activation**: with a provider configured, `apps/noeta-agent` works end
   to end (a real-container e2e, gated on `NOETA_TEST_AIO_SANDBOX_URL` or a local
   Docker).
9. **Docs**: the ADR is updated to the v2 shape; known-limitations is updated (the
   mount isolation level / idle cost / cross-machine Docker reconnect not working);
   CONTEXT gains `SandboxProvider`.

## Risks

- **R1 Weak mount isolation**: through the mount, the container writes directly into
  the host workspace; this is not full FS isolation. Mitigation = mount only the
  workspace + skills (never the host root); record the limitation; real isolation
  would use a copy-in/sync-out provider (the seam is reserved).
- **R2 Cross-machine Docker reconnect does not work**: a Docker-local backend's
  container is bound to the machine that started it, so a cross-machine reclaim
  cannot attach. Mitigation = record the limitation; cross-machine relies on a
  K8s / internal-service backend (the seam is reserved); the **NAS shared-storage
  backend (D5-NAS) solves this from the storage layer** — the file state is
  reachable across machines, so switching machines only requires starting a new
  container against the same NAS.
- **R3 Fidelity of web through the container's curl**: `web_search` (Tavily) and
  friends are re-issued via the container's `curl`, and the response parsing must
  stay identical. Mitigation = the Local and container paths share the parsing
  layer and swap only the transport; pinned by contract tests.
- **R4 Idle container cost**: a per-session container holds resources while the
  session is suspended (waiting on a human or a timer). Mitigation = record the
  limitation; warm pool / pause is left for later.
- **R5 Skill index path translation**: under a mount the container path differs from
  the host path, and the base directory / cwd must be the container path.
  Mitigation = read uniformly through the container (D6) so paths never cross the
  boundary.
- **R6 Provisioning latency**: cold-starting an AIO container per session (pulling
  the image, starting services) costs seconds. Mitigation = pre-pull the image
  locally; a warm pool is left for later.

## Files / areas to inspect

- SDK: `packages/noeta-sdk/noeta/client/sandbox.py` (the manager→provider
  refactor), `host_config.py` (provider injection), `host.py`
  (`_build_engine` / `exec_env_ref` / teardown).
- The runtime seam: `packages/noeta-runtime/noeta/tools/fs/exec_env.py` (add
  methods as needed), `tools/fs/skill_script.py`, `tools/web/fetch.py` +
  `search.py`, `context/skills/indexer.py`, `execution/skills.py`,
  `execution/builder.py` (rerouting the loaders + containerising the skills
  directories).
- The durable ref chain: `protocols/events.py`, `protocols/task.py`,
  `core/fold.py`, `execution/resolver.py`, `execution/driver.py` (the `seed_start`
  weld + the terminal release).
- agent: `apps/noeta-agent` (`DockerSandboxProvider` + lifecycle mounting + the
  default config).
- Reference: `docs/adr/execution-environment-seam.md`,
  `docs/implementation-specs/archive/2026-07-07-sandbox-exec-env.md` (the existing
  v1 chain, mirrored point by point), `multi-host-lease-fencing.md` (the D7
  boundary), `conversation-rewind-and-file-checkpoint.md` (subtasks sharing the
  parent's container).
