# Noeta is an MCP client over a request/response subset; credentials stay host-side and a resume rebuilds the tool set from the recording

## Context

An MCP server — local stdio or remote HTTP — exposes tools, prompts and
resources that an Agent should be able to use. Noeta is always the client, the
borrowing party. Two hard lines constrain how: the client is **synchronous and
single-threaded**, and a Task's tool set must survive **resume** — the
recording, never process memory, decides which servers a Task was given, so a
restart or another machine picking the Task up reaches the same tool set.

## Decision

**Scope is the request/response half of MCP.** Both clients implement
`initialize`, `tools/list`, `tools/call`, plus `prompts/list`, `prompts/get`,
`resources/list` and `resources/read`. Neither implements the server-initiated
half — no `list_changed`, no `sampling`, no `elicitation`. The HTTP client posts
one JSON-RPC request over the standard library and reads back exactly one
response, accepting either a bare JSON object or an event-stream body carrying a
single data line; it never holds a stream open. Resource templates and lazy tool
search are out of scope, and exposing Noeta *as* an MCP server is the opposite
direction and out of scope.

**A Streamable HTTP session is joined, not held open.** A 2025-spec server
assigns an `Mcp-Session-Id` on `initialize` and rejects every later request that
does not carry it. The HTTP client captures that id, finishes the lifecycle with
`notifications/initialized` (answered `202` with an empty body, so nothing is
parsed), echoes the id on every request it makes afterwards, and `DELETE`s it
when the connection is torn down — retired, expired, or closed with the host. A
`404` while an id is in play means the server dropped the session: the client
forgets the id and the fault travels the path a dead connection already travels
(retire, connect fresh once, handshake again). A server that assigns no id stays
stateless and sees exactly the requests it saw before, the notification
included: the lifecycle asks for it unconditionally, and sending it only into a
session is the conservative cut — a stateless server has no state for it to
advance, and the connectors that work today keep a byte-identical wire. A
notification the server refuses fails the connect, the same fail-fast the stdio
client gives it. None of this holds a stream open — it is still one POST, one
response — and the id is treated like a credential: on the wire only, never in
an event, a recording, or provenance. The injectable POST function may return
raw body bytes (the original shape, stateless) or an `McpHttpResponse` carrying
the response headers, which is what a host-supplied transport returns to join a
session; only the client's own transport sends the `DELETE`, since a host that
supplied a transport owns its network path.

**The transport belongs to the `mcp` built-in.** The stdio and HTTP clients, the
tool wrapper, and the prompt and resource helpers live there; the shared
vocabulary — the reserved prefix, the server specs, the error types, the
injectable POST function — sits kernel-side so the kernel builder's
reserved-prefix check and the public SDK surface can name it statically.

**Each server tool becomes an ordinary Tool** named `mcp__{alias}__{tool}`, with
every character outside `[A-Za-z0-9_-]` replaced and the whole name matching a
provider-safe pattern of at most 64 characters. An empty raw name, a name that
sanitizes to empty, an over-long name, and an intra-server collision all fail
fast — no silent truncation. `mcp__` is a reserved prefix, and a built-in tool
occupying it is a hard configuration error. Being ordinary Tools, they flow
through the one tool set into the composer schema, the Policy, and the permission
Guard with no special casing.

**Credentials never leave the host.** A host supplies a resolver callback that
maps an enabled alias to its full spec; the SDK never holds the config store.
Static credentials — a bearer token, an API key, a custom header — are injected
into the HTTP request headers at call time and appear in no request body, no
event, and no recording. A turn carries only the list of enabled aliases.

