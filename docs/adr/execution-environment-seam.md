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

## v2 (2026-07-08): per-session containers + Tier 2 + product activation

The v1 "one shared container per host, fs/shell only, product-inactive" shape is
superseded (its record above stays for history). See the implementation spec
`docs/implementation-specs/2026-07-08-per-session-sandbox.md` for the full design.

**A `SandboxProvider` seam splits *provisioning* from *mechanism*.** v1 addressed
one pre-existing container by `base_url`; v2 provisions a **fresh container per
root-task tree**. The SDK defines a `SandboxProvider` Protocol
(`allocate(session_root_id, spec)` / `release` / `attach`) plus the value types it
exchanges (`SandboxHandle` = durable addressing + a live `SandboxAuth` strategy
never serialized; `SandboxSpec` / `MountSpec` = the configurable image / caps /
mount list). The agent product implements it — `LocalDockerSandboxProvider`
`docker run -d`s the AIO image (workspace + skills `-v` mounts, api-key, resource
caps, `-p 127.0.0.1:<port>:8080`), polls `GET /v1/sandbox` to ready, and
`docker rm -f`s on release. The three-layer split holds: mechanism → runtime
(`ExecEnv`), binding + reconnect → SDK (`exec_env_ref`, the `SandboxExecEnvManager`
that drives the provider), provisioning + lifecycle → agent (the provider impl).
v1's `HostConfig.exec_env` (attach-one-container) survives as a degenerate provider
(`_ConfigAttachProvider`), so that deployment + its gated e2e are unchanged.

**`exec_env_ref` now carries the `sandbox_id`.** It is a flat string encoded
`"{base_url}#{sandbox_id}"` (split on the last `#`), reusing the existing
`__canonical_omit_none__` omit-when-`None` idiom — no canonical-serialization
reshaping. An attach provider mints no id, so its ref stays a bare `base_url`,
byte-identical to a v1 recording; every non-sandbox recording still omits the
field. The driver pre-mints the root task id at `seed_start` (so the container is
keyed by it), eagerly `allocate`s, and welds the encoded ref into `TaskHostBound`;
reconnect on resume/reclaim goes through `provider.attach`.

**Per-session teardown.** A container is `release`d when its ROOT task reaches a
terminal (the tree is done), with `Client.shutdown` as the backstop for interactive
sessions that rest at `suspended`. This is only possible because the container is no
longer shared — v1 could only tear down at host shutdown.

**Scope widened to Tier 2.** Beyond fs + foreground shell, the skill indexer,
`run_skill_script`, the workspace config loaders (instructions / environment /
shell-allowlist), and web fetch/search **egress** now all execute through the
session's `ExecEnv` in sandbox mode. The skill indexer reads `SKILL.md` through the
container so a skill's rendered *base directory* is a container path the model can
`read`; `run_skill_script` reads the script bytes + runs the interpreter in the
container (cwd = container workdir); the loaders read their files in the container
(fixing a v1 bug where they read container paths against the host FS); web egress
goes out via `exec_env.run_argv(["curl", ...])`. Deliberately **left on the host**:
`memory_*` (global cross-session user memory, not workspace-scoped), MCP (Tier 3),
`shell` background/poll/kill (AIO has no durable job), `open_app` (host preview
gateway). The seam widened by **constructor field injection** (the same idiom v1
used), never through `ToolContext`, so tool schemas — and the stable prefix — are
untouched.

**Product activation.** `apps/noeta-agent` gains `NOETA_AGENT_SANDBOX` (+ image /
memory / cpus / api-key-env knobs); when on it wires `LocalDockerSandboxProvider`
into `HostConfig`. Off by default (needs a local Docker daemon + the AIO image).

### v2 alternatives considered

- **A `{base_url, sandbox_id}` structured ref instead of the flat encoding.**
  Rejected for v1: it forces a canonical-tag/register change to the serialization
  for a value the flat `"{base_url}#{sandbox_id}"` already expresses, and the
  adapter splits it in one place. Revisit if a third addressing field appears.
