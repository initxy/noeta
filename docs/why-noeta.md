# Why noeta

This page says what noeta is for, who it is for, what it deliberately is not, and how it
relates to Claude Code.

## What it is

noeta is an open-source coding agent that behaves, from the model's side, like Claude Code,
and is built, from the operator's side, like a server program. It is one Python package
(`noeta`) with a terminal UI (`noeta-tui`) on top of its API, plus an experimental web app and
desktop shell built from source. What sets it apart is the pair: an agent that keeps Claude
Code's contract with the model, and that you self-host on as many workers as you like, all
sharing one Postgres store, any of them continuing any task.

Two decisions shape everything else:

1. **What the model sees follows Claude Code.** Models carry their strongest habits on the
   tools and prompts of the most-used coding agent. noeta sends the same tool names and
   schemas, keeps the rules and order of Claude Code's prompt parts (in its own words), sends
   the same reminders and notices, compacts the way Claude Code does, and shapes the provider
   request the same way. A difference is a bug unless it is listed with its reason, and
   `make parity` checks it by replaying recorded Claude Code sessions against noeta
   ([Matching Claude Code](./how-it-works/parity.md), [ADR-003](https://github.com/initxy/noeta/blob/main/docs/adr/003-claude-code-is-the-model-visible-contract.md),
   [`parity/deviations.md`](https://github.com/initxy/noeta/blob/main/parity/deviations.md)).
2. **Underneath, a task is an append-only log with one writer.** Nothing about a running task
   lives only in a process. A worker claims a task, folds its log into its state, takes one
   step and appends what happened. Any worker can continue any task, a crash never repeats a
   side effect blindly, and a task that waits for a person holds nothing
   ([ADR-001](https://github.com/initxy/noeta/blob/main/docs/adr/001-task-is-an-event-log-with-one-writer.md),
   [A task is a log](./how-it-works/event-log.md)).

## What it is for

- **Many machines.** Point any number of `noeta worker` processes at one Postgres store and put
  `noeta serve` in front; front ends attach to any server
  ([Run it as a server](./guides/server.md), [Deploy with Docker](./guides/docker.md),
  [Many machines](./how-it-works/many-machines.md)).
- **Coding agents that run unattended.** A task can run for hours; when its worker is killed or
  redeployed, another worker picks it up where its log stops.
- **Agents that wait for people.** Approvals and questions park the task, holding no process.
  Answer from the terminal, an editor, the HTTP API or your own service, minutes or days later.
- **Embedding.** The same kernel is a Python library: create tasks, watch their events and
  answer their questions from your own program ([Embed it in Python](./start/embedding.md)).
- **Any model.** Anthropic's API, any OpenAI-compatible endpoint (vLLM, gateways, DeepSeek),
  or OpenAI's Responses API, several at once with each model routed to its provider
  ([Models and providers](./guides/models.md)).
- **Programs written for Claude Code.** `noeta -p` speaks Claude Code's headless flags and
  its stream-json protocol, so a program built on Claude Code's Agent SDK can drive noeta
  unchanged ([The terminal UI and -p](./guides/terminal.md)).

## Who it is for

- Developers who want Claude Code's working style with a model of their choice, in a
  terminal, an editor or a browser.
- Teams that run coding agents on servers: CI jobs, review bots, long refactors, agents that
  answer in a chat.
- People who build products on an agent and need its state durable, auditable (the log is the
  full record) and spread over machines.
- Plugin authors: every capability is a plugin behind one interface, and a plugin can be a
  single Python file ([Write a plugin](./guides/plugins.md)).

## What it is not

- **Not Claude Code's code.** noeta is a separate implementation. Its prompts are written in
  its own words; only the rules, their order and the model-visible shapes follow Claude Code.
- **Not a reader of Claude Code's files.** noeta reads only `~/.noeta/`, a project's `.noeta/`,
  `NOETA.md` / `AGENTS.md` and the machine's managed directory. The formats inside are Claude
  Code's, so moving over is a copy, but `~/.claude/`, `.claude/`, `CLAUDE.md` and `.mcp.json`
  are never read, so the two tools never run each other's hooks or servers
  ([ADR-004](https://github.com/initxy/noeta/blob/main/docs/adr/004-noeta-reads-only-its-own-config-places.md)).
- **Not multi-tenant.** A store and a server serve one party: whoever holds the server's token
  drives every task, and tools run as the worker's OS user. Separate parties get separate
  servers and stores, or separation you build on the extension points
  ([ADR-028](https://github.com/initxy/noeta/blob/main/docs/adr/028-no-tenants-in-noeta.md)).
- **Not a workflow engine.** There is no graph to define. The model decides each step; a fixed
  procedure is an agent plus sub-agents (or the `Workflow` tool, as in Claude Code).
- **Not a hosted service.** It runs in your process, on your machines, against your store.
  There is no noeta cloud and nothing phones home.
- **Not everything Claude Code has.** Claude Code plugin monitors, `bin/` folders, LSP servers,
  themes and output styles are not loaded; some hook events belong to Claude Code features
  noeta lacks and are skipped with a warning. The full list is in
  [Compared with other agents](./operations/comparison.md) and
  [Known limitations](./operations/limitations.md).
- **Young.** 1.0 is the first release of this codebase. The 0.x releases were the separate
  `noeta-runtime` / `noeta-sdk` packages and describe a different codebase.

## How it relates to Claude Code

| | Claude Code | noeta |
|---|---|---|
| What the model sees | the reference | follows it; differences listed in `parity/deviations.md` and checked by `make parity` |
| Settings, hooks, skills, agents, MCP, plugin folders | `~/.claude`, `.claude/`, `CLAUDE.md`, `.mcp.json` | the same formats in `~/.noeta`, `.noeta/`, `NOETA.md` / `AGENTS.md`, `.noeta/mcp.json` |
| Environment knobs | `CLAUDE_*`, `MAX_MCP_OUTPUT_TOKENS`, … | the same knobs under the `NOETA_` prefix |
| Headless use | `claude -p`, stream-json, Agent SDK | `noeta -p` with the same flags and protocol |
| A conversation | one local session | a task: a log in a store, continued by any worker |
| A crash mid-tool | the session ends | the next worker continues; the unfinished call is answered "outcome unknown" unless the tool can look its outcome up |
| Waiting for approval | a process waits | the task parks and holds no worker |
| Models | Anthropic's | Anthropic's, OpenAI-compatible, OpenAI Responses, several at once |
| Extending it | plugins, hooks, MCP | the same, plus Python plugins behind one `Extension` interface |
| Telemetry | yes, can be turned off | none |

How the matching is kept honest — recordings of real Claude Code requests, an offline replay,
and a list of accepted differences — is on [Matching Claude Code](./how-it-works/parity.md).
How noeta compares with pi, DeepSeek Harness and other agents, point by point, is on
[Compared with other agents](./operations/comparison.md).
