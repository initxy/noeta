# Use a sandbox execution environment

**Goal:** run an agent's fs / shell / browser side effects inside a per-session
container, so an untrusted agent cannot touch the host directly. Noeta ships the
seam (`ExecEnv`, `SandboxProvider`) and the container-wire adapters; the host
provisions the container.

**Before you start:** you can drive an agent with `Client` from
[Your first agent](../tutorials/first-agent.md) and you understand
[HostConfig](../reference/sdk.md) wiring.

## Two ways to wire a sandbox

Noeta gives you two entry points on `HostConfig`, depending on whether you want
one shared container or a fresh container per session:

| Mode | `HostConfig` field | When to use |
| --- | --- | --- |
| **Attach** (one shared container) | `exec_env: SandboxExecEnvConfig` | A single long-lived container every session attaches to — simplest, good for development. |
| **Provision** (per-session container) | `sandbox_provider: SandboxProvider` + `sandbox_spec: SandboxSpec` | A fresh container allocated when a session opens and torn down when it ends — production isolation. |

`SandboxProvider` takes precedence when both are set.

## Attach mode — point at a running container

The fastest path: start a container yourself (e.g. an
[AIO Sandbox](https://github.com/bytedance/aio-sandbox) image), then tell Noeta
its address.

```python
from noeta.sdk import Client, HostConfig, Options, SandboxExecEnvConfig

host_config = HostConfig(
    exec_env=SandboxExecEnvConfig(
        base_url="http://localhost:8080",
        api_key_env="SANDBOX_API_KEY",   # read from env at connect time
        workdir="/workspace",
    ),
)

client = Client(
    Options(system_prompt="You are a coding agent."),
    provider=my_provider,
    workspace_dir="./workspace",
    model="claude-sonnet-4-5-20250929",
    host_config=host_config,
)
```

The API key is read from the environment variable named by `api_key_env`
**only when a request is made** — it never enters the config, the log, or an
event. Set it before the first turn:

```bash
export SANDBOX_API_KEY=your-container-key
```

In attach mode every session shares the same container. `release` is a no-op
because the SDK does not own the container's lifecycle.

## Provision mode — implement `SandboxProvider`

For per-session isolation, implement the three-method `SandboxProvider`
protocol and pass it as `HostConfig.sandbox_provider`. The SDK calls
`allocate` when a root task opens, `attach` on resume / stale-reclaim, and
`release` when the root task reaches a terminal state.

```python
from dataclasses import dataclass
from noeta.sdk import (
    SandboxProvider, SandboxSpec, SandboxHandle,
    StaticApiKeyAuth, encode_exec_env_ref, decode_exec_env_ref,
)

class DockerSandboxProvider:
    """Provision a fresh container per root-task tree via the Docker CLI."""

    def allocate(self, root_task_id: str, spec: SandboxSpec) -> SandboxHandle:
        # 1. Run the container. Mount the workspace; spec.mounts carries the
        #    assembled list (workspace + skills + host extensions).
        mounts = " ".join(
            f"-v {m.source}:{m.target}:{m.mode}" for m in spec.mounts
        )
        import subprocess
        result = subprocess.run(
            ["docker", "run", "-d", "--rm",
             *mounts.split(),
             spec.image,
             "sleep", "infinity"],
            check=True, capture_output=True, text=True,
        )
        container_id = result.stdout.strip()

        # 2. Probe readiness, then return the handle. base_url must be the
        #    container's API root; workdir is the container-side workspace.
        return SandboxHandle(
            base_url=f"http://localhost:8080",
            sandbox_id=container_id,
            auth=StaticApiKeyAuth(env_name="SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        # Reconnect to a container recorded on TaskHostBound.exec_env_ref.
        base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=sandbox_id,
            auth=StaticApiKeyAuth(env_name="SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def release(self, root_task_id: str) -> None:
        # Idempotent: look up the container for this root task and stop it.
        # A real provider keeps a root_task_id -> container_id map.
        ...
```

Wire it:

```python
from noeta.sdk import HostConfig, SandboxSpec, MountSpec

host_config = HostConfig(
    sandbox_provider=DockerSandboxProvider(),
    sandbox_spec=SandboxSpec(
        image="aio-sandbox:latest",
        mounts=(
            MountSpec(source="./workspace", target="/workspace", mode="rw"),
        ),
        resources={"memory": "2g", "cpus": "2"},
    ),
)
```

### What `SandboxSpec` carries

| Field | Shape | Purpose |
| --- | --- | --- |
| `image` | `str` | The container image to run. |
| `mounts` | `tuple[MountSpec, ...]` | Workspace + skills + host mounts. `MountSpec.kind` is `"local-path"` / `"volume"` / `"nas"` / `"pvc"` so a distributed provider maps the same shape to its storage backend. |
| `resources` | `Mapping[str, str]` | Memory / CPU caps — passed straight to your provisioner. |
| `env` | `Mapping[str, str]` | Extra container environment. |

### The durable binding: `exec_env_ref`

A session's container address is recorded on `TaskHostBound.exec_env_ref` as
the flat string `"{base_url}#{sandbox_id}"`. On resume or stale-reclaim the SDK
calls `provider.attach(exec_env_ref)` — never `allocate` — so a resumed task
reconnects to the **same** container. Credentials are **not** in the ref; the
reconnecting host rebuilds `SandboxAuth` from its own environment.

## What runs in the container

Under a sandbox, these side effects route through the session's `ExecEnv`:

- **fs tools** — `read`, `write`, `edit`, `glob`, `grep`, `apply_patch`
- **foreground shell** — `shell_run` (background shell is host-side and refused
  under a container)
- **web egress** — `webfetch` / `web_search` go out via `curl` inside the
  container
- **skill indexing** — `tree_snapshot` batches the walk into one round-trip
- **browser tools** — `browser_navigate`, `browser_click`, … via the
  `BrowserBackend` wire

Deliberately **host-side**: `memory_*` (global cross-session store), MCP,
background shell, and the app preview gateway.

## Browser tools in a sandbox

The `browser` built-in contributes the `browser_*` tool pack, but it mounts
only when **both** hold:

1. the agent activates `browser` (`"browser" in Options.plugins`), and
2. the session is bound to a live sandbox container.

Among the official presets only the `web` subagent opens `browser`; `main`
stays browser-free and delegates page work to it. Opt in with
`sandbox_browser_options()`:

```python
from noeta.presets import sandbox_browser_options

options = sandbox_browser_options()   # main + the web subagent, browser on
```

## Knobs

| `HostConfig` field | Purpose |
| --- | --- |
| `sandbox_exec_preamble` | `(exec_env_ref, argv) -> str` — prepend a per-command shell prefix (e.g. mint a fresh credential). Re-invoked every command. |
| `sandbox_backend_factory` / `sandbox_browser_factory` | Swap the container wire without touching the seam. |
| `sandbox_policy` | `(root_task_id, workspace_dir) -> bool` — per-session opt-out of the sandbox; `False` falls back to `LocalExecEnv`. |

## Known boundaries

- The library ships **no provisioner** — running `docker`, a K8s API, or a
  remote session service is the host's job. The in-box provider only attaches.
- Sandbox side effects are **not fenced** across worker generations: a worker
  whose lease expired can still `POST` to the container. Bounded by the same
  step-attempt re-drive as crashed-step side effects.
- `shell_run`'s `timeout` is enforced client-side under a sandbox — the command
  keeps running in the container after the call returns.

## See also

- [SDK reference — sandbox surface](../reference/sdk.md) — the full API
- [ADR: execution environment seam](https://github.com/initxy/noeta/blob/main/docs/adr/execution-environment-seam.md) — the design rationale
- [Known limitations](../operations/limitations.md) — the sandbox boundaries in detail
