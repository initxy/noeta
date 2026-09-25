---
layout: home
title: "Noeta — Python SDK for durable, crash-safe AI agents"
titleTemplate: false

hero:
  name: Noeta
  text: Agents that keep running.
  tagline: A Python SDK for agents that survive crashes, wait days for a human, and scale from one script to a cluster — without changing the agent.
  image:
    src: /logo.svg
    alt: Noeta
  actions:
    - theme: brand
      text: Quickstart
      link: /start/quickstart
    - theme: alt
      text: Why Noeta
      link: /why-noeta
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: Survives crashes
    details: State is rebuilt from an append-only event log, never held in memory. Kill a worker mid-task and another one carries on from the last step. The same log is a complete audit trail.
    link: /how-it-works/event-log
    linkText: How recovery works
  - title: Waiting costs nothing
    details: A task can pause for a human approval, a timer, a subtask or an outside event — for seconds or for months. It uses no resources while it sleeps and is woken exactly once.
    link: /how-it-works/tasks-and-waking
    linkText: How waking works
  - title: From a script to a cluster
    details: Call query() in a script, run a worker pool inside your service, then point several hosts at one Postgres. The agent code is the same at every step. No daemon, no extra service.
    link: /guides/deploy
    linkText: Deploy it
---

## A working agent in a few lines

<p class="nt-lead">Install <code>noeta-sdk</code>, set <code>ANTHROPIC_API_KEY</code>, and the agent explores your project with its built-in file tools.</p>

```python
from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider

result = query(
    Options(system_prompt="You are a concise coding assistant."),
    goal="What files are in this directory, and what does each one do?",
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
)
print(result.answer())
```

The result is more than the answer: it holds every model call, tool call and
token count from the run. [Quickstart →](/start/quickstart)

## How it fits together

<NtArchitecture />

Every step the agent takes is appended to the event log, so any worker can rebuild the task from it. Everything the agent can do — tools, MCP, memory, the model itself — is a plugin. [How it works →](/how-it-works/)

## Built to be extended

<div class="nt-cards">
  <div class="nt-card"><strong>Everything is a plugin</strong><span>The kernel ships no capabilities. File tools, web, memory, MCP, sandboxes and model adapters are all plugins that use the same public API yours does.</span></div>
  <div class="nt-card"><strong>Any model</strong><span>Anthropic, any OpenAI-compatible gateway, or the OpenAI Responses API. Switching is one line; the agent and its history stay the same.</span></div>
  <div class="nt-card"><strong>Approval before action</strong><span>Risky tool calls pause for a human. Guards can block a call before it runs; observers watch without being able to interfere.</span></div>
</div>

## How it compares

<div class="nt-compare">

| | **Noeta** | Claude Agent SDK | LangGraph | Temporal |
|---|---|---|---|---|
| What it is | Durable agent runtime, as a library | Agent loop library for Claude | Graph-based agent framework | Durable workflow platform |
| Control flow | The model decides each step | The model decides | A graph you define | Workflow code you write |
| What is saved | Every event; state is derived from it | The conversation | Checkpoints of graph state | Workflow history |
| Waiting for a human / timer | Built in, woken exactly once | Resume the conversation | Interrupt, then the caller resumes | Built in |
| Scaling out | Worker pool; many hosts on Postgres | One process | Up to you, or the hosted platform | A Temporal cluster |
| Models | Any, one line to switch | Claude | Any | — |
| Extra service to run | None — your process, your database | None | None | Temporal server |

</div>

<p class="nt-muted">Pick Noeta when an agent runs unattended for a long time and you need to recover, audit and scale it. <a href="why-noeta.html">Full comparison →</a></p>

## Proven on public benchmarks

<div class="nt-stats">
  <div class="nt-stat"><div class="num">82.5%</div><div class="label">Terminal-Bench 2.1</div><div class="sub">40-task sample · public board 58.7%–83.8%</div></div>
  <div class="nt-stat"><div class="num">86.7%</div><div class="label">SWE-bench Verified</div><div class="sub">15-instance subset · field top ~79%</div></div>
</div>

<p class="nt-muted">An agent built only on the public SDK (<a href="https://github.com/initxy/noeta-agent">noeta-agent</a> <code>main</code>, Claude Opus 4.8), run on the official harness. Both are samples, not full leaderboard runs. <a href="benchmarks.html">Method and caveats →</a></p>

## Where to go next

<div class="nt-cards">
  <a class="nt-card" href="start/quickstart.html"><strong>Quickstart</strong><span>A real agent running in five minutes.</span></a>
  <a class="nt-card" href="start/tutorial.html"><strong>Tutorial</strong><span>Custom tools, approvals, multi-turn conversation and durable storage.</span></a>
  <a class="nt-card" href="how-it-works/"><strong>How it works</strong><span>The event log, tasks and waking, and the plugin system, one page each.</span></a>
</div>
