# Where commands run

Every file a tool reads or writes and every command it runs goes through the task's **exec env**
(`ExecEnv`, `noeta/core/execenv.py`). This page says which exec envs exist, what the command
sandbox does and does not protect, how to choose between them, and how to configure each.

## The exec env

A tool never calls `open()` or `subprocess` itself. It asks the task's exec env:
`run(argv, cwd=…, env=…, timeout=…)`, `read_bytes`, `read_range`, `write_bytes`, `stat`,
`realpath` and a few more. The runtime picks the env per task with `env_for(state)`, a
`Runtime` argument. The `noeta` command's `env_for` gives every task a local env on its `cwd`
(`noeta/cli/config.py` `local_env_for`), with the sandbox when the settings enable it.

<Diagram name="guide2-execenvs" caption="Tools see one interface; env_for picks the implementation per task." />

Because everything goes through the env, a task's own files go there too: long tool outputs saved
for the model (the spill directory), plan files, auto memory and sub-agent transcripts are written
through it. They live where the task's tools live, not on the worker that happened to run a step.

There are three implementations:

| Exec env | Entry point (`noeta.execenvs`) | Where commands run | Who uses it |
|---|---|---|---|
| `LocalExecEnv` | `local` | child processes on this machine, as your user | the default everywhere |
| `LocalExecEnv` with a `LocalSandbox` | `local` (with `sandbox=`) | `Bash` commands inside bubblewrap (Linux) or Seatbelt (macOS); file tools as above | the settings' `sandbox.enabled` |
| `ContainerExecEnv` | `container` | one `docker exec` per operation, in a container started beforehand | host code, through `Runtime(env_for=…)` |

A third-party exec env is a class in the `noeta.execenvs` entry-point group, constructed as
`factory(cwd, **kw)` (`noeta/core/registry.py` `execenv`).

## The local env

`LocalExecEnv` (`noeta/backends/execenvs/local.py`) runs each command as its own process group,
with stdin closed. On timeout or cancellation the whole group gets SIGTERM, then SIGKILL after
5 seconds, which also reaps grandchildren a command put in the background. Output is bounded
while it is read.

A command can do anything your user can. What it inherits is your environment **minus noeta's own
credentials** (`noeta/toolkit/local_env.py`): `NOETA_API_KEY`, `NOETA_STORE`,
`NOETA_SERVER_TOKEN`, `NOETA_MCP_CLIENT_SECRET`, `NOETA_WEB_SEARCH_API_KEY`; the libpq variables
(`PGPASSWORD`, …) when the store is Postgres; and every variable a `{env:NAME}` reference in
noeta's configuration names. The same applies to hooks and stdio MCP servers.

Without the sandbox, what keeps a command in check is the permission layer: which commands need
your approval, the deny rules, and the protected paths it always asks about. That is
[Permissions and hooks](./permissions.md). Two shell checks from there matter here:

- **Deny rules reach into commands.** A `Read(...)` deny rule also refuses a `Bash` command
  that reads a matching path (`cat`, a `<` redirection, …), and an `Edit(...)` deny rule refuses
  a command that writes one, or that removes or moves a directory holding one. The command is
  read leniently, including subshells, heredocs and substitutions; globs are expanded and links
  resolved through the env (`noeta/plugins/permissions/deny_paths.py`, `rule_check.py`). No
  mode gets past this, `bypassPermissions` included.
- **noeta's own config directory.** A command that names `.noeta` is asked about in every mode
  but `bypassPermissions`.

These are checks on the text of a command. They catch mistakes; they do not stop a command that
is written to hide what it touches. For that you need the sandbox or a container.

## The sandbox

The sandbox is Claude Code's `sandbox` settings object, implemented with bubblewrap on Linux and
Seatbelt (`sandbox-exec`) on macOS (`noeta/backends/execenvs/sandbox.py`,
`noeta/toolkit/sandbox.py`). It confines **`Bash`**: foreground commands, background jobs and
`Monitor` scripts. The file tools (`Read`, `Edit`, `Write`, `Grep`, `Glob`) are not run inside
it; the permission rules govern them, and `Edit(...)` allow rules, `Read(...)` deny rules and
`WebFetch(domain:…)` allow rules feed the sandbox's own lists so both agree.

<Diagram name="guide2-sandbox" caption="What a sandboxed command can write, cannot read, and can reach." />

What a sandboxed command may do:

- **Read** everything except the credential stores in your home directory (`~/.ssh`,
  `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.config/gcloud`, `~/.kube`, `~/.docker/config.json`,
  `~/.config/gh`, `~/.netrc`, `~/.git-credentials`, `~/.pypirc`, `~/.npmrc`, cargo credentials,
  `~/.pgpass`, `~/.password-store`), noeta's own home directory (`~/.noeta`: the token, the task
  store; its saved tool outputs, project memory and pasted images stay readable),
  `filesystem.denyRead`, `Read(...)` deny rules and `credentials.files` entries.
  `filesystem.allowRead` opens a path inside one of them again. A denied directory holding a
  writable one (`denyRead: ["~"]` with the project in `~/proj`) stays denied, the writable one
  shown inside it.
