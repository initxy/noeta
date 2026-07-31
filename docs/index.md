---
layout: home

hero:
  name: "Noeta"
  text: "A durable, provider-neutral runtime + SDK for AI agents"
  tagline: A Python library for long-horizon agents. Task state is folded from an append-only event log, so a killed process resumes where it stopped; tasks suspend for a human, a timer, or a subtask and wake durably. Import noeta.sdk and drive the engine in-process — no server, and no credentials needed to run it.
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
    details: A task's ground truth is its append-only event log, never a value held in memory. Kill the process mid-task; the next worker folds the log back, seals the interrupted attempt, and either re-drives it or parks it for a human — it never silently re-runs a side-effectful call.

  - title: Fully inspectable
    details: Every step, LLM round-trip, tool call, guard verdict, and per-turn token/cache count is an event in the log. The trace answers why a step happened, not just what happened.

  - title: Long-horizon by design
    details: A task suspends to wait on a person, a timer, or a subtask, and wakes exactly once when the condition fires — the match is durable, so a crash between wake and resume re-delivers rather than loses it.

  - title: Provider-neutral
    details: Anthropic, any OpenAI chat-completions gateway, and the OpenAI Responses API sit behind one internal protocol that never names a vendor. Swapping endpoints is wiring, not a rewrite — the kernel is barred from importing a provider package.

  - title: In-process, no server
    details: Install noeta-sdk, import noeta.sdk, and drive the engine in your own process. There is no HTTP hop between your code and the runtime, and no daemon to operate.

  - title: Offline out of the box
    details: FakeLLMProvider in noeta.sdk.testing runs the whole SDK deterministically with no API key and no network — the same double the engine's own suite runs on.

  - title: Every capability is a plugin
    details: Tools, agents, policies, guards, observers, MCP servers, and sandbox providers are all manifest-declared contributions over sixteen extension surfaces. Noeta's own built-ins ride the identical loader your plugin does.
---
