# Troubleshooting

What goes wrong in practice, the message noeta prints when it does, and what to change. Every
message on this page is quoted from the code; search the repository for a phrase to find where
it comes from.

## Where to look first

- **The command's own error.** The `noeta` command exits with one line starting `noeta:` for
  anything it can name (no model, no key, a refused flag). Read that line before anything else.
- **The task's end or turn failure.** A turn that cannot go on records a `TurnFailed` with a
  reason. An interactive task keeps its conversation and waits for the next message; a one-shot
  task (any sub-agent, or an agent file with `one_shot: true`) ends with `TaskEnded(status="failed", error=…)`, and
  `error` starts with the reason. The reasons are listed in [Turn failures](#turn-failures).
- **The worker's standard error.** noeta configures no logging of its own, so Python's default
  applies: warnings and errors from `noeta worker` and `noeta serve` go to standard error. Run
  them under a service manager that keeps it.
- **`noeta plugins`** lists every plugin with its source, and every plugin file that failed to
  load, marked `(failed)`, with the reason.
- **`/mcp`** in the terminal UI shows each MCP server as connected, waiting for a sign-in, or
  failed with its error.

## Starting up

| You see | Cause | Fix |
|---|---|---|
| `noeta: no model configured (--model or NOETA_MODEL)` | No setting names a model. | Pass `--model`, set `NOETA_MODEL`, or set `model` in `~/.noeta/config.toml` ([configuration](../start/configuration.md)). |
| `noeta: no API key for the anthropic provider: set NOETA_API_KEY (ANTHROPIC_API_KEY is read too), or api_key in ~/.noeta/config.toml` | The default provider has no key. | Set one of the named variables, or `api_key` (a literal or a `{env:NAME}` / `{keyring:service/user}` reference). |
| `…/config.toml: unknown top-level keys ['modle']; ignored` | A top-level key noeta does not know, often a typo; the setting it meant stays unset. | Fix the key's name ([configuration keys](../reference/config.md#top-level-keys)). |
| `no API key for provider 'x': set api_key in its [providers.x] table of …/config.toml` | A `[providers.<name>]` table has no key. | Add `api_key` to that table (a `{env:NAME}` or `{keyring:service/user}` reference works). |
| `…: {env:NAME} names an environment variable that is not set` / `…: the keyring has no secret for 'service/user'` / `…: {keyring:…} needs the keyring package` | A secret reference cannot be filled on this machine. References are filled only when the plugin or provider is made, so this shows when it is first used. | Set the variable, store the secret (`keyring set service user`), or install the `keyring` extra. |
| `noeta: this needs the server extra, which is not installed: pip install '…'` (also `postgres`, `acp`, `keyring`) | `noeta serve`, a Postgres store, `noeta acp` or keyring secrets need an optional extra. | Run the printed line. With `uv tool install --reinstall`, name every extra you use: a reinstall drops the ones it does not name. |
| `noeta: the terminal UI is its own package, noeta-tui, which is not installed: …` | `noeta` with no subcommand starts the terminal UI, which is the separate `noeta-tui` package. | Run the printed install line, or use `noeta -p` for one prompt. |

## Models and providers

noeta maps every provider failure onto a few classes (`noeta/core/errors.py`) and retries the
ones worth retrying. A retry is recorded in the log and shown by front ends as "retrying".

| Class | When | What noeta does |
|---|---|---|
| `Transient` | network errors, timeouts, HTTP 5xx (including Anthropic's 529 overloaded), a stream that broke after it began | Retries with backoff (doubling from 1 s, capped at 30 s, half of it random), up to 9 attempts in all (`DEFAULT_MAX_ATTEMPTS`, `noeta/core/retry.py`). A broken stream may be sent again in one piece. |
| `RateLimited` | HTTP 429 | Retries like `Transient`; when the provider sends `Retry-After`, it waits exactly that long. |
| `ContextOverflow` | a 413, or a 4xx whose body says the prompt is too long | Not retried as is. When the provider said the input fits and only the output cap does not, the call is made again with a smaller cap; otherwise compaction makes room (below). |
| `Fatal` | any other 4xx: a bad key, a bad request, a refusal | Not retried. The turn fails with `llm_error` and the provider's message, its body cut to 500 characters. |

After the retries run out the turn fails with `llm_error: <message> — <body>`. Read the body:
it is the provider's own words.

Messages that point at configuration rather than at the provider:

- `provider 'x' cannot be used: …` — the adapter for a `[providers.x]` table could not be
  built (no key, a bad setting). Fix the table.
- `this task runs 'model' on provider 'x', which is not configured here (providers: …)` — with
  several providers, a task records which provider its model goes to (`model_route`), and every
  worker sends it there. This worker's `config.toml` has no table of that name. Give every
  worker the same `[providers.*]` tables.
- `request carries an image but model 'm' is catalogued with supports_vision=False; refusing to
  send the image to a model that cannot read it.` — the catalog says the model has no vision.
  Switch models for that message.
- `this provider does not support image input outside a plain user turn; refusing to silently
  drop the image.` — an OpenAI-compatible endpoint got an image where its wire has no place for
  one.
- `the model call kept being interrupted` (with `llm_error`) — a model call was started three
  times without a reply being recorded (`MAX_MODEL_ATTEMPTS`): the worker died mid-call each time.
  Look at why the workers die (memory, a supervisor killing them).

A model the catalog does not know gets a 128,000-token window and a 16,384-token output cap,
with a warning. If compaction starts too early or too late for a gateway model, give its limits
in a models file or the provider's table ([models](../guides/models.md)).

## Context overflow and compaction

When a request does not fit, the compaction plugin summarizes the conversation and the call is
made again. You only see a failure when that cannot work:

| Reason | Message | What to do |
|---|---|---|
| `context_overflow` | the provider's overflow text | No plugin could make room (compaction is off, or the agent does not run it). Turn compaction on, or start a new task. |
| `autocompact_thrashing` | `Automatic compaction stopped: the context filled up again within 3 model calls of the previous compaction, 3 times in a row. A file or tool output is probably too large for the context window; read it in smaller parts, or start a new conversation.` | One file or output is close to the whole window. Ask for it in parts. |
| `compaction_summary_failed` | why the summary call failed | The summarize call itself failed (it is retried up to three times when it is too long). After three failed automatic compactions in a row, automatic compaction is off for the task; `/compact` still works. |
| `compaction_config_invalid` | the bad value | `autoCompactWindow` or `NOETA_AUTOCOMPACT_PCT_OVERRIDE` is not usable for this model. Fix the agent file or settings. |

See [context, caching and compaction](../how-it-works/context.md) for the mechanism.

## Turn failures

The reasons a turn fails with (`noeta/core/runner/texts.py` and the plugins that raise
`TurnFailed`). Front ends show them; hosts branch on the prefix.

| Reason | Meaning |
|---|---|
| `llm_error` | The provider failed after retries, or rejected the request. |
| `llm_empty_response` | The model returned no text and no tool call. |
| `llm_truncated` | `the response kept hitting the output token limit`: three output-cap recoveries in a row. Raise the model's output cap or ask for smaller steps. |
| `context_overflow`, `autocompact_thrashing`, `compaction_summary_failed`, `compaction_config_invalid` | See above. |
| `max_turns` | `Reached maximum number of turns (n)`: the task's `max_turns` (`--max-turns`) was reached. |
| `needs_human` | `the sub-agent stopped without completing (it needed an interaction that is unavailable to a background agent)`: a task a host marked `unattended` reached an approval or a question. Give it permission rules that cover what it does, or do not mark it unattended. |
| `blocked_by_hook`, `stopped_by_hook` | A `UserPromptSubmit` hook blocked the prompt, or a hook stopped the turn. The detail is the hook's reason. |
| `max_structured_output_retries` | With an output schema, the model failed to produce a valid `StructuredOutput` answer within the retry cap. |
| `internal_error` | A bug in the runner, a plugin hook or a provider adapter. A second one in a row ends the task. The worker's standard error has the traceback. |

A task that stops at its step limit is not a failure: the model is told `You have reached the
limit of {n} steps for this task…` and its last answer starts `Stopped at the {n}-step limit
before finishing.`

## A tool call says "outcome unknown"

```
Interrupted before its result was recorded, so its outcome is unknown — it may have fully,
partly, or not applied. Check its effect before running it again.
```

The worker that ran the call died (or lost its claim) after logging `ToolStarted` and before
logging the result. noeta never runs such a call again by itself unless the tool can look its
outcome up (`Tool.recover`) or says it is safe to repeat (`retry_safe`). The model reads this
text and usually checks. Nothing needs fixing unless workers die often
([recovery](../how-it-works/recovery.md)).

A background `Bash` job whose worker died is reported as `Background command "…" was lost:
the worker process running it stopped, so its further output and exit status are unknown`
(`Monitor "…" was lost: …` for a monitor). A job
lives in one worker process; there is no way to reattach to it.

## Plugins

| You see | Cause | Fix |
|---|---|---|
| `(failed) <path>: <error>; install it next to noeta: uv tool install --reinstall --with <module> '…'` (from `noeta plugins` or `/reload-plugins`) | A plugin file imports a module noeta's environment lacks. `noeta plugin add` installs no dependencies on purpose. | Run the printed line, naming every extra and `--with` you already use. |
| `plugin file <path>: 'name' joins no agent; set add_to = ("main",) on its class, or [agents.main] add_plugins = ["name"] in config.toml` | The plugin loaded, but no agent runs it. | Do one of the two things the notice says ([write a plugin](../guides/plugins.md)). |
| `<project>/.noeta/plugins was not loaded: this project is not trusted (run `noeta trust` in <project> to load its plugins)` | A project's `.noeta/plugins`, `.noeta/agents`, `.noeta/prompts`, `.noeta/skills` and `.agents/skills` load only once you trust it. | Run `noeta trust` in the project, or answer the terminal UI's trust question. |
| A plugin never appears, with no error | The machine's `/etc/noeta/config.toml` `[policy]` blocks it (`blocked_plugins`), or `allowed_plugins` is set and does not name it (it limits only plugins noeta does not ship). The policy wins over every user and project setting. | Ask whoever manages the machine. |
| A task ends with `plugin_missing: this task runs with <names>, which the worker that claimed it does not have; install them there (or run the task where they are)` | See below. | Install the plugin on every worker. |

### A worker without a plugin the task needs

A task records which plugins it runs with when it starts (`records.Plugins`). A worker that
lacks one of them never runs the task without it: it writes nothing, logs `task …: runs with …,
which this worker does not have; leaving it to a worker that has them`, and lets its lease run
out so another worker can claim the task. If the same worker claims it three times in a row
(`LOAD_ATTEMPTS`) with nothing new in the log, it ends the task failed with `plugin_missing`.

<Diagram name="ops-plugin-missing" />

This is what a rolling install looks like when it takes too long, or when one machine was
forgotten. Ship the same plugins (and the same `~/.noeta`) to every worker
([run it as a server](../guides/server.md)). The count is kept in each worker's memory, so a
worker restart starts it again.

A task's log that no claim can read or fold (three claims in a row) ends with
`unreadable_log: the task's log could not be read on 3 claims in a row; last error: …`. A log
written by a newer noeta is not counted: an older worker leaves it alone for a newer worker.

## The store

| You see | Cause | Fix |
|---|---|---|
| `schema 'noeta' is at version N; this noeta knows M (stop every worker and upgrade)` (Postgres), `sqlite store <path> is at schema version N; …` | A newer noeta already moved the store forward. An older process refuses to open it. | Upgrade this machine. Workers of different versions must not share a store: stop them all, upgrade, start them. |
| `cannot open the postgres store: …`, `no postgres connection: …`, `postgres connection lost: …` | Postgres is unreachable. | Check `NOETA_STORE` and the network. Nothing was written; the call can be made again. |

A write whose connection dropped while it was committing may or may not have landed
(`CommitUnknown`). The worker stops running that task as if it had lost its claim; whoever claims
it next reads the log. You may see the task pause for one lease (30 seconds by default).

## The server and its clients

| You see | Cause | Fix |
|---|---|---|
| `noeta: the server at <url> needs its bearer token: set NOETA_SERVER_TOKEN to the token noeta serve printed when it made it` | The client has no token. | Set `NOETA_SERVER_TOKEN` on the client. |
| `noeta: the server at <url> refused the token: …` / HTTP 401 `{"error": "unauthorized"}` | Wrong token. The server keeps only its SHA-256 in `~/.noeta/server-token`, so it cannot show it again. | Find the token you saved when `noeta serve` first printed it, or delete `~/.noeta/server-token` and restart the server for a new one (printed once). |
| HTTP 403 `{"error": "cross-site request refused"}` | The `Host` header is not one the server answers (on a loopback bind only the loopback names; elsewhere only `--allow-host` names when any are given), or a browser page from another origin sent the request. | Add `--allow-host NAME` for the name clients use, or `--allow-origin ORIGIN` for a page served elsewhere. |
| `noeta: --no-token serves only a loopback address` | `--no-token` with `--host` other than loopback. | Keep the token, or bind to `127.0.0.1`. |
| `this server does not allow bypassPermissions (start it with --allow-bypass-permissions)` | `noeta serve` and `noeta worker` refuse bypass mode by default. In a running task the switch is refused with the notice `bypassPermissions is disabled here`. | Start every worker and server with `--allow-bypass-permissions` if you mean it. |
| `noeta: --permission-mode bypassPermissions needs --allow-bypass-permissions` | `noeta serve` or `noeta worker` was started in bypass mode without allowing it. | Add `--allow-bypass-permissions`, or pick another mode. |
| `noeta: on 0.0.0.0 any Host header is answered; name the names this server is reached by with --allow-host (DNS-rebinding protection)` | `noeta serve` on an address that is not loopback, without `--allow-host`. A warning: the server runs, the token still guards it. | Add `--allow-host NAME` for each name clients use. |
| HTTP 409 `task … is not waiting on a request '…' (unknown, or already answered)` on `POST /tasks/{id}/answers` | The `request_id` is a typo, from a stale card, another task's, or already answered and taken. | Read `GET /tasks/{id}/pending` again and answer what is there. |
| A `/stream-json` WebSocket closed with code `4401` / `4403` | `4401`: no valid token. `4403`: the permission mode or `cwd` asked for is refused; the close reason says why. | Pass the token (`?token=` or the header); ask for an allowed mode or directory. |
| `cwd '…' is outside this server's directories (…); start it there or add the directory with --allow-root` | A client asked for a task in a directory the server does not serve. | Add `--allow-root PATH` to `noeta serve`. |
| `a request may not set the task config key 'x' (only model, effort, …)` | Only some config keys may come from a client; the rest would loosen permission gates. | Set the key in the server's own config or agent file. |
| `the request failed on the server; its log has the details` | An error raised outside noeta's own code. Its text is not sent, since it may name server paths. | Read the server's standard error. |
| `noeta: -p runs its task here; --server attaches the terminal UI (or noeta acp --server) to a server` | `-p` always runs locally. | Drop `--server`, or use the terminal UI or `noeta acp --server`. |
| `noeta: --plugin-dir, … apply to the tasks this process runs; with --server the server's settings and plugins run` | A remote task runs with the server's plugins, permission rules, settings and MCP servers. | Set them on the server, or drop `--server`. |

Live text not appearing across machines while recorded events do: live output goes through
Postgres notifications (`<schema>_deltas`), best effort and never stored. A front end that
attached after a reply began sees the recorded reply when it lands. On sqlite, live text stays in
the process running the task.

## MCP servers

`/mcp` lists every server: `✔ name · connected · N tools`, `△ name · needs sign-in`, or
`✘ name · failed: <error>`. `noeta mcp list` checks them from the command line.

| You see | Cause | Fix |
|---|---|---|
| `noeta mcp list` shows ``project not trusted (not started; run `noeta trust` here)`` | A `local` or `project` server of a project not yet trusted; it is not started, not even to check it. | Run `noeta trust` in the project. |
| A project's server in `.noeta/mcp.json` never starts | A project's servers start only in a trusted project and with consent in its own settings. `noeta mcp add` says: `It starts once you consent: add 'name' to enabledMcpjsonServers in …`. | Run `noeta trust`, then add the name to `enabledMcpjsonServers` (or set `enableAllProjectMcpServers: true`) in `.noeta/settings.local.json`. |
| `MCP tool name 'x' is produced by both server 'a' and server 'b'; rename one alias` | Two servers produce one tool name. | Rename one server in your config. |
| `MCP server 'x' needs exactly one of command and url` | A config entry has both or neither. | Fix the entry. |
| `spawn failed: …` | A stdio server's command could not start. | Check `command` and `PATH` on the worker. |
| `The 'x' MCP server needs a sign-in. …` (to the model), `△ x · needs sign-in` | An HTTP server wants OAuth. | `noeta mcp login x`, or follow the link the model gives you. |
| A server's tools are missing from requests | MCP tools are deferred: the model loads them with `ToolSearch` when it needs them. A tool whose name passes 128 characters is never offered (logged once per listing). | Set `alwaysLoad: true` on the server to send its tools from the first request. |
| A call or listing times out | Listing, prompts and resources have 30 s (`NOETA_MCP_TIMEOUT`, ms); tool calls have no practical limit unless the server's `timeout` (ms) or `NOETA_MCP_TOOL_TIMEOUT` sets one. | Set the server's `timeout` or the variable. |
| No MCP tools at all from an SDK host using `default_agents` | The built-in agents keep MCP, memory and the repetition guard off until a setting names them (`OFF_UNLESS_SET`, `noeta/agents/__init__.py`). The `noeta` command sets them for you. | Pass a setting for `mcp` (`plugins={"mcp": {...}}`). |

More in [MCP servers](../guides/mcp.md).

## The sandbox

| You see | Cause | Fix |
|---|---|---|
| `sandbox.enabled is set but the sandbox cannot start: bubblewrap (bwrap) is not installed; install it (apt install bubblewrap, dnf install bubblewrap) to run commands in the sandbox. Commands run without it.` | Linux without `bwrap`. With `failIfUnavailable` the tail is `Commands that would run in it are refused (failIfUnavailable).` | Install bubblewrap. |
| `… bwrap cannot create the sandbox here: …` | `bwrap` exists but cannot create namespaces: a container without user namespaces, or a kernel setting that forbids them. | Allow unprivileged user namespaces, or run noeta outside that container. |
| `… the sandbox's system-call filter (refusing Unix sockets and new user namespaces) does not support the <arch> architecture` | The filter knows only x86_64 and aarch64. | Set both `sandbox.network.allowAllUnixSockets` and `sandbox.allowNestedUserNamespaces` (the filter is then not needed), or turn the sandbox off on that machine. |
| `the sandbox is required (sandbox.failIfUnavailable) but cannot start: <reason>. The command was not run, and running it outside the sandbox (dangerouslyDisableSandbox) is refused too.` (to the model) | `failIfUnavailable` is on and the sandbox cannot start. | Fix the reason above. |
| `the sandbox does not allow connecting to host:port; add it to sandbox.network.allowedDomains to watch this socket` | A `Monitor` WebSocket to a host the sandbox does not allow. | Add the host to `sandbox.network.allowedDomains`. |

Without `failIfUnavailable`, a sandbox that cannot start leaves commands running unconfined,
with the notice above at start. See [where commands run](../guides/sandbox.md).

## Optional tools that do not appear

- **`WebSearch`** is offered only with a search backend key, `NOETA_WEB_SEARCH_API_KEY` (Tavily).
- **`Workflow`** runs scripts in an embedded V8 from the `mini-racer` package. Without it the tool
  refuses with `the mini-racer package is not installed (…)` or `mini-racer's V8 library does not
  load here (…)` and starts nothing.
- **The task tools** (`TaskCreate` and the rest) are offered only to the models Claude Code gives
  them to, unless `[plugins.todos] all_models = true`.