- **Write** only in the working directory, `filesystem.allowWrite`, the paths of `Edit(...)`
  allow rules and `additionalDirectories`, and a private `$TMPDIR`. Never in
  `filesystem.denyWrite`, and never in the **protected paths**.
- **Connect** only through a filtering proxy that noeta runs outside the sandbox, and only to
  `network.allowedDomains` (`*.example.com` covers subdomains, `host:port` one port;
  `deniedDomains` wins). With no allowed domains, no host answers. The hosts it refused are
  listed after the command's output in `<sandbox_violations>`. An allowed name that resolves to
  a loopback, link-local or cloud-metadata address is refused too. With
  `network.httpProxyPort` / `socksProxyPort`, your own proxy is used instead.
- **Variables** named in `credentials.envVars` are unset.

On Linux the command also gets its own network, PID and mount namespaces, with relays to the
proxy and a seccomp filter against Unix sockets and new user namespaces.

### Protected paths

Some paths in a project run code later, outside the sandbox and without anyone confirming: git
hooks and config, `.noeta/` (settings, hooks, plugins), `.vscode`, `.husky`, shell rc files,
`.npmrc`, `.yarnrc`, `.cargo`, `.mvn`, `.bazelrc`, `.pre-commit-config.yaml` and more
(`noeta/toolkit/protected_paths.py`, `noeta/backends/execenvs/sandbox_plan.py`). A command that
wrote one would escape the sandbox at your next ordinary `git commit` or `npm install`.

