# fs / shell / browser side effects route through an `ExecEnv` + `BrowserBackend` seam, backed by a per-session sandbox container the host provisions

## Context

The fs and shell tools perform real side effects — read and write files, spawn
processes. A `WorkspaceRoot` gives them *containment*, not *isolation*: a tool
that spawns a process (`shell_run`) can reach the rest of the filesystem. Running
an untrusted agent's tools directly on the host is the gap a sandboxed execution
model closes.

Three constraints shape the design.

- **The model-facing tool contract is frozen.** Name, schema, and description
  feed the stable prefix, and the KV-cache reproducibility invariant forbids
  perturbing them. Isolation may swap the *execution backend*; it may never touch
  the tool surface.
- **A session must be reconnectable from another host.** A worker can crash and a
  different host folds the task back, so whatever container a session is bound to
  has to be addressable from the durable record.
- **The library does not provision infrastructure.** Who runs `docker`, a K8s
  API, or a remote session service is the embedding product's concern; the SDK
  drives an interface, never a daemon.

## Decision

**`ExecEnv` is the IO seam under the tool packs.** Every file and process
operation a tool performs — `read_bytes` / `read_text` / `write_bytes` /
`create_exclusive` / `unlink` / `mkdir`, the stat predicates, `glob` / `rglob` /
`tree_snapshot`, and `run_argv` — goes through an injected `ExecEnv`. The seam is
deliberately IO-only: path resolution stays on `WorkspaceRoot`, so a tool
resolves a user path through its containment fence and hands the resolved
absolute path to the backend. `LocalExecEnv` (host `Path` + subprocess) is the
default; a container adapter satisfies the same Protocol over HTTP. The backend
is a per-tool construction field bound at wiring time, never read from
`ToolContext`, so tool schemas are untouched whichever backend is bound.

**One adapter owns the container wire.** Field names, the base64 read/write
encoding, the merged output stream, the `cd <cwd> &&` command shape, the shell
translation of stat / walk / unlink, and the mapping from the container's
`error_type` to the stdlib `OSError` subclass the local backend would raise — all
confined to the AIO adapter in the `sandbox` built-in plugin and pinned by
fake-transport tests. Contract drift is a one-file change, and tools branch
identically against either backend.

**`SandboxProvider` splits provisioning from mechanism.** The SDK defines
`allocate(root_task_id, spec)` / `release` / `attach` plus the values they
exchange: `SandboxHandle` (durable addressing — `base_url`, `sandbox_id`,
`workdir` — plus a live `SandboxAuth` strategy that is never serialized) and
`SandboxSpec` / `MountSpec` (image, resource caps, mount list, with `kind`
abstracting local path, volume, NAS, and PVC behind one shape). The host
implements the provider; the SDK's `SandboxExecEnvManager` is its only consumer.
A host that instead names one pre-existing container by `base_url` is wrapped in
a degenerate attach provider, so the manager has a single code path.

**`exec_env_ref` is the durable binding.** The handle's addressing is packed into
a flat `"{base_url}#{sandbox_id}"` string (split on the last `#`), welded onto
`TaskHostBound`, folded into governance, and threaded through the engine
resolver's cache key alongside workspace and provider — two sessions bound to
different containers never share an Engine. A provider that mints no id encodes to
the bare `base_url`. Resume and stale-reclaim read the recorded ref and reconnect
through `provider.attach`; credentials come from the reconnecting host's own
environment and never enter the record, a log, or an event.

**Lifetime is per root-task tree.** The driver pre-mints the root task id at seed,
allocates eagerly, and welds the ref. Subtasks inherit the root's binding. The
container is released when the root task reaches a terminal, with process shutdown
as the backstop for interactive sessions that rest at `suspended`.

**Scope of the seam.** Under a sandbox, fs tools, foreground shell, skill
indexing, `run_skill_script`, the workspace config loaders, and web fetch/search
egress all execute through the session's `ExecEnv`. Skill discovery uses the
batch `tree_snapshot` primitive — a remote backend's per-call fixed cost makes a
per-file `rglob` + `is_file` + `read_text` walk O(N) round-trips where the batch
is O(1) — and reads `SKILL.md` through the container so a rendered base directory
is a container path the model can read. Web egress goes out via
`run_argv(["curl", ...])`. Deliberately host-side: `memory_*` (global
cross-session user memory, not workspace-scoped), MCP, background shell
launch/poll/kill, and the app preview gateway. Background shell is refused under a
container (`supports_background` is false) because the host process registry
spawns detached host subprocesses and the container API exposes no durable job
handle.

