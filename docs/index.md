---
layout: home
title: "noeta — the open-source, Claude Code-compatible coding agent"
titleTemplate: false

hero:
  name: noeta
  text: The coding agent that never loses its place.
  tagline: The model sees what Claude Code shows it. Underneath, every task is an append-only log with one writer, so it survives a crash, waits days for a person at no cost, and runs on one laptop or many machines unchanged.
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
  - title: Claude Code's contract
    details: The same tool names and schemas, prompt rules, reminders, compaction and request shape. It reads Claude Code's formats for settings, hooks, skills, agents, MCP servers and plugins, from noeta's own folders.
    link: /how-it-works/parity
    linkText: How it matches Claude Code
  - title: A task is a log
    details: Ten event types, one writer per task, fenced by an epoch. State is a fold of the log, so a crashed task continues on any worker and never re-runs a tool blindly.
    link: /how-it-works/event-log
    linkText: The event log
  - title: Waiting costs nothing
    details: A question, an approval, a sub-agent or a timer parks the task. It holds no worker and no process until the answer lands in its inbox, minutes or weeks later.
    link: /how-it-works/waiting
    linkText: Waiting and waking
  - title: Everything is a plugin
    details: Files, shell, permissions, hooks, skills, sub-agents, MCP, memory and compaction all sit behind one Extension interface. The built-in plugins load exactly like yours.
    link: /how-it-works/plugins
    linkText: The plugin system
  - title: One laptop or many machines
    details: sqlite on a laptop, Postgres for many workers. Any worker continues any task; a front end attaches to any server with the same API.
    link: /guides/server
    linkText: Run it as a server
  - title: Every way in
    details: A terminal UI, Claude Code's -p and stream-json protocol, ACP for editors, an HTTP / WebSocket API, a web app, and a Python SDK. All of them are one client API.
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

**Claude Code's contract on a server.** Models are tuned against Claude Code's tools and
prompts, so noeta copies what the model sees, not how Claude Code is built: tool names and
schemas, the order and roles of prompt parts, reminders, compaction, the request a provider
gets. `make parity` replays Claude Code's recorded requests against noeta and reports every
difference ([Matching Claude Code](./how-it-works/parity.md)). Your settings, hooks, skills,
agents, MCP servers and plugin folders keep their Claude Code format but live in `~/.noeta`
and `.noeta/`; noeta never reads `~/.claude` ([Configuration](./start/configuration.md)).

**An event log with one writer.** A task is a sequence of ten event types in the store. A
worker must claim a task before it writes, and every append names the sequence number it
expects and the claim's epoch, so a worker that froze and lost its claim can never write again.
A tool call is logged as started before it runs; after a crash the next worker answers an
unfinished call "outcome unknown" instead of running it twice
([A task is a log](./how-it-works/event-log.md), [Crash recovery](./how-it-works/recovery.md)).

**Waiting costs nothing.** A permission prompt, an `AskUserQuestion`, a sub-agent or a timer
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
  <div class="nt-stat"><div class="num">50.5 %</div><div class="label">SWE-rebench 07-2026</div><div class="sub">111 tasks, 50 min cap, two runs pooled · Claude Code 2.1.312: 45.0 % · difference +5.4 (SE 3.6)</div></div>
  <div class="nt-stat"><div class="num">3.7 %</div><div class="label">Terminal-Bench 4.0</div><div class="sub">54 of 66 tasks, 2 h cap, one run · Claude Code: 1.9 % · near zero for both on this model</div></div>
</div>

<p class="nt-muted">noeta 1.0 and Claude Code 2.1.312 on the same model (DeepSeek V4.1 Flash,
effort <code>high</code>), same tasks, caps and machine. noeta also cost less per run
($5.01–$5.77 against $5.70–$6.25 on SWE-rebench).
<a href="./benchmarks.html">Method, every run and the caveats →</a></p>

## Where to go next

<div class="nt-cards">
  <a class="nt-card" href="./start/quickstart.html"><strong>Quickstart</strong><span>Install, pick a model, and run the terminal UI or <code>noeta -p</code> in five minutes.</span></a>
  <a class="nt-card" href="./start/embedding.html"><strong>Embed it in Python</strong><span>A store, a runtime and a client in your own program.</span></a>
  <a class="nt-card" href="./guides/server.html"><strong>Run it as a server</strong><span>One Postgres store, many workers, an HTTP API in front.</span></a>
  <a class="nt-card" href="./why-noeta.html"><strong>Why noeta</strong><span>What it is for, who it is for, and what it is not.</span></a>
</div>
