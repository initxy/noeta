# ADR-010: noeta sends nothing anywhere but to the configured model provider

- Status: accepted
- Date: 2026-10-28

## Context

Agent harnesses commonly phone home: install and usage telemetry, crash reports, session-log
uploads for "improvement", update checks. Task logs hold source code, credentials that passed
through tool output, and private conversations.

## Decision

noeta itself makes no network request other than to the model provider the user configured:
no telemetry, no log or session upload, no update check, on by default or otherwise. Tools
reach the network only when the model calls them (`WebFetch`, `WebSearch`, MCP servers,
commands) under the permission rules. An operator who wants an exporter installs one as a
plugin (`on_event`).

## Rationale

A server-side harness runs on other people's infrastructure with their data; any default
upload is a data-handling decision noeta has no right to make for them, and an opt-out is
discovered too late. Keeping the core silent makes the privacy statement checkable by reading
the code.

Rejected: opt-out telemetry; session-log upload to a vendor endpoint (as some harnesses do by
default); update checks against a package index.

## Consequences

- No built-in usage analytics; decisions rely on evals, parity recordings and reports.
- A dependency or plugin that adds network traffic is a change to review against this ADR.