- **Read skill `SKILL.md` host-side (from the mount source) + translate paths.**
  Rejected: faster, but it reintroduces a host↔container path translation and a
  rendered base directory the model cannot `read` inside the container. Reading
  through the container keeps paths container-native (D6-Skills).
- **Give the `ExecEnv` seam an `http_fetch` method for web egress.** Deferred:
  `run_argv(["curl", ...])` reuses the existing process seam with no new interface;
  a first-class fetch method (or AIO's browser) is a later refinement.
- **Fully replace the attach-one-container config path.** Rejected: keeping
  `HostConfig.exec_env` working through a degenerate provider preserves the simple
  "attach a shared container" deployment and its gated e2e at no cost.

### v2 consequences / known-limitations (updates)

- **Weak FS isolation via mounts.** The container writes the host workspace through
  a `-v` mount (not a full FS jail); only workspace + skills are mounted (never host
  root). Real isolation needs a copy-in/sync-out provider (the seam allows it).
- **Cross-machine Docker reconnect does not work.** A `LocalDockerSandboxProvider`
  container is bound to the machine that ran it; a cross-host reclaim `attach`
  raises. A Distributed / NAS-backed provider (TAE) removes this from the storage
  layer — file state is reachable cross-machine, so a reclaiming host just re-pulls
  a container against the same NAS. `SandboxHandle.auth` (strategy, not a static
  key), gateway-path-prefix `base_url`, and `MountSpec.kind=nas` are the three
  zero-rework seams already in place for it.
- **Idle container cost + cold-start latency** (unchanged from v1, now per session):
  a suspended session's container is billed until release; each session pays a
  seconds-scale AIO cold start. Warm pool / pause / snapshot are future work.
- **Per-session containers shrink the unfenced blast radius.** A slow fenced-out
  zombie now pollutes only its own session's container (v1: the host-shared one).
  Cross-generation writes stay unfenced (`fence_token` still `None`).

### Per-exec identity preamble (host hook)

`HostConfig.sandbox_exec_preamble` — a host-supplied `(exec_env_ref, argv) ->
prefix` minted **fresh for every** container `run_argv` and prepended, verbatim,
between the `cd <cwd> &&` and the command. It is the **process twin of
`auth_headers`** (D8, the per-request HTTP header factory): the AIO wire carries
only a `command` string (no env field), so per-session shell state — most
importantly a credential that expires mid-session — must be re-established on
each exec, and once fs / shell tools route through the container the SDK / runtime
own the shell command, leaving the product no per-exec hook otherwise.

Shape and boundaries:

- The runtime `AioSandboxExecEnv` takes a plain `preamble: Callable[[argv], str]`
  and knows nothing about sessions or products (product-neutral). The SDK
  `SandboxExecEnvManager` curries the session's durable `exec_env_ref` into the
  host `sandbox_exec_preamble` when it builds a backend, so the product maps the
  ref back to its own session / user. Keyed on `exec_env_ref` (stable across a
  root and its subtasks, reconnect-safe), not the per-call task id.
- The prefix is returned complete, **including its own separator** (`export X=Y &&
  `, `foo; `); `""` is a no-op and keeps the command byte-identical — the
  stable-prefix invariant is preserved for every deployment that sets no preamble.
- Like `auth_headers` it is a host runtime injection — never LLM-controlled, never
  recorded (D5) — and must be total (return `""` on its own failure; a raise
  propagates).

**Why a preamble string, not an env map.** An env map is cleaner and more
backend-portable, but cannot express a credential a CLI accepts only as a command
(e.g. `bytedcli auth set-...`, which has no env form); a shell preamble covers
both env exports and setup commands, stays generic over any shell-based backend
(every current family), and lets the host use a tool's stable public CLI rather
than reverse-engineering its on-disk credential format.

## Browser subsystem (2026-07-09): noeta-owned browser tools over the container `/mcp`, not an MCP connector

A sandbox session can now drive the container's headless browser. The capability
lands in two layers, both **sandbox-gated** (no container ⇒ no browser tools) and
detailed in `docs/implementation-specs/2026-07-09-sandbox-browser-subsystem.md`:

- **Layer 3 — a noeta-owned browser tool pack.** Five tools (`browser_navigate`
  / `browser_click` / `browser_type` / `browser_extract` / `browser_screenshot`)
  whose **name / schema / description are noeta's**, injected per session the way
  the fs pack is (a construction field, never `ToolContext`), gated on a new
  `Capabilities.browser` bit. Each tool delegates through a narrow `BrowserBackend`
  seam whose one production impl, `AioBrowserBackend`, pins the container `/mcp`
  browser wire in a single adapter + fake-transport test.
- **Layer 4 — a `web` subagent.** An official agent identity (browser pack + a
  read-only floor) the main agent delegates page work to, so browsing token bloat
  stays in a child context and the parent gets back a distilled result.

**This threads the needle of alt #5; it does not reopen it.** Alt #5 above
rejected *mounting AIO's `/mcp` as a live MCP connector* — that injects the
container's own tool names/schemas, perturbs the stable prefix, and overlaps
fs/shell. The browser pack does none of these: the model-facing schema is
**noeta's** (the container's names never reach the model), the `McpHttpClient` is
reused purely as an **internal transport** (the browser tools never enter
`mcp_registry` or take an alias), and browser is **net-new** surface with no
fs/shell overlap. It also does not reopen the MCP-is-Tier-3 deferral (per-session
spec): the `ExecEnv` seam gains **no** MCP method — the browser backend carries its
own transport. This delivers the "AIO's browser as a later refinement" the v2
web-egress alternative pointed at (above).

