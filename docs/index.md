---
layout: home

hero:
  name: "Noeta"
  text: "A durable, provider-neutral runtime + SDK for AI agents"
  tagline: A Python library for building long-horizon agents on a durable, event-sourced runtime — crash-safe exactly-once execution, suspend/wake for humans and timers, and full audit + replay. In-process like the Claude Agent SDK; offline out of the box.
  actions:
    - theme: brand
      text: Your first agent
      link: /tutorials/first-agent
    - theme: alt
      text: SDK reference
      link: /reference/sdk
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: Survives crashes
    details: A task's state is rebuilt from an append-only event log, never held in memory. Kill the process mid-task; a fresh one folds the log back and finishes the work — exactly once.

  - title: Fully inspectable
    details: Every event, LLM turn, tool call, and token/cache stat is recorded. The trace answers why a step happened, not just what.

  - title: Long-horizon by design
    details: A task can suspend to wait on a human, a timer, or a sub-task, and wake exactly once when the condition fires.

  - title: Provider-neutral
    details: Anthropic and any OpenAI-compatible endpoint sit behind one internal protocol. Swapping vendors is wiring, not a rewrite.

  - title: In-process, no server
    details: Import noeta.sdk and drive the engine in-process — no HTTP between your code and the runtime, like the Claude Agent SDK.

  - title: Offline-first
    details: A deterministic mock provider runs the whole SDK with no API key and no network.
---
