---
layout: home
title: "noeta — the open-source, Claude Code-compatible coding agent"
titleTemplate: false

hero:
  name: noeta
  text: The Claude Code–compatible coding agent you can self-host on many workers.
  tagline: The model sees what Claude Code shows it. Underneath, every task is an append-only log with one writer in a Postgres (or SQLite) store, so any worker continues any task, a task waiting for a person holds no process, and a crash or a redeploy only moves the task to another worker.
  image:
    src: /logo.svg
    alt: noeta
  actions:
    - theme: brand
      text: Quickstart
      link: /start/quickstart
    - theme: alt
      text: How it works
      link: /how-it-works/
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: Many workers, one store
    details: Any number of workers on one Postgres, an HTTP API in front. No worker owns a task; each step can run on any of them. sqlite on a laptop runs the same code.
    link: /guides/server
    linkText: Run it as a server
  - title: Claude Code's contract
    details: The same tool names and schemas, prompt rules, reminders, compaction and request shape. It reads Claude Code's formats for settings, hooks, skills, agents, MCP servers and plugins, from noeta's own folders.
    link: /how-it-works/parity
    linkText: How it matches Claude Code
  - title: A task is a log
    details: Ten event types, one writer per task, fenced by an epoch. State is a fold of the log, so a crashed task continues on any worker and never re-runs a tool blindly.
    link: /how-it-works/event-log
    linkText: The event log
  - title: Waiting holds no process
    details: A question, an approval, a sub-agent or a timer parks the task. It holds no worker and no process until the answer lands in its inbox, minutes or weeks later.
    link: /how-it-works/waiting
    linkText: Waiting and waking
  - title: Everything is a plugin
    details: Files, shell, permissions, hooks, skills, sub-agents, MCP, memory and compaction all sit behind one Extension interface. The built-in plugins load exactly like yours.
    link: /how-it-works/plugins
    linkText: The plugin system
  - title: Every way in
    details: A terminal UI, Claude Code's -p and stream-json protocol, ACP for editors, an HTTP / WebSocket API, a Python SDK, a web app and a desktop app. All of them are one client API.
    link: /reference/cli
    linkText: The noeta command
---

## How it fits together

<Diagram name="home-overview" />

Every front end talks to one client API: create a task, send to it, answer it, cancel it,
read and watch its events. Workers claim tasks from the store, fold each task's log back
into its state, call the model, run tools through plugins and append what happened. What the
model sees is built by the plugins to match Claude Code. [How it works →](./how-it-works/index.md)

## What sets it apart

**Many workers, one store.** Point any number of `noeta worker` processes at one Postgres store
and put `noeta serve` in front. A worker claims a task for one step at a time and appends what
happened to the task's log, so the next step can run on any worker: a deploy, a crash or a lost
machine only moves the task. Front ends attach to any server with the same API
([Run it as a server](./guides/server.md), [Deploy with Docker](./guides/docker.md)).

**Claude Code's contract on a server.** Models are tuned against Claude Code's tools and
prompts, so noeta copies what the model sees, not how Claude Code is built: tool names and
schemas, the order and roles of prompt parts, reminders, compaction, the request a provider
gets. `make parity` replays recorded Claude Code sessions against noeta offline, compares
every request, and reports each difference not listed as accepted with its reason
([Matching Claude Code](./how-it-works/parity.md)). Your settings, hooks, skills,
agents, MCP servers and plugin folders keep their Claude Code format but live in `~/.noeta`
and `.noeta/`; noeta never reads `~/.claude` ([Configuration](./start/configuration.md)).

**An event log with one writer.** A task is a sequence of ten event types in the store. A
worker must claim a task before it writes, and every append names the sequence number it
expects and the claim's epoch, so a worker that froze and lost its claim can never write again.
A tool call is logged as started before it runs; after a crash the next worker answers an
unfinished call "outcome unknown" instead of running it twice
([A task is a log](./how-it-works/event-log.md), [Crash recovery](./how-it-works/recovery.md)).

**Waiting holds no process.** A permission prompt, an `AskUserQuestion`, a sub-agent or a timer
*parks* the task: it logs `Waiting`, releases its claim and is gone from every worker until its
inbox gets the answer. A waiting task is a row and a log in the store, with no thread or
process behind it ([Waiting and waking](./how-it-works/waiting.md)).

**Everything is a plugin.** The kernel runs logs; every capability is a plugin behind one
`Extension` interface, found by name through Python entry points or as a single file in
`~/.noeta/plugins/`. A task records the plugins it started with, and a worker missing one
never runs the task without it ([The plugin system](./how-it-works/plugins.md),
[Write a plugin](./guides/plugins.md)).

**Private by default.** noeta sends nothing anywhere except requests to the model provider
you configure: no telemetry, no log upload, no update check. Tools reach out only when the
model calls them, under your permission rules.

## Measured next to Claude Code

<div class="nt-stats">
  <div class="nt-stat"><div class="num">+5.4</div><div class="label">SWE-rebench 07-2026: noeta − Claude Code, standard error 3.6</div><div class="sub">111 tasks, 50 min cap, two runs pooled · noeta 50.5 %, Claude Code 2.1.312 45.0 % · on par or slightly ahead, not significant</div></div>
  <div class="nt-stat"><div class="num">+1.9</div><div class="label">Terminal-Bench 4.0: noeta − Claude Code, standard error 3.2</div><div class="sub">54 of 66 tasks, 2 h cap, one run · noeta 3.7 %, Claude Code 1.9 % · near zero for both on this model</div></div>
</div>

<p class="nt-muted">noeta 1.0 and Claude Code 2.1.312 driven by the same model, DeepSeek V4.1
Flash (effort <code>high</code>), not a Claude model; same tasks, caps and machine. The cost per
run was in the same range ($5.01–$5.77 against $5.70–$6.25 on SWE-rebench).
<a href="./benchmarks.html">Method, every run and the caveats →</a></p>

## Where to go next

<div class="nt-cards">
  <a class="nt-card" href="./start/quickstart.html"><strong>Quickstart</strong><span>Install, pick a model, and run the terminal UI or <code>noeta -p</code> in five minutes.</span></a>
  <a class="nt-card" href="./start/embedding.html"><strong>Embed it in Python</strong><span>A store, a runtime and a client in your own program.</span></a>
  <a class="nt-card" href="./guides/server.html"><strong>Run it as a server</strong><span>One Postgres store, many workers, an HTTP API in front; a Docker compose file to start from.</span></a>
  <a class="nt-card" href="./why-noeta.html"><strong>Why noeta</strong><span>What it is for, who it is for, and what it is not.</span></a>
</div>