**Why the container `/mcp`, not the `/v1/browser` HTTP face.** `/v1/browser/*` is
**coordinate-level** computer-use (pixel `CLICK(x,y)` / `SCROLL` / `HOTKEY`); the
high-level, element-level, LLM-friendly verbs (navigate, click-by-element, extract
page markdown) live **only** in the container's `/mcp` browser server. Both paths
require talking to the container; the only real choice is *who owns the schema*,
and layer 3 puts that with noeta, so an image upgrade that renames a `browser_*`
tool breaks one adapter test, never the model.

**The wire is pinned from source, not guessed.** The container's browser server is
`@agent-infra/mcp-server-browser` (published in `bytedance/UI-TARS-desktop`); its
real verbs differ from a Playwright-MCP-shaped guess in three places, so
`AioBrowserBackend` maps: elements are addressed by a numeric **`index`** (from
`browser_get_clickable_elements`), not a string ref; noeta's `browser_type` fans
out to `browser_form_input_fill` (+ `browser_press_key` "Enter" on submit); and
noeta's `browser_extract` composes `browser_get_markdown` with the numbered
element list. Names and arg keys are source-accurate; a live-container e2e still
owes runtime-return-shape confirmation.

**Perception is text/element-level in v1.** `browser_extract` returns page text
plus the numbered interactive-element list; `browser_screenshot` stores the PNG as
a **workspace artifact** (viewable in the file panel) and does **not** feed it to
the model as vision. A config-gated vision mode (feed the screenshot back; use the
`/v1/browser` coordinate path for anti-bot/visual sites) is increment 2 — the tool
schema is identical in both modes (whether the model sees the image is a runtime
behaviour, not a stable-prefix byte), so it lands without a prefix change.

**Permission.** Every browser action can egress to any site, so the pack is
`risk_level="high"` and routes through the same approval predicate as `shell_run`;
`bypassPermissions` passes, default / acceptEdits gate an unauthorized navigation.

### Browser alternatives considered

1. **Mount `/mcp` as an MCP connector (= alt #5).** Rejected for the same reasons,
   restated above: it surrenders the model-facing schema to the container.
2. **Drive the browser over CDP / Playwright directly.** Rejected: async +
   heavy-dependency, against noeta's stdlib-only synchronous transport discipline;
   the container already wraps Playwright behind `/mcp`.
3. **Present the element handle as an opaque string ref (Playwright-MCP "e7"
   style).** Rejected once the source showed the real server keys elements by a
   numeric `index` it renders as `[7]` in the extract snapshot — a string ref
   mis-types what the model literally sees. noeta owns the schema, so it uses
   `index: integer`.
4. **Feed screenshots as vision in v1.** Deferred to increment 2 (above); the seam
   is left in place so it does not perturb the prefix later.