So inside the sandbox these paths are read-only, and the ones that would run without a
confirmation step are covered, when missing, by an empty read-only placeholder for the
command's lifetime, so they cannot be created either. No directory above one can be renamed
away. They are looked for up to three levels below the working directory too. The reasons and
the rejected alternatives are in
[ADR-008](https://github.com/initxy/noeta/blob/main/docs/adr/008-sandbox-protected-path-placeholders.md).

Consequences you will notice: `git init` fails in a directory that is not a repository, and
nothing under `.noeta/` can be written by a command (the file tools may still change
`.noeta/skills` and `.noeta/commands` under the permission rules). Placeholders are hidden from
git when the working directory is a repository's top.

### Turning it on

In a settings file (`~/.noeta/settings.json`, a trusted project's `.noeta/settings.json` or
`.noeta/settings.local.json`, or the machine's `managed-settings.json` in `/etc/noeta` on
Linux):

```json
{
  "sandbox": {
    "enabled": true,
    "filesystem": {
      "allowWrite": ["~/.cache/pip"],
      "denyRead": ["~/secrets"]
    },
    "network": {
      "allowedDomains": ["pypi.org", "*.pythonhosted.org", "github.com"]
    },
    "excludedCommands": ["docker *"]
  }
}
```

On Linux install bubblewrap first (`apt install bubblewrap` or `dnf install bubblewrap`). If the
sandbox is enabled but cannot start, noeta says why when it starts and commands run without it,
unless `failIfUnavailable` is true: then they are refused.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | confine `Bash` |
| `failIfUnavailable` | `false` | refuse commands when the sandbox cannot start |
| `autoAllowBashIfSandboxed` | `true` | a command that runs sandboxed needs no approval (except in plan mode) |
| `allowUnsandboxedCommands` | `true` | a call may set `dangerouslyDisableSandbox: true` to leave the sandbox; that always asks you (off: the flag is ignored and the command stays inside) |
| `excludedCommands` | `[]` | command patterns (`docker *`, `Bash(npm *)`) that always run outside |
| `filesystem.allowWrite` / `denyWrite` / `denyRead` / `allowRead` | `[]` | the path lists above |
| `filesystem.disabled` | `false` | network filtering only |
| `network.allowedDomains` / `deniedDomains` | `[]` | the host lists above |
| `network.allowUnixSockets`, `allowAllUnixSockets`, `allowLocalBinding` | off | Unix sockets and binding local ports (macOS; `allowAllUnixSockets` also lifts the Linux filter on Unix sockets) |
| `network.httpProxyPort` / `socksProxyPort` | none | use your own proxy |
| `credentials.files` / `credentials.envVars` | `[]` | entries with `mode: "deny"` (or `"mask"`, treated as deny) hide a file or unset a variable |
| `enableWeakerNestedSandbox` | `false` | Linux: no fresh `/proc`, for hosts that cannot mount one |
| `allowNestedUserNamespaces` | `false` | Linux: commands may create their own user namespaces |
| `bwrapPath` | `PATH` | an absolute `bwrap`; read from managed settings only |

Lists add up across files. A project's files can only **tighten** the sandbox: they can turn
`enabled` and `failIfUnavailable` on and `allowUnsandboxedCommands` and
`autoAllowBashIfSandboxed` off, never the other way, and the keys and lists that would open it
up (`excludedCommands`, `allowWrite`, `allowRead`, `allowedDomains`, the socket and proxy keys,
the nested-sandbox switches, `filesystem.disabled`) are ignored there with a warning
(`noeta/cli/settings_files.py` `merge_sandbox`).

The model is told what the sandbox allows in a `sandbox_instructions` note, sent again whenever
it changes, and told when it is off again.

## The container env

`ContainerExecEnv` (`noeta/backends/execenvs/container.py`) runs every operation as one
`docker exec` into a running container: commands, and file reads and writes (through `cat`,
`stat`, `realpath`, a temp file and a rename). A command runs as its own process group inside
the container, so a timeout kills it there, not just the local `docker` client. The host's
environment never reaches a command: variables you pass travel in the client's environment,
never on its command line. The image needs `sh`, GNU coreutils, util-linux `setsid` and `bash`
(any Debian or Ubuntu image; not busybox or Alpine).

Starting containers is the host's job; `start_container`
(`noeta/backends/execenvs/container_provision.py`) is a helper with secure defaults: no network,
a read-only root with writable scratch (`/tmp`, `/var/tmp`, `/run`, `/root`), a reduced
capability set, `no-new-privileges`, at most 1024 processes, and optional `memory` / `cpus`
limits. Given `sandbox` settings with `network.allowedDomains`, the container joins an internal
network of its own whose only way out is a filtering proxy this process serves on the network's
gateway (`ensure_container_proxy` serves it again after a restart). The filesystem keys have no
counterpart: the container sees only its mounts.

The `noeta` command has no setting that selects the container env. You wire it in host code:

```python
import asyncio
import noeta
from noeta.backends.execenvs.container import ContainerExecEnv, start_container

async def main() -> None:
    name = await start_container(
        "python:3.12-bookworm",
        mounts=[("/srv/checkouts/app", "/workspace")],
        sandbox={"enabled": True, "network": {"allowedDomains": ["pypi.org"]}},
    )
    store = noeta.store("sqlite:///tasks.db")
    agents = {a.name: a for a in noeta.default_agents("claude-sonnet-5-5")}
    model = noeta.provider("anthropic", api_key="sk-ant-...")
    runtime = noeta.Runtime(
        store=store, agents=agents, provider_for=lambda _name: model,
        env_for=lambda state: ContainerExecEnv(name, cwd=str(state.config.get("cwd") or "/workspace")),
    )
    client = noeta.Client(store)
    await client.create("main", "Run the tests and fix what fails.", config={"cwd": "/workspace"})
    await runtime.run_until_idle()

asyncio.run(main())
```

`env_for` is called for each task, so it can give each task (or each party) its own container.
A task's `cwd` is then a path inside the container.

## Choosing one

| Situation | Use |
|---|---|
| You at your own machine, watching | the local env; turn the sandbox on if you let commands run without asking |
| Unattended runs on your machine (`-p`, CI) | the local env with the sandbox, `failIfUnavailable: true` and a short `allowedDomains` list |
| Several workers on different machines | a shared filesystem at the same path on every worker, or a container env every worker reaches (see [Run it as a server](./server.md)) |
| Untrusted code, or parties that must not see each other | a container per task or per party, from host code |

On several machines the exec env must look the same from every worker, because a task may
continue on another worker after any step. With a container env, the task's files live in the
container and nothing else needs sharing (noeta's home path must be the same on every worker and
writable in the container). With the local env on a shared filesystem, noeta's home directories
for saved outputs, plans and memory must be on the shared mount too.

Two things stay local to one worker whatever the env: a background `Bash` job (its task keeps
its claim on that worker while the job runs, see
[Sub-agents and background work](./subagents.md#background-shell-commands)) and the filtering
proxy of a sandbox or a sandboxed container, which runs in the process that started it.

## When things go wrong

- **`bubblewrap (bwrap) is not installed`**, **`bwrap cannot run: …`**, or
  **`bwrap cannot create the sandbox here: …`** (no user namespaces):
  the sandbox cannot start. Commands run unconfined (with a notice at start) or, with
  `failIfUnavailable`, are refused with that reason. On macOS a missing `/usr/bin/sandbox-exec`
  does the same; other platforms have no sandbox.
- **A command fails writing a dotfile or `.git/hooks`.** That is a protected path; run that step
  yourself, or outside the sandbox with approval.
- **A download fails inside the sandbox.** Look for `<sandbox_violations>` in the output and add
  the host to `network.allowedDomains` in your own settings (a project cannot add it).
- **`docker` is not found or the container is gone.** `ContainerExecEnv` raises
  `ContainerError`; the tool call fails with that error and the model sees it.
- **A worker dies mid-command.** The command's outcome is unknown; the next worker tells the
  model so instead of running it again
  ([One writer and crash recovery](../how-it-works/recovery.md)).
