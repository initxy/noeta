# Context, caching and compaction

This page explains what noeta sends the model on every call, in which order, why those bytes
come out the same after a restart or on another machine, and how a long task stays inside the
model's context window: long outputs saved to files, old tool results cleared, and the
conversation summarized.

Everything here rests on one rule from [the event log](./event-log.md): the next model request
is a pure function of the task's folded state. Whatever could differ between two runs (a file
on disk, the date, a plugin's setting, a summary) is recorded in the log the first time it is
used, and read back from there afterwards.

## What a request is made of

`build_request` (`noeta/core/loop/request.py`) turns the folded `TaskState` into a
`ModelRequest`; `build_model_request` and `call_model` (`noeta/core/runner/model.py`) feed it
the agent, the plugins' parts and the tool list, record the step's reminders, run every
`before_model`, and send it.

<Diagram name="ctx-request-layout" caption="One request, top to bottom, as the Anthropic adapter sends it" />

| Part | What goes in it | Where it comes from |
|---|---|---|
| Tools | Every tool the task offers, sorted by name; tools flagged `listed_last` (a changing set, such as server tools) after them, also by name. A deferred tool goes with `defer_loading`; a withheld one is left out. Entries that are no tool (`ToolEntry`, such as `DeferredToolPlaceholder`) go where they say. | `offer_tools` (`noeta/core/extension.py`): each plugin's `tools(view)`, the agent's allow and deny lists, then each plugin's `tool_changes`. `request_tools` sorts them. A tool's description is the prompt `tool/<Tool>`. |
| System, block 1 | The identity line. | The prompt `identity/<agent>`: the agent file's `identity`, else `You are noeta, an open-source coding agent.` (`noeta/agents/agent_files.py`). |
| System, block 2 | The agent's prompt, then each plugin's `ContextPart`s in `order`. A part with `before` goes ahead of the prompt line equal to it (a section placed before a heading). | The prompt `system/<agent>` (the agent file's body) and `Extension.context(view)`; the memory section and the task-list line are two built-in parts. |
| Messages | The conversation as the fold holds it: the opening context recorded at the task's start, the person's messages, the model's replies, tool results, notes, and this step's reminders. | `TaskState.messages` (`noeta/core/loop/state.py`). |

The opening context is not in the system prompt. The `instructions` plugin reads the
instruction files (`NOETA.md`, `AGENTS.md`, `.noeta/rules`, the memory index) in `on_start` and
records them as one message ahead of the first prompt; the `environment` plugin records the git
snapshot and the commit-attribution note the same way, and the environment block (working
directory, platform, model, date) as the first request's system turn. Keeping them in the
conversation keeps the system prompt identical for every task of an agent, and lets a
compaction put them back next to its summary (ahead of it for an automatic one).

Two plugins change the system text in `before_model`: `environment` keeps the prompt
paragraphs written for the model in use, drops the `<total_tokens>` line for a model that is
told no budget (`noeta/plugins/environment/notes.py`), and drops the `! <command>` line unless the
task's config says its front end runs `!` commands (`bash_mode`, set by the terminal UI); `skills`
drops the prompt's skill line when the task has no skills or no `Skill` tool. Both lines sit
under `# Session-specific guidance`, which goes once neither is left (`drop_guidance_line`,
`noeta/toolkit/guidance_lines.py`). `environment` also sends a turn the person asked to
`ultrathink` at `xhigh` effort.

### Prompt names and overrides

Every model-facing text has a name (`noeta/core/prompts.py`): `system/<agent>`,
`identity/<agent>`, `tool/<Tool>`, `<plugin>/<key>` (`compaction/summarize`, `memory/section`)
and `reminder/<key>`. The code that owns a text holds its default. An override replaces it, and
`<name>.append` adds to it after a blank line. Overrides come from, lowest first:

1. the host's prompt directories (`~/.noeta/prompts` for the `noeta` command; a file
   `tool/Read.md` overrides `tool/Read`, `tool/Read.append.md` appends to it);
