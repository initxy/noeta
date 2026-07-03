---
layout: home

hero:
  name: "Noeta"
  text: "Durable runtime for AI agents"
  tagline: |
    Open-source, self-hostable, provider-neutral, event-sourced.
    Built for long-horizon, task-oriented agents.
  actions:
    - theme: brand
      text: Quickstart
      link: /tutorials/quickstart
    - theme: alt
      text: View on GitHub
      link: https://github.com/initxy/noeta

features:
  - icon: 🛡️
    title: Durable by construction
    details: Every state change is an appended event. Task state is deterministically folded from the log, never held across runs. Kill the process mid-task and fold brings it right back.
    link: /concepts/event-sourcing
    linkText: Learn the model

  - icon: 🔌
    title: Provider-neutral
    details: Anthropic and OpenAI-compatible endpoints are adapters behind one internal protocol. Swapping providers is wiring, not a rewrite.
    link: /concepts/provider-neutrality
    linkText: See how it works

  - icon: 🤖
    title: Bring your own agent
    details: The runtime hosts and schedules; you supply the policy, tools, and context. A ReAct policy and a coding agent ship in-tree, but nothing forces you to use them.
    link: /how-to/use-the-coding-agent
    linkText: Use the agent

  - icon: 🧪
    title: Offline-first
    details: A deterministic stub provider runs the whole stack with no API key and no network, so install, storage, and wiring are provable on a fresh checkout and in CI.
    link: /tutorials/quickstart
    linkText: Try it in 5 minutes

  - icon: 🧩
    title: Use the layer you need
    details: Embed the kernel, import the SDK, or run the batteries-included coding agent with its bundled web UI.
    link: /reference/sdk
    linkText: Browse the API

  - icon: 📜
    title: Event-sourced truth
    details: Every step an agent takes lands in an append-only EventLog. Suspend/resume, crash recovery, replay, and exactly-once wake are not bolted on — they fall out of treating the log as truth.
    link: /concepts/task-model
    linkText: Understand tasks
---

## Screenshots

<figure>
  <img src="./assets/web-app.png" alt="The bundled web app — chat composer with a running task" style="max-width: 840px; border-radius: 8px;">
  <figcaption style="text-align: center; color: var(--vp-c-text-2); margin-top: 8px;">The bundled web app — chat composer with a running task.</figcaption>
</figure>

<figure>
  <img src="./assets/trace.png" alt="The per-task trace view — the folded event stream" style="max-width: 840px; border-radius: 8px;">
  <figcaption style="text-align: center; color: var(--vp-c-text-2); margin-top: 8px;">The per-task trace view — the folded event stream.</figcaption>
</figure>
