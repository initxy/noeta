# Noeta documentation

This directory holds the **user-facing documentation**. Architecture *decisions*
(why the code is the way it is) live one level down in
[`adr/`](adr/) and are a separate audience — read those before changing a
subsystem, not to learn how to use Noeta.

Start at the top and go as deep as you need.

## Start here

| Doc | Read it when |
| --- | --- |
| [`quickstart.md`](quickstart.md) | You just want it running — the offline stub smoke, then a real provider. |
| [`concepts.md`](concepts.md) | You want the core model: Task, EventLog, Dispatcher, Engine, Guard, Observer, Policy, Composer, and how a step flows. |
| [`noeta-agent.md`](noeta-agent.md) | You're using the bundled coding agent (`python -m noeta.agent`): its tools, presets, skills, write/shell permission model, HTTP surface, MCP / hooks. |

For runnable SDK snippets — minimal agent, custom tool, MCP server, permission
gating, provider swap, sub-agent — see [`../examples/`](../examples/).

## Going deeper

| Doc | Read it when |
| --- | --- |
| [`noeta-architecture-deep-dive.md`](noeta-architecture-deep-dive.md) | You want the top-down architecture walkthrough (event-sourced engine → 3-package layout), with Claude Agent SDK comparisons. |
| [`failure-modes.md`](failure-modes.md) | You need the honest failure story: missing API key, budget exhaustion, durable exactly-once wake recovery, the partial-step-orphan edge. |
| [`daemon.md`](daemon.md) | You're embedding the resident drain loop (`WorkerLoop`) yourself — its guarantees and limits. |

## Decisions & vocabulary (for changing the code)

| Where | What it is |
| --- | --- |
| [`adr/`](adr/) | **Architecture Decision Records** — one file per cross-module decision: what was decided, why, and why the alternatives were rejected (Chesterton's fence). Audience: anyone about to change the code. Start at [`adr/README.md`](adr/README.md). |
| [`../CONTEXT.md`](../CONTEXT.md) | The glossary — what each term *currently means* in this repository. |
| [`../AGENTS.md`](../AGENTS.md) | The contributor conventions router (communication, language, engineering constraints). |

The dividing line: `docs/` (this level) tells you **how to use** Noeta; `adr/`
tells you **why it is built the way it is**. Keep new user guidance here and new
decisions in `adr/`.
