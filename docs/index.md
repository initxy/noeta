---
layout: home

hero:
  name: "Noeta"
  text: "Durable runtime for AI agents"
  tagline: Open-source and self-hostable. A task survives crashes, pauses for a human or a timer, and records every step — provider-neutral, and offline out of the box.
  actions:
    - theme: brand
      text: Quickstart
      link: /tutorials/quickstart
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

  - title: Bring your own agent
    details: The runtime hosts and schedules; you supply the policy, tools, and context.

  - title: Offline-first
    details: A deterministic stub provider runs the whole stack with no API key and no network.
---
