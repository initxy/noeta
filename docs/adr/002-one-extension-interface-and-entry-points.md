# ADR-002: Every capability is a plugin behind one Extension interface, found through entry points

- Status: accepted
- Date: 2026-10-28

## Context

The kernel runs logs; everything the model can do — tools, prompt parts, reminders, permission
gates, hooks, compaction, memory, sub-agents, MCP — has to plug in somewhere. Each extra plugin
interface is another contract for third parties and another way for built-ins to be special.

## Decision

- One capability interface, `Extension` (`noeta/core/extension.py`), whose methods are all
  optional: tools, context parts, reminders, commands, gates (`before_tool`), `resolve_ask`,
  `after_tool`, `on_event`, `reduce` and the rest. Backends (providers, exec envs, stores) and
  channels are their own small protocols.
- Plugins and backends are found by name through entry points (`noeta/core/registry.py`). The
  built-ins register in `pyproject.toml` exactly as a third-party package would; a plugin can
  also be a single file in noeta's plugin directories.
- The kernel carries no capability, and plugins never import each other; they meet only
  through the kernel (a tool by name, inherited config, a record documented as read by
  others). `lint-imports` and `scripts/lint-layout.py` enforce the layering.
- A task records the plugin set it runs with; a worker missing one fails the task with
  `plugin_missing` rather than running it differently.

## Rationale

With one interface, a third-party plugin has every power a built-in has, and the built-ins
prove the interface is enough. Entry points need no registry service and work with ordinary
Python packaging. Keeping plugin state in the task log (ADR-001) makes every plugin durable and
machine-independent for free. Pinning the set per task makes a fleet with mismatched plugins
fail loudly instead of producing silently different requests.

Rejected: per-feature plugin interfaces (tools, hooks, prompts each with their own API: more
contracts, uneven powers); hooks that rewrite the raw provider request (the request would stop
being a function of the log); in-process sandboxing of plugin code (not achievable in CPython;
isolation lives in the exec env, MCP servers and command hooks).

## Consequences

- New capabilities are added as plugins; a change to the kernel needs a reason no plugin can
  meet. `docs/extending.md` documents the extension points and what is deliberately not built.
- Reloading plugin code happens between turns, never inside one.
