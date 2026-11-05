# Python API

The names importable from `noeta` (`noeta/__init__.py`), with their signatures, grouped by what
they are for. They load on first use, so `import noeta` stays cheap. Everything else is
imported from its module (`noeta.core.*`, `noeta.toolkit.*`, `noeta.plugins.*`,
`noeta.backends.*`, `noeta.host.*`, `noeta.agents`); `noeta.cli` is the `noeta` command, not
part of the SDK. A walk-through is on [Embed it in Python](../start/embedding.md); the plugin
side is in [Extension interface](./extension.md).

## Making the parts

### `noeta.store(url) -> Store`

The store for `url`, chosen by its scheme through the `noeta.stores` entry points:
`sqlite:///path` (`~` expanded, parent directories made), `postgres://…` or
`postgresql://…` (the `postgres` extra). An unknown scheme raises `ValueError`.

### `noeta.provider(family, **kw) -> Provider`

The provider adapter for `family` (the `noeta.providers` entry points: `anthropic`,
`openai_compat`, `openai_responses`, or a third party's). Keywords go to its factory; the
built-ins take `api_key=`, `base_url=` and `extra_headers=`. `openai_compat` needs
`base_url` (the API root that `/chat/completions` is appended to, e.g. `…/v1`); `openai_responses` needs both `base_url` (the whole
`…/v1/responses` URL) and `api_key`. Without `api_key`, `anthropic` reads
`ANTHROPIC_API_KEY` and `openai_compat` reads `OPENAI_API_KEY`; `anthropic` also takes
`prompt_cache_ttl="1h"`.

### `noeta.default_agents(model, *, plugins=None, add=None, extensions=(), extra_files=(), made=None, **main_kw) -> list[Agent]`

`main` and the sub-agents `Explore`, `Plan` and `general-purpose`, built from the agent files
in `noeta/agents/files/`, plus the agents of `extra_files` (a file named like a built-in
replaces it).

| Argument | Meaning |
|---|---|
| `model` | the model every agent runs unless its file names another; a file with `model: inherit` (or no `model`) runs on it, and as a sub-agent on its caller's model |
| `plugins` | per plugin name: its arguments (a mapping; `enabled: False` turns it off), a ready `Extension`, or `False` to leave it out of every agent. `memory`, `mcp` and `repetition_guard` stay off until this names them |
| `add` | per agent name (`"*"` for all): more plugins after the file's own, as `(name, arguments)` pairs |
| `extensions` | `Extension` instances added to every agent |
| `extra_files` | more `AgentFile`s (from `read_agent_dir` / `parse_agent_file`) |
| `made` | a dict that keeps the plugins made, so a second build reuses the unchanged ones |
| `**main_kw` | fields of `main` to replace, e.g. `one_shot=True`, `system_prompt=…`, `name=…` |

### `noeta.parse_agent_file(text, *, source="", default_name="") -> AgentFile`

Reads one agent file: YAML frontmatter in Claude Code's fields (plus `plugins`, `one_shot` and `identity`)
and the system prompt as the body. Raises `AgentFileError` on a malformed file.

### `noeta.read_agent_dir(path) -> list[AgentFile]`

Every `*.md` agent file in `path` (not recursive). A file that cannot be read is logged and
skipped.

### `noeta.Agent`

A dataclass; what a `Runtime` runs.

| Field | Default | Meaning |
|---|---|---|
| `name` | — | the agent's name, as tasks record it |
| `model` | — | the model id |
| `system_prompt` | `""` | the system prompt (the prompt name `system/<agent>`) |
| `identity` | `""` | a one-line identity sent as its own cached system block ahead of the prompt |
| `description` | `""` | its line in another agent's sub-agent roster |
| `extensions` | `()` | its plugins |
| `tools` | `()` | tools of its own, besides the plugins' |
| `effort` | `None` | `low` … `max` |
| `max_tokens` | `None` | the output cap; `None` takes the catalog's |
| `one_shot` | `False` | end the task when its first turn ends instead of waiting for the next message |
| `output_schema` | `None` | the JSON Schema answers follow when the task sets none |
| `step_budget` | `1000` | model calls allowed in one turn; the next asks for the final answer with tools off |
| `extra` | `{}` | the agent file's other fields, as written |
| `allowed_tools` | `None` | only these tool names are offered (`None`: all) |
| `denied_tools` | `()` | tool names never offered |

`all_extensions()` is the list a task runs: `extensions`, with `tools` wrapped as a `ToolSet`
ahead of them; it raises `ValueError` when two plugins share a name or one has none.

## Running tasks

### `noeta.Runtime`

A worker: claims tasks from the store and runs them, many at once. A dataclass:

| Field | Default | Meaning |
|---|---|---|
| `store` | — | the store |
| `agents` | — | `dict[name, Agent]` |
| `provider_for` | — | `model id -> Provider` |
| `env_for` | the local machine at the task's `cwd` | `TaskState -> ExecEnv`: where a task's tools run |
| `task_provider` | `None` | `(state, model) -> Provider`, to follow a route the task's config records |
| `prepare_child` | `None` | completes a child task's `TaskCreated` before it is written |
| `worker` | `host:pid:random` | this worker's name in claims |
| `queues` | `("default",)` | the queues it claims from |
| `lease_seconds` | `30.0` | how long a claim lasts without renewal |
| `poll_interval` | `0.5` | seconds between looks for claimable work, when no change wakes it |
| `concurrency` | `8` | tasks run at once |
| `spill_root` | `None` | where tool outputs too long to return inline are saved; `None` cuts them |
| `prompt_dirs` | `()` | host directories of prompt override files |
| `workspace_trusted` | `None` | `cwd -> bool`: may a task's `.noeta/prompts` override prompts (`None`: every workspace) |
| `on_delta` | `None` | `(task_id, Delta) -> None` for live output |
| `output_ceiling` | the catalog's | `model -> max_tokens` when the agent sets none |
| `snapshot_every` | `100` | events between fold snapshots; `0` never |
| `agent_aliases` | `{}` | a former agent name -> its name now, for tasks recorded under the former |
| `roots` | `None` | serve only these task trees (a set the front end adds to); `None` serves every task |
| `sleep` | `asyncio.sleep` | how it waits between model retries (tests pass a fake) |

Methods:

| Method | Does |
|---|---|
| `await run_until_idle()` | claim and run tasks until none is claimable |
| `await run_once() -> bool` | claim one task and run it until it waits or ends; `False` when nothing was claimable |
| `await serve(stop: asyncio.Event)` | run tasks as they become claimable until `stop` is set; running tasks are handed back on the way out |
| `await let_go()` | give up the claims kept over waits (tasks owning a background job); the tasks stay waiting |
| `await preview(task_id) -> ModelRequest \| None` | the request the task's next model call would start from, recording nothing |
| `serve_root(root_id)` | add a task tree to `roots` and look for work at once |
| `agent(name) -> Agent \| None` | the agent by name or former name |
| `provider_of(state, model) -> Provider` | the provider a task's call goes to: `task_provider` when set, else `provider_for` |
| `await run_claim(claim)` | run one claim the caller already holds until the task waits or ends |

### `noeta.LocalApp(store, agents, provider, *, own_tasks_only=False, task_config=None, channels=(), **runtime_kw)`

A store, a `Runtime` serving in the background and a client, in one process: what the terminal
UI, `noeta acp`, `noeta -p` and `noeta worker` run on (`noeta/host/local.py`).

| Argument | Meaning |
|---|---|
| `agents` | a list of `Agent` |
| `provider` | a `Provider`, or a `model -> Provider` function |
| `own_tasks_only` | serve only the task trees this app created or adopted |
| `task_config` | laid under every new task's config (`prompts` merged key by key) |
| `channels` | `Channel`s to run while started |
| `**runtime_kw` | passed to `Runtime` (`spill_root` defaults to `noeta-<uid>/tool-results` in the system temp directory; `poll_interval` defaults to 0.2 and the client polls at the same interval; a caller's own value wins) |

| Member | Is |
|---|---|
| `client` | a `LocalClient` (a `Client` subclass) whose new tasks this app's worker serves; it lays `task_config` under each new task and puts an agent file's `initialPrompt` ahead of a new conversation's first message |
| `runtime`, `store` | the parts |
| `await start(worker=True)` / `await stop()`, or `async with` | run the worker, the channels and the daily sweep of old saved tool outputs in the background; stopping also closes the store |
| `await run_until(stop)` | `start()`, run until the `asyncio.Event` `stop` is set (or the worker fails, which is raised), then `stop()`: a process that only runs tasks, as `noeta worker` does |
| `deltas(sink) -> unsubscribe` | live output: `sink(task_id, Delta)` per fragment, also from other machines on Postgres |
| `await adopt(task_id)` | serve an existing task's tree (resuming it); raises `UnknownAgent` for an agent this app lacks |
| `replace_agents(agents)` | run these agents from the next pickup on |
| `await agent_names()` | the agents' names, sorted |
| `await commands(agent, ctx)`, `await run_command(agent, ctx, text)` | an agent's slash commands |
| `await context(task_id)` | what fills the task's next request, by category |
| `await summaries(limit=50, archived=None)`, `await archive(task_id, archived=True)` | the root tasks for a resume list |
| `notices`, `main_agent` | what a front end should show; the agent to start with (`"main"`) |
| `channels` | the channels it runs |
| `reload_plugins`, `list_models`, `prepare_model` | hooks a host sets (the `noeta` command does): find the plugins again, list every provider's models, settle a model before a switch; `None` otherwise |

### `noeta.Client(store, *, poll_interval=0.5)`

The client API on a store, in this process (`noeta/core/client.py`). It writes to the store; a
`Runtime` somewhere runs the work.

| Method | Does |
|---|---|
| `await create(agent, input=None, *, task_id=None, config=None, queue="default", dedupe_key=None, history_from=None, history_through=None) -> str` | create a task, optionally with its first message; returns its id. A repeated `dedupe_key`, or an existing `task_id`, returns the existing task |
| `await send(task_id, input, *, when="next_turn", dedupe_key=None) -> int` | send a message (`"now"`: at the next step of the running turn); returns its inbox seq |
| `await answer(task_id, request_id, decision=None, *, always=False, data=None) -> int` | answer an approval (`"allow"` / `"deny"`) or a question (`data`); idempotent per request id |
| `await signal(task_id, name, data=None, *, dedupe_key=None, wake=True) -> int` | post a named signal a plugin reads (`permission_mode`, `config`, …) |
| `await cancel(task_id, reason="", *, turn_only=False)` | end the task, or stop only its current turn |
| `await get(task_id) -> TaskRow` | the task's row; raises `UnknownTask` |
| `await tasks(*, parent_id=None, status=None, limit=100) -> list[TaskRow]` | rows of the task table |
| `await events(task_id, after=0) -> list[Recorded]` | the log after seq `after` |
| `watch(task_id, after=0) -> AsyncIterator[Recorded]` | the log from `after` on, live, until the task ends |
| `await wait(task_id, *, timeout=None) -> TaskRow` | until the task is waiting or ended |

`input` is a string or a `noeta.core.types.Message` (a user turn of `Text` and `Image` blocks);
an image is shrunk to fit before it is stored. A `history_from` that is no task raises
`UnknownTask`.

### `noeta.RemoteClient(url, *, token=None, transport=None)`

The same methods as `Client`, over a `noeta serve` server's HTTP API
(`noeta/host/remote.py`); `watch` reconnects by itself. It also has `await context(task_id)` (what fills the task's next
request) and `await close()`, which closes its HTTP client.
`transport` is an `httpx` transport (tests).

### `noeta.connect(url, *, token=None, **kw) -> Client | RemoteClient`

`sqlite:///…` or `postgres://…` gives a `Client` on that store; `http://…` / `https://…` a
`RemoteClient` for that server with `token`. Extra keywords go to the client's constructor.

## Records and state

| Name | Is |
|---|---|
| `noeta.Recorded` | an event as stored: `seq`, `at` (a Unix time), `event` (one of the ten event types in `noeta.core.events`; see [Events](./events.md)) |
| `noeta.TaskState` | a task's state folded from its log: config, messages, calls, waits, children, timers, usage, and each plugin's state in `ext` (read only for plugins) |
| `noeta.ToolUse` | a tool call the model made: `id`, `name`, `input` |

## Writing plugins and tools

These are the plugin author's names; each is described fully in
[Extension interface](./extension.md) and [Write a plugin](../guides/plugins.md).

| Name | Signature | Is |
|---|---|---|
| `noeta.Extension` | subclass it; set `name` | the one plugin interface: tools, context, reminders, commands, gates on tool calls, state folded from the log; every method optional |
| `noeta.TaskView` | passed to the hooks | what a plugin sees of a task: its folded state, a way to record entries, its exec env |
| `noeta.ToolChanges` | `(add=(), withhold={}, entries=())` | what `Extension.tool_changes` changes in a task's tool list |
| `noeta.ContextPart` | `(text, order=100, before="")` | system prompt text a plugin adds |
| `noeta.Reminder` | `(text, kind="reminder", data=None, origin="system", role="user", order=100)` | text for the model that is not the user speaking |
| `noeta.Command`, `noeta.CommandContext` | `(name, description, template="", argument_hint="", local=False)`; `(cwd, task_id=None)` | a slash command front ends offer, and where it is listed or run |
| `noeta.Allow`, `noeta.Ask`, `noeta.Deny`, `noeta.Rewrite` | `Allow(approve=False, outright=False)`, `Ask(prompt="", detail={}, firm=False)`, `Deny(reason="")`, `Rewrite(input, name=None)` | what `before_tool` returns: deny wins over rewrite, ask and allow |
| `noeta.Tool` | a protocol: `name`, `description`, `input_schema`, `read_only`, `clearable`, `async run(input, ctx)` | a tool the model can call; optional `flags`, `resume`, `recover`, `rule_inputs` |
| `noeta.ToolContext` | passed to `run` | the call's task, exec env, plugin states, `record`, `spawn`, `post`, `keep`, `progress`, … |
| `noeta.ToolOutput` | `(content=[], is_error=False, meta={}, display=None, park=None, end_turn=None, block_list=False)` | a tool's result |
| `noeta.text_output` | `(text, *, is_error=False, **meta) -> ToolOutput` | a text result |
| `noeta.function_tool` | `@function_tool` or `@function_tool(name=None, description=None, read_only=False, clearable=False, deferred=False)` | a typed function as a tool: name, docstring, schema from the annotations |
| `noeta.ask` | `ask(*questions) -> Park` | park a tool call on questions to the person (`{"question", "header"?, "options"?, "multiSelect"?}`) |
| `noeta.read_answer` | `read_answer(outcome, asked) -> dict \| None` | the checked answers a parked question got, or `None` when it was withdrawn |

## Useful modules outside `noeta`

| Module | For |
|---|---|
| `noeta.core.events` | the event types (`TaskCreated`, `ModelReplied`, `ToolStarted`, `Waiting`, `TaskEnded`, …) and inbox items |
| `noeta.core.types` | `Message`, `Text`, `Image`, `ToolUse`, `Delta`, `Usage` |
| `noeta.core.models` | the model catalog: `find_spec`, `register_models`, `ModelSpec`, `ALIASES` |
| `noeta.host.pending` | `pending_tree`, `choices`, `answer_choice`: what a task tree waits on a person for |
| `noeta.host.questions` | `answer_question`: answer a question after checking it |
| `noeta.host.http` | `create_app`: the HTTP API as a Starlette app in your own server |
| `noeta.testing` | `ScriptedProvider`, `reply`: a scripted model for tests |