2. the task's workspace, `<cwd>/.noeta/prompts`, only when the workspace is trusted;
3. the task config's `prompts` mapping (`config.toml` `prompts`, or an SDK host's config).

The files are read once, when the task first starts (at most 256 files of 256 KB each), and
recorded in its log as the core `prompts` record. A resumed task, on any worker, uses the
recorded texts, so an edit to a prompt file changes only the tasks started after it.

### Reminders

A reminder is text for the model that is not the person speaking: the environment, the list of
deferred tools, the date, a budget, a file that changed, a memory recalled. A plugin returns
`Reminder`s from `reminders(view)`; the runner calls every plugin's before each model call,
records each reminder as an `ExtensionRecorded` entry carrying its message, and puts the
recorded messages into this very request after the conversation (`_remind` in
`noeta/core/runner/model.py`):

- the `role="user"` ones first: they ride in the person's turn as host text (`origin` `system`,
  or `memory` for a recalled memory);
- then the `role="system"` ones, sorted by `order`: each a system turn after it. The
  environment plugin uses `order` to place its notes (environment 0, deferred tools 30,
  language 75, budget 80, date 90).

Because a reminder is recorded, it is part of the conversation from then on: the next request
carries it again at the same place, and a task resumed on another worker sends the same bytes.
Text that is captured once, when the task starts, is recorded from `on_start` instead; input
that must wake a waiting task goes through the [inbox](./waiting.md), never a reminder.

The provider adapters render host text so that it cannot pass for the person. Anthropic gets
`<system-reminder>` blocks inside the user turn and mid-conversation `system` turns (adjacent
ones joined, as Claude Code sends its environment and budget notes). The OpenAI shapes get a
system message that opens with a line saying the text is automated context from the host and
not the user (`HOST_INJECTED_PREAMBLE`, `noeta/backends/providers/_common.py`). Text the person
or a tool wrote cannot forge the tag: reserved tags inside it are neutralized.

### Deferred tools