**List at every turn's build, over a pooled connection; the tool set is
frozen for the turn.** The enabled aliases resolve to specs, each server's
connection is acquired from the host's pool (see below), `tools/list` runs,
and the tool set is frozen for the turn (the Engine is a per-turn value —
see the engine-per-turn ADR). Order is deterministic — servers alias-sorted,
tools within a server sorted by their Noeta-side name — so the tool dict
order, the schema order and the stable hash reproduce. A per-server connect
/ handshake / list fault drops that server, records a durable skip event the
host surfaces (once per outage, not per turn), and continues with the rest;
one bad connector never sinks the Task. A duplicate alias is a hard
configuration error, because it is a caller wiring bug rather than a connect
fault. The discovery path that populates a configuration menu takes the
opposite stance and fails fast, tearing down every client it had opened.

**Connections are pooled by server identity and the host's scope.**
`McpConnectionPool` keys a live client on the spec plus a scope —
`(transport, scope, alias, argv, env)` or `(transport, scope, alias, url,
headers)`, in memory only — so every task naming one server in one scope
shares one connection, tenants with different credentials do not, and a
host that resolves a scope per task (`HostConfig.mcp_scope_resolver`, the
same tenancy seam as `memory_root_resolver`; `None` = the shared scope)
keeps a stateful server's state — a browser, a login — from crossing
tenants even when the spec is byte-identical. A build acquires and counts a
holder; the turn's Engine releases when the turn settles; a holder-less
connection expires after `HostConfig.mcp_idle_ttl`;
`Client.reconnect_mcp(alias=None)` retires connections (in every scope) so
the next build reconnects while a turn still holding one keeps it;
`Client.shutdown()` closes them all. Each client serializes its JSON-RPC exchanges on an instance
lock, so concurrent turns on one connection queue rather than interleave. A
pooled connection that stops answering `tools/list` is retired and the
server connected fresh once before it is skipped for the turn.

**A resume takes the server list from the recording and reconnects it.** The
real tool spec — name, input schema, and description verbatim — is pinned into
the first recorded LLM request, so what a run was given stays auditable byte for
byte whatever the server does afterwards. A resume *within* the turn reuses the
turn's Engine and touches nothing. A resume in another process — a restart, a
daemon worker, another machine — has lost the per-turn alias selector, so it
reads the enabled aliases back off the folded provenance and connects them
through the host's resolver, in the pool scope a turn-open build would use.
Reconnecting is not optional: a schema rebuilt from a recording carries no live
client, and the call an approval resumes into has to execute. With the servers
unchanged the rebuilt tool set — and the stable prefix over it — is the recorded
one; a server that changed underneath shows up exactly as it would at the next
turn's `tools/list`, and an unreachable one is skipped the way a turn-open build
skips it.

**Provenance is recorded, credential-free.** One event per Task — and one
more each time the enabled set changes — emitted at the build that connects,
records which aliases were enabled and which of each server's tools were
ticked — names only, never a URL, token or header. A Task with no MCP emits
nothing and folds to an empty record with zero drift. Tool *behaviour* is not
carried here; the recorded request spec pins that. This record is also what a
resume in another process reads to know which servers to connect, so a rebuild
that lands on the same set adds no second event.

**Prompts arrive as recorded messages.** A server's prompts hang on the same
slash-invocation menu skills use, named `/mcp__<alias>__<prompt>`. Expanding one
flattens the returned messages into the turn's opening content and records it as
an ordinary message tagged with a system origin — so a resume reads it back and
never re-calls the server.

**Resources are user-driven reference material.** `@<alias>:<uri>` names an MCP
resource and `@<path>` names a workspace file; both ride the same content channel
and the same selector. The content is read at send time and snapshotted into a
recorded message, not stored as a path. No tool lets the model pull a resource
itself.

**Server-controlled injected text is capped.** A prompt expansion or resource
snapshot is truncated at the inline-content ceiling with a visible marker naming
what was cut.

**Subtasks inherit MCP only by opting in.** A child inherits the parent's enabled
aliases only when the child's own `AgentSpec` activates the `mcp` plugin; a spec
without that activation stays MCP-free.

## Rationale