**The browser is a noeta-owned tool pack, not a connector.** Five tools —
`browser_navigate`, `browser_click`, `browser_type`, `browser_extract`,
`browser_screenshot` — carry noeta's own names, schemas, and descriptions and
delegate through a narrow `BrowserBackend` Protocol that the `browser` built-in
owns and the `sandbox` built-in implements over the container's `/mcp` browser
server. The pack enters a session as a `session_pack` contribution whose
applicability check is the sandbox-backend gate plus the `browser` capability
flag, so no container means no browser tools. Elements are addressed by the
numeric `index` a prior extract handed the model; `browser_screenshot` stores the
PNG as a workspace artifact rather than feeding it to the model as vision. Every
action can egress anywhere, so the pack is `risk_level="high"` and routes through
the same approval predicate as `shell_run`. A `web` subagent owns page work so
browsing token bloat stays in a child context.

**Two host hooks tune the container without reshaping the seam.** A per-exec
preamble — `(exec_env_ref, argv) -> prefix` — is minted fresh for every container
command and prepended verbatim between the `cd <cwd> &&` and the command; it is
the process twin of the per-request auth header factory, because the wire carries
only a command string and a credential can expire mid-session. The prefix carries
its own separator, and `""` keeps the command byte-identical. A `sandbox_policy`
— `(root_task_id, workspace_dir) -> provision?` — lets a host decline a container
for one session while a provider serves the rest: declining records no ref, and
the build falls back to `LocalExecEnv` plus the host `WorkspaceRoot`.

**The public surface is protocols and factory types only.** `ExecEnv`,
`BrowserBackend`, `BackendFactory`, `BrowserBackendFactory`, `BoundPreamble`, and
the provider value types are exported from `noeta.sdk`; the concrete AIO adapter
classes are not. A product swaps the whole wire by injecting its own factories.

**Cross-generation container writes are not fenced.** Lease fencing rests on there
being no load-bearing write outside the shared transaction, and a fenced-out
zombie worker can reach the container regardless. This is accepted as an external,
at-least-once effect in the same class as a half-run `shell_run`: a reclaiming
worker reconnects to the same container and re-drives. The seam reserves an opaque
`fence_token` — always `None` — so a generation fence can fill it without
reshaping the interface.

## Rationale

- **Swapping only the executor keeps the stable prefix reproducible.** Isolation
  is host wiring, not part of any agent's identity, so two clients differing only
  in whether they sandbox emit byte-identical tool schemas and the KV-cache
  invariant holds for free.
- **A one-file wire contract survives an evolving external API.** The container's
  HTTP surface can drift; confining every field name and encoding to one adapter
  pinned by fake-transport tests means drift never leaks into tool code.
- **Addressing is durable, the secret is not.** Recording the address and fetching
  the credential at connect time makes a container reconnectable from the record
  without a key ever landing in it — the same split the workspace path model uses.
- **Provisioning belongs to the product.** Keeping the SDK on one interface leaves
  it orchestration- and tenancy-agnostic: a Docker daemon, a K8s cluster, and a
  remote session service are all the same shape above the seam.
- **Not fencing cross-generation writes is the honest cost of an external
  resource.** Fencing needs an orchestration-layer generation token and a
  validating proxy; claiming isolation guarantees the deployment cannot keep would
  be worse than naming the gap.
- **Owning the browser schema keeps an image upgrade off the model.** When the
  container renames one of its own tools, one adapter test breaks; the model-facing
  contract does not move.

## Alternatives considered

1. **Route tool IO through a per-call `ToolContext.exec_env` field.** Rejected:
   `WorkspaceRoot` is a per-tool construction field, so mirroring it keeps every
   tool-construction call site unchanged and needs no `ToolRuntime` / `ToolContext`
   change. The rewind restore — the one runtime-level file operation — takes its
   backend from the recorded ref instead.
2. **Put `glob` / `grep` / `list_dir` in the `ExecEnv` interface.** Rejected: they
   are expressible above the seam from `run_argv` and `read_bytes`, and a deep
   module wants the small interface.
