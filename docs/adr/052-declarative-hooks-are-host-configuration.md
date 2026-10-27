# ADR-052: Declarative hooks are host configuration

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-010](010-guard-observer-hooks.md) (Hooks have exactly two roles: Guard and Observer). Where the two disagree, this record wins.

## Decision

The `HookGuard` and the `HookObserver` were built but unreachable: nothing a
host could set fed their rules. They are now wired from one field,
`HostConfig.hooks` (a `HooksConfig`, validated at construction).
`pre_tool_use` rules feed the existing `HookGuard` input of the default guard
stack, so the resume rule of [ADR-010](010-guard-observer-hooks.md) is unchanged: the rules are part of the guard
shape, and a resuming host must pass the same ones. `post_tool_use` and
`notification` rules build one live-only `HookObserver` per Client, subscribed
at construction and stopped by `shutdown()`. The Client reaches it through a
loader doorway (`noeta.builtins.governance.impl:build_hook_observer`, resolved
by `noeta.client.parts`), never a static import, and the rule types
(`PostToolUseRule`, `NotificationRule`) moved beside `PreToolUseRule` in
`noeta.runtime.governance` so the SDK can name them; the built-in re-exports
them. Hook rules are host configuration, not agent identity, so they stay out
of `AgentSpec` like every other `HostConfig` field.