The request/response subset is what keeps the client synchronous and
single-threaded. Every server-push feature needs a long-lived stream and a
reader: `list_changed` contradicts freezing the tool set for the turn (a per-build `tools/list` over the pooled connection covers the same need by pulling),
`sampling` hands a remote server the power to initiate model calls, which is hard
to govern for both safety and determinism, and `elicitation` interrupts mid-turn.
Common servers are entirely request/response, so the subset suffices.

Credentials staying host-side is the host boundary. A request body carrying
tokens would spread them into logs, recordings and provenance; a callback that
resolves an alias into a spec keeps them in one place under operator control.

A resume depends on the servers being reachable — a tool has to be live to be
called — but never on their being identical, because the tool spec that actually
shaped the recording is pinned into it. Two live runs against the same server can
legitimately differ; faithfully recording the tools this run was given is the
guarantee that matters, and a server that is down at resume costs the Task that
server's tools rather than the Task.

Injected server text is both a prompt-injection surface and a context bomb — the
transport caps only at megabytes — so it is bounded before it reaches the model.

Resources are read-material chosen by a person; tools are actions taken by the
model. Snapshotting the content rather than a path is what keeps a resume from
drifting when the underlying file or resource changes.

## Alternatives considered

1. **Implement full streamable HTTP, including the server-push half.** Weighed
   and rejected: it requires a persistent open stream and a background reader,
   contradicting the synchronous single-threaded client and the frozen tool set,
   and `sampling` would hand a remote server the ability to start model calls.
   The rejection is the push half only — the session half (see the session
   decision above) is one request header and costs neither a stream nor a
   thread, and without it every stateful hosted server is unreachable.
2. **Expose Noeta itself as an MCP server.** Rejected: it is the opposite
   direction from connecting out, and shares nothing with this client.
3. **Support OAuth as the auth mechanism.** Rejected: the authorization redirect
   needs a real browser, which a host process cannot provide, and token refresh
   and reconnect are a separate body of complexity. Static credentials cover the
   bulk; the spec shape leaves room for a refresh slot.
4. **Ship a lazy tool-search layer so large servers do not flood the context.**
   Rejected: it reaches into the composer and context core. Narrowing the ticked
   tool subset at configuration time keeps the active set small without that.
5. **Send credentials along with the turn request.** Rejected: it breaks the host
   boundary outright — tokens would end up in request bodies and in anything
   derived from them.
6. **Let a subtask never inherit MCP, or always inherit it.** Rejected: never
   inheriting guts delegation, since the child doing the real work cannot touch
   the connector; always inheriting breaks a read-only child's isolation. Opting
   in per spec is the only shape that respects both.
7. **Give the model a tool to fetch resources on its own.** Rejected: it erases
   the resource/tool boundary and makes the context budget ungovernable.
8. **Treat a failed connect at task start as fatal.** Rejected: one unreachable
   connector would sink an otherwise workable Task; skipping with a durable,
   surfaced warning keeps the rest usable.

## Consequences

- The clients, tool wrapper, prompt and resource helpers live in the `mcp`
  built-in; the reserved prefix, server specs and error types sit kernel-side in
  `noeta.runtime.mcp`. None of it may seep into a host or product layer.
- A host supplies the alias resolver and, optionally, an injectable POST function
  so tests can run the whole path without real network; `build_mcp_tools`
  takes the host's pool on the task-start path and connects fresh, caller-owned
  clients without one.
- Every process that may resume a Task needs the alias resolver wired, because
  that is where the reconnect reads its specs; a process without one rebuilds
  the Task with no MCP tools.
- Credentials have hard in/out constraints: never into a request body, never into
  a recording, never into a host-config fingerprint. Any auth mechanism must keep
  the token landing host-side.
- The tools land in a fixed merge band ahead of custom tools, so a custom tool
  can intentionally shadow an MCP tool of the same name and the merge order stays
  byte-stable.