3. **Fence container writes with a generation token.** Rejected as premature: it
   needs a controlled proxy in front of the container plus rotation on
   stale-reclaim. The `fence_token` placeholder reserves the shape.
4. **A structured `{base_url, sandbox_id}` ref.** Rejected: it forces a
   canonical-serialization change for a value the flat encoding already expresses,
   and exactly one codec splits it. Revisit if a third addressing field appears.
5. **Multiple providers, one per session.** Rejected: the manager, its backend
   cache, and the reaper are built around one provider per host; a boolean gate at
   allocate is the entire surface a per-session opt-out needs.
6. **A tier enum (`none` / `local` / `sandbox`) on the opt-out seam.** Rejected:
   stripping fs tools and choosing a working directory are product distinctions the
   embedding host already expresses elsewhere. A boolean keeps product tier
   vocabulary out of the engine.
7. **Let the driver skip allocation on a flag.** Rejected: it pushes product policy
   into the runtime. The host-config callback is the sanctioned injection point and
   needs no runtime edit.
8. **Mount the container's `/mcp` as a live MCP connector.** Rejected: it hands the
   model-facing schema to the container, perturbs the stable prefix, and overlaps
   the built-in fs/shell tools. Useful only as a throwaway probe that the container
   works.
9. **Drive the browser over CDP or Playwright directly.** Rejected: async and
   heavyweight, against the stdlib-only synchronous transport discipline; the
   container already wraps Playwright behind its own server.
10. **Use the container's coordinate-level browser HTTP face.** Rejected for the
    default path: pixel click / scroll / hotkey is the wrong altitude for a model.
    The element-level verbs live only behind `/mcp`.
11. **Present a browser element as an opaque string handle.** Rejected: the server
    keys elements by a numeric index it renders as `[7]` in the extract snapshot, so
    a string ref mis-types what the model literally sees.
12. **Feed screenshots to the model as vision.** Rejected for the default: text plus
    a numbered element list is cheaper and sufficient for most pages. A config-gated
    vision mode is a runtime behaviour, not a schema byte, so it can be added without
    a prefix change.
13. **Read a skill's `SKILL.md` host-side from the mount source and translate
    paths.** Rejected: faster, but it reintroduces host↔container path translation
    and renders a base directory the model cannot read inside the container.
14. **Give `ExecEnv` an `http_fetch` method for web egress.** Rejected:
    `run_argv(["curl", ...])` reuses the existing process seam with no new interface.
15. **Express the per-exec hook as an environment map instead of a shell preamble.**
    Rejected: an env map is cleaner and more backend-portable but cannot express a
    credential a CLI accepts only as a command. A preamble covers both env exports
    and setup commands and stays generic over any shell-based backend.
16. **Re-export the concrete container adapters from `noeta.sdk`.** Rejected:
    everything on `noeta.sdk` is a semver commitment, and the adapters are
    implementation detail behind the factory seams; publishing them would freeze a
    wire that is meant to be swappable.

## Consequences

- The `ExecEnv` Protocol and `LocalExecEnv` live in the runtime kernel; the
  container `ExecEnv` and browser adapters live in the `sandbox` built-in; the
  `BrowserBackend` Protocol and the five browser tools live in the `browser`
  built-in; the provider seam, its value types, and the lifecycle manager live in
  the SDK client layer. Provisioning lives in the embedding host.
- `TaskHostBound` carries an optional `exec_env_ref`, omitted from the canonical
  form when absent, so a non-sandbox recording is unaffected.
- The rewind restore writes baselines back through the session's `ExecEnv` when a
  ref is recorded, so a rewind under a sandbox restores inside the container.
- **Mounts are weak filesystem isolation.** The container writes the host workspace
  through a mount rather than a full jail; only workspace and skills are mounted,
  never host root. Stronger isolation needs a copy-in / sync-out provider, which
  the seam allows.
- **A machine-local provider cannot be reattached cross-machine.** A container
  bound to the daemon that ran it fails a cross-host reclaim `attach`. A NAS-backed
  or cluster provider removes this from the storage layer; the auth strategy, a
  `base_url` that tolerates a gateway path prefix, and `MountSpec.kind` are the
  three seams that make such a provider a drop-in.
- **Idle cost and cold start.** A suspended session's container is billed until
  release, and each session pays a seconds-scale container start. Warm pools,
  pause, and snapshot are open work.
</content>
</invoke>
