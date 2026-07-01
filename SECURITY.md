# Security Policy

Noeta is a single-host, durable, task-oriented agent runtime. Because it runs
agents that execute real tools on the host, we take security reports seriously
and want them to reach us privately.

## Supported versions

Noeta is pre-1.0 and preview-stage (`0.1.0`). Only the latest `main` is
supported — fixes land on `main`, and there are no backports to older tags or
branches. If you're on an older checkout, update to `main` before reporting so
we're looking at the same code.

| Version        | Supported          |
| -------------- | ------------------ |
| latest `main`  | :white_check_mark: |
| `0.1.0` tag    | best-effort        |
| anything older | :x:                |

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through GitHub's private vulnerability reporting:

- Go to the [**Security** tab → **Report a vulnerability**](https://github.com/initxy/noeta/security/advisories/new)
  (or use the "Report a vulnerability" button on the repository's Security page).

If you can't use GitHub advisories, email `initxy0@gmail.com` instead.

A useful report includes: what the issue is, the affected package
(`noeta-runtime` / `noeta-sdk` / `noeta-agent`), a reproduction or proof of
concept, and the impact you think it has.

## Scope / notable surface

Noeta executes shell commands and tools on the host and connects out to LLM
providers and MCP servers. The sensitive surface is **untrusted task input
driving tool execution**: task text, tool arguments, and model output can all
flow into filesystem writes, shell commands, and outbound provider/MCP calls on
the host that runs the agent. Treat that path as the primary attack surface when
assessing impact. Configuration secrets (provider API keys) and the
event-sourced storage on disk are secondary but in scope.

## Response expectations

This is a preview-stage, best-effort project. We aim to acknowledge a report
within a few business days and will keep you updated as we investigate. Please
give us reasonable time to ship a fix before any public disclosure; we're happy
to credit you if you'd like.