A tool whose `ToolFlags` say `deferred=True` is named to the model but sent only once the model
loads it with `ToolSearch` (Claude Code's deferred tool loading, [ADR-017](https://github.com/initxy/noeta/blob/main/docs/adr/017-deferred-tool-loading-is-a-plugin.md)).
MCP tools are deferred by default, so twenty servers do not put their schemas in every request.

- The `tool_search` plugin (`noeta/plugins/tool_search`) adds `ToolSearch` through
  `tool_changes` and withholds every deferred tool not loaded yet. A call to a withheld tool is
  answered with an error telling the model to load it first, before any permission check.
- A tool counts as loaded once a tool result named it (a `ToolReference` block). The plugin
  records the names with that result, so a compaction does not unload a tool and a continued
  task keeps its source's.
- The request always lists `DeferredToolPlaceholder`, an entry that is never called, second to
  last, so the vendor's deferred-loading mode is on from the first request and the tool list's
  shape does not change when the first tool loads.
- The environment plugin names the deferred tools in the first request's system turn and tells
  the model when the set changes (an MCP server connected or went away).

Loading is a plugin, not kernel code, because `tool_changes` hands every plugin the same list
of offered tools: no plugin's answer depends on the order of the others, and the kernel applies
the withheld set before it builds the request, so the request stays a function of the log.

## Prompt caching

A provider's prompt cache bills a request's prefix at a fraction of the price when the bytes
match a recent request exactly, up to a cache mark. noeta's requests are built to hit it on
every step.

<Diagram name="ctx-cache-breakpoints" caption="Step N+1 reads step N's prefix from cache and writes only what is new" />

**Where the marks sit.** `build_request` marks both system blocks (`SystemPart(cache=True)`) and
the newest message (`cache_breakpoints`: one mark, on the last message, as Claude Code places
it). The Anthropic adapter (`apply_cache_control`, `noeta/backends/providers/_anthropic_wire.py`)
stamps them as `cache_control` on the wire, newest first, up to the API's four. The tools get no
mark of their own: Anthropic renders tools, then system, then messages, and the first system
block's mark covers the tools before it. So a main request carries three marks.

**Why on the newest message.** The next step's request is this one plus the model's reply and
the tool results: everything up to this step's last mark is read from cache, and only the new
reply and results are written. The reminders a step adds are recorded before the request goes,
and the mark is placed again after them, so the mark is on the message the next request also
has.

Other requests place their own marks: the summarize call marks the message before its
instruction so it reads the conversation from the main request's cache, and the auto-mode
permission classifier marks its own blocks.

**The cache's lifetime.** Marks ask for the API's default five-minute cache. An Anthropic
provider set with `prompt_cache_ttl = "1h"` (`config.toml`, on the default provider or a
`[providers.<name>]` table) asks for an hour (`{"type": "ephemeral", "ttl": "1h"}`), which costs
more per write and pays off for tasks that pause longer than five minutes. The reply says how
long its cache lives (`ModelResponse.cache_ttl`), folded into `TaskState.cache_ttl`, which is
how the compaction plugin knows when an idle task's cache will expire.

The OpenAI wires take no marks; those servers cache prefixes on their own, and the same stable
prefix serves them.

### Why the bytes stay the same {#why-the-bytes-stay-the-same}

A cache hit needs the prefix byte for byte, after a restart and on another worker too. Each
source of variation is pinned:

| What could differ | How it is held still |
|---|---|
| The order of tools | Sorted by name; a tool set that changes goes `listed_last`, at the end of the prefix. |
| Prompt override files | Read once at the task's start, recorded (`prompts`). |
| Instruction files, git status, the date, the environment block | Captured in `on_start` or the first request, recorded as messages or plugin records. A changed instruction file is shown again as a new message, never by editing the old one. |
| The plugins and their arguments | Pinned per task (`records.Plugins`); a worker without one does not run the task ([plugin pin](./plugins.md#the-plugin-pin)). |
| A model's provider and limits | Recorded in the task's config when the model is chosen (`model_route`, see [models](./models.md#routing-with-several-providers)). |
| System prompt parts | A `ContextPart` must be stable; text that changes is recorded as a message instead. |
| When to compact or clear | Decided from recorded usage and a deterministic estimate, not a tokenizer (`noeta/plugins/compaction/estimate.py`: a CJK character counts as one token, other text as four characters a token), so two hosts decide alike. |
| Summaries, cleared results, files read again | Recorded (`Compacted`, the compaction plugin's records), never recomputed. |

Each `ModelCalled` event records `request_hash`, a hash of the whole request, so two runs of the
same step can be compared from their logs. Some changes break the cache on purpose: a model
switch, a compaction, and a clearing of old results each start a new prefix.

## Long tool outputs {#long-tool-outputs}

A tool's text result that is too long is saved whole to a file the model can `Read`, and the
model gets a preview (Claude Code's rule, `noeta/core/large_output.py`).

<Diagram name="ctx-large-output" caption="A result over its limit becomes a file in the task's exec env and a 2 KB preview" />

- **Per tool.** A result longer than its tool's `ToolFlags.max_result_chars` (default and
  ceiling 50,000 characters, `RESULT_CEILING_CHARS`) is saved. `Bash` caps its own output at
  30,000 first; `Read` sets `None` and bounds its own output.
- **Per reply.** The text results of one reply's calls together get 200,000 characters
  (`REPLY_RESULTS_BUDGET_CHARS`). Once that is spent, every later result over 2,000 characters
  is saved too. Calls running together reserve their share before they wait, so they cannot
  all pass the budget at once.
- **What the model sees.** A `<persisted-output>` block: the size, the path, and the first
  2,000 characters, cut at a newline.
- **Where the file goes.** Through the task's exec env, under the spill root:
  `<spill root>/<task id>/<call id>.result.txt` (`result_spill_path`). The path is a function of
  the task and the call, so a call run again names the same file. Because the file is written
  through the exec env, the model reads it with its own file tools on whichever worker runs the
  task next. The `noeta` command's spill root is `~/.noeta/tool-results`; a host's sweep removes
  an ended task's directory 30 days after its last write (`noeta/host/spill_sweep.py`).
- **When it cannot save.** With no spill root (`Runtime(spill_root=None)`) a result over the
  limit keeps its start and its end around a note (`<truncated-output>`). When the write fails,
  the result stays whole, as Claude Code keeps it.
- Errors, results with images and parked calls pass unchanged.

A sub-agent's final report is bound the same way at 100,000 characters
(`RESULT_LIMIT_CHARS`, `noeta/plugins/subtasks/tool.py`).

## Staying inside the window {#staying-inside-the-window}

A long task meets its model's window. noeta has Claude Code's set of answers, cheapest first,
in the kernel and the `compaction` plugin (`noeta/plugins/compaction/`):

| Mechanism | When | Default |
|---|---|---|
| Retry with a smaller output cap | The provider says the input fits but not with `max_tokens` | always |
| Clear old tool results | The prompt cache has gone cold after an idle gap | off |
| Write the summary ahead | The context passes about 80% of the effective window | on |
| Compact | The context reaches the threshold | on |
| Compact after an overflow | The provider refused a request as too long | on |
| Compact while idle | One-hour cache, large context, idle before the cache expires | off |
| `/compact [focus]` | The person asks | always |

### Measuring and the thresholds

Before every model call (mid-turn too) the compaction plugin measures the context as the newest
reply's reported usage (input plus output) plus an estimate of what was added after it. Before
any reply reports usage, and right after a compaction, the whole request is estimated. The
first request after a compaction always goes as it is.

<Diagram name="ctx-window" caption="The lines a model's window is cut into (noeta/plugins/compaction/window.py)" />

The numbers derive from the model catalog, as Claude Code derives them. A summary needs room
to be written, so the effective window holds back `min(output cap, 20,000)` tokens. The
threshold sits 13,000 tokens under that, or at `auto_compact_percent` of the effective window
when that is lower. A summary starts being written in the background at 80% of the effective
window. `auto_compact_window` caps the window the numbers start from. The two knobs read
`NOETA_AUTO_COMPACT_WINDOW` and `NOETA_AUTOCOMPACT_PCT_OVERRIDE` when the plugin is given
neither (Claude Code's knobs, under noeta's prefix). A model the catalog does not know gets a
conservative 128,000-token window and 16,384-token output cap, so it still compacts.

The plugin records the threshold whenever it changes (a `threshold` record), which is what front
ends read for "N% until auto-compact".

### What a compaction does

<Diagram name="ctx-compaction" caption="The conversation before and after an automatic compaction" />

1. **Asks the other plugins.** Every plugin's `before_compact(view, trigger, instructions)` may
   add instructions for the summary or return `Deny` to skip this compaction (the person is told
   why). The hooks plugin runs Claude Code's `PreCompact` hooks here.
2. **Chooses what to summarize.** Everything before the newest reply; that reply and what
   followed it stay verbatim. When the newest reply answered the first request after the
   previous compaction, everything is summarized. With nothing worth summarizing before the
   newest reply, an automatic compaction waits.
3. **Writes the summary.** The summarize call is the main request with the summarize
   instruction (`compaction/summarize`) added to the last user message, so it reads the
   conversation from the main request's cache; `compaction_model` sends it to another model. A
   reply's `<analysis>` is dropped and its `<summary>` kept. When the call fails or brings no
   summary, a plainer fallback call follows (a summarizer system prompt, only the `Read` tool,
   plus `ToolSearch` and the MCP tools when tool search is on, images replaced, no cache marks). When the summarize call is itself too long, its oldest
   rounds are dropped, up to three times, and the summary says so. Every call's cost is
   recorded as a core `spent` record.
4. **Frames the summary.** The summary message says it stands for an earlier part of the
   conversation, lists the tools used before it, names a file with the whole transcript (on a
   local exec env: `~/.noeta/projects/<workspace>/<task id>.jsonl`, refolded from the log),
   tells the model to carry on without recapping, and says when newer messages follow it.
5. **Rebuilds what follows.** Plugins give back what must survive (`lead_resident`: the
   instruction files read again, the memory index, a fresh git snapshot and the
   commit-attribution note; `resident`: invoked skills, MCP server instructions, the plan,
   running background jobs and sub-agents). The environment block, the deferred-tool list and
   the agent list go out again with the next request. Up to five files the conversation
   touched last are read again through `Read` (5,000 tokens each, 50,000 in all) and given in
   the next request's system turn.
6. **Records it.** All of this goes into one core `Compacted` event: the `summary`, the seq
   it covers `through`, and the messages that go ahead of it (`lead`), right after it (`kept`)
   and after the verbatim tail (`after`), plus the plugin's own `compacted` record. The fold
   replaces the history up to `through` with them (`_compact` in `noeta/core/loop/state.py`),
   and `before_model` raises `RequestStale`, so every plugin builds the request again from the
   compacted state.

A manual `/compact [instructions]` does the same with the person's focus added, puts nothing
ahead of the summary, and ends the turn without another model call (`TurnDone`).

**When it goes wrong.** An automatic compaction whose summary fails lets the request go out
uncompacted (an overflow then compacts after it); three failures in a row turn automatic
compaction off for the task, while `/compact` and compaction after an overflow still run. If the
context fills up again within three model calls of the previous compaction in the same turn,
three times in a row, the turn stops with `autocompact_thrashing`: a single file or output is too big for the
window, and the person is told to read it in parts. A compaction restarted by a crash builds the
same summary request again from the log, since nothing of it was recorded yet.

### Writing the summary ahead {#writing-the-summary-ahead}

From about 80% of the effective window the summary of the older rounds is written in the
background (`auto_compact_precompute`, on by default), so that at the threshold it can be swapped
in with no summarize call in the step. The difficulty is that a task has one writer and every
request is a function of the log, so a result computed beside the step cannot be written by
whoever computed it ([ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md)):

<Diagram name="ctx-precompute" caption="The background summary comes back through the inbox" />

- The step that passes 80% records `precompute_started` with a random job id and starts the call
  with that step's request. The call lives only in the plugin's memory and decides nothing.
- When it finishes, its outcome and cost are posted to the task's own inbox as an `External`
  signal (`compaction.precomputed`). The runner takes it at its next boundary, like any input,
  on whichever worker holds the task then; the plugin's `on_event` records the summary only when
  its job id is the one the log says is running. Any other (outdated, or posted by someone else)
  leaves only its cost.
- The recorded summary names what it covers: the compaction count and a digest of the messages.
  At the threshold (or after an overflow, or when idle) it is used only while both still match;
  otherwise the step summarizes as usual. A summary still being written at the threshold is not
  waited for.
- A worker that dies loses the call in flight; a later step starts another. After three failed
  ones no more are written for the task.

### Clearing old tool results

After an idle gap longer than the prompt cache lives, the next request misses the cache anyway,
so it is a cheap moment to make it smaller. With `auto_compact_clear_results` on, once more than
`auto_compact_clear_idle_seconds` (default 3,900) have passed since the previous model call, the
results of older calls to tools whose `clearable` attribute is true are replaced by
`[Old tool result content cleared]` (`noeta/plugins/compaction/clearing.py`). The newest 12 are
kept, results already saved to a file are kept, and nothing is cleared unless it saves at least
20,000 tokens. The cleared call ids are recorded and every later request is rewritten from that
record. A summary still reads the cleared results as they were.

### Compacting while idle

With the one-hour cache and `auto_compact_idle` on, a turn that ends with at least 200,000 tokens
of context sets a kernel timer at 90% of the cache's life. If the task is still idle when it
fires (not late by more than 60 seconds, no newer request), it is compacted from
`TaskView.next_request` before the cache expires, and the person is told. Sub-agents are never
compacted this way.

### When the provider says the request is too long

Each provider classifies its errors into neutral ones (`noeta/backends/providers/_common.py`): a
413, or a 4xx whose code is `context_length_exceeded` or whose message says the prompt is too
long (a rate-limit or output-limit message never counts), becomes `ContextOverflow`. When the
message states the input length and the window (Anthropic's `input length and max_tokens exceed
context limit: 188240 + 21333 > 200000`, OpenAI's and vLLM's `maximum context length is …`
wordings), the retry helper (`noeta/core/retry.py`) makes the call once more with
`max_tokens = window − input − 1,000`, when that is at least 3,000 and below the cap the
call asked for: the input fits, only not with
the output asked for. Otherwise every plugin's `on_context_overflow(view, request)` is asked;
the compaction plugin compacts (with the summary written ahead when it fits, else summarizing
everything) and the call is made again. When nobody makes room the turn fails with
`context_overflow`.

## The log keeps everything, the fold keeps what the next step needs

<Diagram name="ctx-log-fold" caption="ADR-030: the log is the only full transcript" />

Compaction changes what the fold holds, never the log. The event log is the only full
transcript; `TaskState` keeps the conversation the model sees (bounded by compaction), a few
kernel facts (`prompt_seq`, the newest person message's seq; usage; timers), and each plugin's
own state ([ADR-030](https://github.com/initxy/noeta/blob/main/docs/adr/030-the-log-is-the-only-full-transcript.md)).
A plugin that needs a fact from before a compaction records it when it happens (`tool_search`'s
loaded tools, the memory pages read), or reads the log where the need is rare (the transcript
file). A task continued from another carries its source's compacted conversation and plugin
states, nothing older. This keeps a [snapshot](./event-log.md) the size of what the next step
needs: a test task compacted 14 times snapshots at 5.9 KB instead of 255 KB.

## Budgets {#budgets}

Three different limits watch what a task spends:

- **Lifetime caps** (the `budget` plugin, `noeta/plugins/budget.py`): `max_iterations` (or
  `max_turns`, model calls), `max_tool_calls`, `max_cost_usd`, `max_spawned_subtasks` and
  `max_subtask_depth` (default 3). A tool call past a cap is denied by `before_tool` with the
  reason. The counts are the plugin's own records, folded from the log, so every worker enforces
  the same caps; only a cap that is set is counted.
- **The USD budget note** (`noeta/toolkit/usd_budget.py`): with the task config's
  `max_budget_usd` (`noeta -p --max-budget-usd`), every model call is told what the conversation,
  the root task and every task below it, has cost so far and what is left, read from the tasks'
  logs. The headless protocol ends the run with `error_max_budget_usd` past it.
- **The token budget note**: Claude models that Claude Code tells a budget get
  `<total_tokens>N tokens left</total_tokens>` in a system turn, the output tokens of the turn
  taken off 15,000,000 (`noeta/plugins/environment/notes.py`).

## Code

| Piece | File |
|---|---|
| Request building, cache marks, request hash | `noeta/core/loop/request.py` |
| The model step: reminders, `before_model`, overflow | `noeta/core/runner/model.py` |
| Prompt names and overrides | `noeta/core/prompts.py` |
| Tool offer, `ContextPart`, `Reminder`, `ToolChanges` | `noeta/core/extension.py` |
| Long outputs | `noeta/core/large_output.py` |
| Anthropic wire, cache marks, TTL | `noeta/backends/providers/_anthropic_wire.py`, `anthropic.py` |
| Overflow classification | `noeta/backends/providers/_common.py` |
| Retries, smaller output cap | `noeta/core/retry.py` |
| Compaction, clearing, write-ahead, idle | `noeta/plugins/compaction/` |
| Deferred tools | `noeta/plugins/tool_search/` |
| Environment, date, budget notes | `noeta/plugins/environment/` |
| Instruction files | `noeta/plugins/instructions/` |
| Lifetime caps | `noeta/plugins/budget.py` |

The plugin options are listed in [built-in plugins](../reference/plugins.md); the config keys in
[configuration keys](../reference/config.md).
