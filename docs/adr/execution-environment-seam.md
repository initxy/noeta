# The ExecEnv seam: fs/shell side effects target a pluggable execution environment (local host or sandbox container)

## Context

The fs and shell tools perform real side effects — read/write files, run
processes — and by default they hit the host filesystem behind a `WorkspaceRoot`
path fence. That fence is *containment*, not *isolation*: its own docstring notes
that a tool which spawns a process (`shell_run`) can still touch the rest of the
filesystem. Running an untrusted agent's tools directly on the host is the P0 gap
against a sandboxed execution model.

The chosen backend is [AIO Sandbox](https://github.com/agent-infra/sandbox)
(`agent-infra/sandbox`, Apache-2.0): a single container exposing
`POST /v1/shell/exec` + `/v1/file/*`. Only shell + file isolation is in scope;
the container's browser / VNC / Jupyter surfaces are out.

Two constraints shape the design. First, the tools' **model-facing contract**
(name / schema / description) must not change — the stable-prefix KV-cache
reproducibility invariant (see the Stable Prefix term in CONTEXT.md) forbids
perturbing tool schemas, so a sandbox may only swap the *execution backend*, never
the tool surface. Second, the multi-host story (see multi-host-lease-fencing.md)
means a worker can crash and another host can fold the task back — so a session's
container must be reconnectable by any host that folds it.

## Decision

**A deep seam, `ExecEnv`, sits between the fs/shell tools and their real IO.**
Every file/process operation a tool performs (`read_bytes` / `write_bytes` /
`create_exclusive` / `unlink` / `mkdir` / stat / `glob` / `run_argv`) goes through
an injected `ExecEnv`, which operates on already-resolved absolute paths — the
tool still owns containment. `LocalExecEnv` is the default (the exact host `Path` /
subprocess operations, byte-identical to before the seam); `AioSandboxExecEnv` is
the adapter to a container. The seam is a per-tool construction field, injected at
wiring time, never read from `ToolContext` — the tools' schemas are untouched, so
the stable prefix is byte-identical whichever backend is bound.

**The AIO wire contract is isolated in the one adapter.** Field names, the base64
read/write encoding, the merged `output` stream, the `cd <cwd> &&` command shape,
the `error_type` → `OSError`-subclass mapping — all live only in
`AioSandboxExecEnv` and are pinned by fake-transport tests. A contract drift is a
one-file change.

**Config carries addressing only; the host builds the live backend.** A
`SandboxExecEnvConfig` (base_url / api_key env var / provision mode / container
workdir) is a pure, serialisable value a product backend can construct without
importing an adapter. The host that holds the config (the SDK host) turns it into
a live `AioSandboxExecEnv` — reading the API key from the environment at connect
time — and threads it into session assembly. The key rides only on the wire; it is
never in the config, a log, or an event.

**Cross-generation sandbox side effects are at-least-once and not fenced (v1).**
The lease-fencing model (multi-host-lease-fencing.md) rests on there being no
load-bearing write outside the shared Postgres transaction. A sandbox breaks that
premise: a fenced-out zombie worker can still `POST` to the container, outside any
transaction. v1 accepts this — a sandbox side effect is an external, at-least-once
effect in the same class as a half-run `shell_run` (step-attempt-recovery.md): a
reclaiming worker reconnects to the same container and re-drives; a slow zombie
polluting the container is bounded by step-attempt re-drive plus human review. The
seam reserves an opaque `fence_token` (always `None` today) so a future generation
fence can fill it without reshaping the interface.

**A session's container is durable and reconnectable.** The container's `base_url`
is welded into `TaskHostBound` as `exec_env_ref` and folded into governance,
exactly like the session workspace path. On resume or stale-reclaim — possibly on
another host whose config default differs — the resolver reads the recorded
address and reconnects to that container; credentials still come from the folding
host's own environment. Reclaim needs no special handling: a re-folded task
re-resolves through the same path.

**v1 is one container per host.** Container orchestration is a non-goal, so a
config names one external container by `base_url` and every session on the host
shares it. `exec_env_ref` therefore records the `base_url` alone, not the
`{base_url, sandbox_id}` a per-container future would carry. Background shell is
refused under a sandbox (the host process registry spawns host subprocesses, and
AIO has no durable job handle); teardown is host-shutdown-scoped, since reaping a
shared container when one conversation closes would break the others.

## Rationale

- **Swapping only the executor keeps the stable prefix reproducible.** Isolation
  is a host-wiring choice, not part of any agent's identity, so two clients that
  differ only in whether they sandbox produce byte-identical tool schemas — the
  KV-cache invariant holds for free.
- **A one-file wire contract survives an evolving external API.** The AIO v1
  surface can drift; confining every field name and encoding to one adapter, pinned
  by fake-transport tests, means a drift never leaks into tool code.
- **Addressing-in-config, secret-in-env mirrors the session path model.** The
  workspace-and-session-path model already records addressing (a path, a provider
  *name*) durably while keeping secrets out of the log; the sandbox ref reuses that
  split so a container is reconnectable from the log without a key ever landing in
  it.
- **Not fencing v1 is the honest cost of an external resource.** The system
  already tolerates at-least-once side effects for crashed steps; a container is
  the same class of external effect. Fencing it properly needs an orchestration-layer
  generation token and a validating proxy — real work that belongs with the
  per-container future, not bolted on now. This is the point multi-host-lease-fencing.md
  named in its first alternative: an epoch/fence token only becomes load-bearing
  once a write lands outside the shared database, which is exactly here.

## Alternatives considered

1. **Route tool IO through `ToolContext.exec_env` (a per-call runtime field).**
   Rejected: `WorkspaceRoot` is already a per-tool construction field, so mirroring
   it as a per-tool `ExecEnv` field keeps every existing tool-construction call site
   unchanged and needs no `ToolRuntime`/`ToolContext` change. The rewind restore —
   the one runtime-level file op — gets its backend from the recorded `exec_env_ref`
   instead, so no runtime choke point needs the per-tool field.
2. **Put `glob` / `grep` / `list_dir` in the `ExecEnv` interface.** Rejected: they
   are expressible above the seam from `run_argv` + `read_bytes`, so keeping them out
   holds the interface small (a deep module).
3. **Fence sandbox writes with a generation token in v1.** Rejected as premature:
   it needs a controlled proxy in front of AIO and stale-reclaim rotation — a
   separate engineering effort. The `fence_token` placeholder reserves the seam shape
   so v2 adds it without an interface change.
4. **One container per root-task tree, provisioned per session.** Rejected for v1:
   AIO's used surface has no container-create call and cluster orchestration is a
   non-goal, so a config addresses one external container. Per-root containers (and a
   distinct `sandbox_id`) arrive with real orchestration.
5. **Mount AIO's `/mcp` as a live MCP server instead of a backend.** Rejected as a
   terminal design: it introduces the container's own tool names/schemas, perturbs
   the stable prefix, and overlaps the built-in fs/shell tools. Useful only as a
   throwaway "does the container work" probe.
6. **Sandbox background shell via the host process registry.** Rejected: the registry
   spawns detached host subprocesses it cannot target a container with, and AIO has no
   durable job handle. A background launch is refused cleanly under a sandbox;
   container-durable jobs are separate future work.

## Consequences

- The `ExecEnv` protocol, `LocalExecEnv`, and `AioSandboxExecEnv` live in
  `noeta.tools.fs.exec_env` (the materials band, alongside the tools that use them —
  not the kernel-services band, which may not import materials). `SandboxExecEnvConfig`
  and `HostConfig.exec_env` live in the SDK host-config surface; the SDK host owns the
  manager that builds and reconnects backends and reaps them on shutdown.
- `TaskHostBound` gains an optional `exec_env_ref` (omitted from the canonical form
  when absent, so every pre-existing recording stays byte-equal); it folds into
  governance and threads through the engine resolver's cache key like the workspace and
  provider bindings, so two sessions bound to different containers never share an Engine.
- The rewind restore writes baselines back through the session's `ExecEnv` when the
  session recorded a sandbox ref, so a rewind under a sandbox restores inside the
  container; a local session keeps the byte-identical host-filesystem path.
- Accepted costs, recorded in the known-limitations: cross-generation sandbox side
  effects are unfenced (a slow zombie can pollute the container), and an idle container
  is billed for the whole time a session is suspended (no pause/snapshot). Both are
  bounded by the v1 single-container-per-host model until per-container orchestration
  lands.
