# Run tools in a sandbox

Route an agent's file, shell, web and browser side effects into a container, so
the agent never touches the host directly. Noeta talks to the container; starting
and stopping containers is your host's job.

| Mode | `HostConfig` fields | Use it for |
| --- | --- | --- |
| **Attach** — one shared container | `exec_env=SandboxExecEnvConfig(...)` | development, simple setups |
| **Provision** — one container per root task | `sandbox_provider=...`, `sandbox_spec=SandboxSpec(...)` | production isolation |

If both are set, `sandbox_provider` wins.

## Attach to a running container

Start a container yourself (for example an
[AIO Sandbox](https://github.com/bytedance/aio-sandbox) image), then point Noeta at it:

```python
from noeta.sdk import Client, HostConfig, Options, SandboxExecEnvConfig
from noeta.sdk.providers import AnthropicProvider

client = Client(
    Options(system_prompt="You are a coding agent."),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    workspace_dir="./workspace",
    host_config=HostConfig(
        exec_env=SandboxExecEnvConfig(
            base_url="http://localhost:8080",
            api_key_env="SANDBOX_API_KEY",   # read at connect time, never stored
            workdir="/workspace",
        ),
    ),
)
```

```bash
export SANDBOX_API_KEY=your-container-key
```

Ask the agent to run `hostname` — it prints the container's id, not your
machine's. Every task shares this container.

## Provision one container per task

Implement the three-method `SandboxProvider` protocol:

```python
import subprocess

from noeta.sdk import SandboxHandle, SandboxSpec, StaticApiKeyAuth, decode_exec_env_ref


class DockerSandboxProvider:
    def allocate(self, root_task_id: str, spec: SandboxSpec) -> SandboxHandle:
        # Called once when a root task opens: start a fresh container.
        mounts = [a for m in spec.mounts for a in ("-v", f"{m.source}:{m.target}:{m.mode}")]
        container_id = subprocess.run(
            ["docker", "run", "-d", "--rm", *mounts, spec.image, "sleep", "infinity"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Wait until it is ready, then return its API address.
        return SandboxHandle(
            base_url="http://localhost:8080",
            sandbox_id=container_id,
            auth=StaticApiKeyAuth(env_name="SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        # Called on resume, possibly on another host: reconnect, never re-create.
        base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=sandbox_id,
            auth=StaticApiKeyAuth(env_name="SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def release(self, root_task_id: str) -> None:
        # Called when the root task ends. Must be idempotent.
        ...  # look up the container for root_task_id and stop it
```

```python
from noeta.sdk import HostConfig, MountSpec, SandboxSpec

host_config = HostConfig(
    sandbox_provider=DockerSandboxProvider(),
    sandbox_spec=SandboxSpec(
        image="aio-sandbox:latest",
        mounts=(MountSpec(source="/srv/skills", target="/skills", mode="ro"),),
        resources={"memory": "2g", "cpus": "2"},
    ),
)
```

- The container's address is recorded on the task as `"{base_url}#{sandbox_id}"`.
  After a crash, the task reconnects to the **same** container via `attach`.
  Credentials are never recorded; `StaticApiKeyAuth` reads them from the environment.
- `SandboxSpec` fields: `image`, `mounts`, `resources` (passed to your provisioner
  as-is), `env`. The task's workspace mount is added to `spec.mounts` for you at
  `allocate` time; list only extra mounts here.
- `MountSpec.kind` is `"local-path"` (default), `"volume"`, `"nas"` or `"pvc"`,
  so a cluster provider can map mounts to its storage.

## What runs where

| In the container | On the host |
| --- | --- |
| `Read`, `Write`, `Edit`, `Glob`, `Grep` | `memory_*` tools |
| foreground `Bash` | background shell (refused under a sandbox) |
| `WebFetch`, `WebSearch` (the container's network is the egress boundary) | MCP servers |
| skill indexing | app preview gateway |
| `browser_*` tools | |

Whether a `WebFetch` needs approval is still decided on the host, from the URL's
host — see [Built-in tools](../reference/tools.md).

## Turn on browser tools

The `browser_*` tools mount only when the agent activates `browser` **and** the
task has a live sandbox. The official presets give browsing to a `web` subagent:

```python
from noeta.sdk import presets

options = presets.sandbox_browser_options()   # main + the web subagent
```

## Knobs

| `HostConfig` field | Purpose |
| --- | --- |
| `sandbox_exec_preamble` | `(exec_env_ref, argv) -> str` shell prefix added to every command (e.g. a fresh credential) |
| `sandbox_policy` | `(root_task_id, workspace_dir) -> bool`; `False` runs that task locally |
| `sandbox_backend_factory` / `sandbox_browser_factory` | replace the container client |

## Run the host itself in Docker

- **Docker socket:** mount `/var/run/docker.sock` into the host container; the
  provider above then starts sibling containers.
- **Sidecar:** run the sandbox as another service in the same compose stack and
  attach with `SandboxExecEnvConfig(base_url="http://sandbox:8080")`.

## Limits

- Sandbox calls are not fenced by the task lease: a worker whose lease expired
  can still reach the container until it notices.
- A foreground `Bash` in the container stops like a local one: interrupt, cancel
  and close end it inside the container and the call returns at once, reading
  *interrupted*; a command that runs past its `timeout` is killed there too. A
  stopped command's partial output is not returned.
- `Read` pulls a large file from the container in 1 MiB pieces instead of one
  whole-file download; the window it shows and the audit record it keeps are the
  same as on the host.

## Next

- [Deploy](deploy.md) — workers, Postgres and Docker
- [Limitations](../operations/limitations.md) — sandbox boundaries in detail
- [ADR: execution environment](https://github.com/initxy/noeta/blob/main/docs/adr/execution-environment-seam.md)
