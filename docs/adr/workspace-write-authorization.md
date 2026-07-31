# Reads are unfenced; writes are contained to the workspace and widened only by host authorization resolved per call

## Context

Every fs tool resolves a user-supplied path through one `WorkspaceRoot`. That seam is the only place path containment is decided, and it has to answer two different questions: where may a tool *read*, and where may a tool *write*. The two answers are not the same, and the containment check is not a sandbox — `shell_run` reaches the whole filesystem through a subprocess, so a path check inside the tools is never the boundary that keeps a secret off a host.

## Decision

**Reads are not fenced.** `read`, `grep` and `glob` join a relative path onto the workspace and canonicalise it, and take an absolute path wherever it points (`resolve_anywhere`). Reading a neighbouring checkout, a skill pack's bundled reference, or a file under `/usr/share` is done by naming it. `glob`'s optional `path` chooses the tree to walk and its pattern stays relative to that tree, which is what keeps every walk bounded.

**Writes are contained.** `edit`, `write` and `apply_patch` resolve through the fenced path, and a target that lands outside the workspace fails with a tool error rather than an exception.

**The unit of widening is a directory, not a call.** `WorkspaceRoot.extra_roots` carries absolute directories the host has authorized this caller to write into. Containment is matched component-wise (`path_within`, i.e. `Path.is_relative_to`) after canonicalisation — never string-prefix, so `/srv/app-old` is not covered by an authorization on `/srv/app`, and no symlink launders a path into an authorized root.

**Widening is resolved per call, not bound at build time.** `WriteRootsResolver` is `task_id -> extra writable directories`, consulted by `authorized_workspace` at invoke time from the task id on the tool context. This is what lets an authorization granted *while a task sits paused on an approval* take effect on the resumed call. Binding the directories into the tool set at construction would require rebuilding the tool set to honour a new authorization, which moves the prompt's stable prefix and breaks the KV-cache invariant the builder exists to protect.

**The authorization path fails closed.** No resolver, no task id on the context, a resolver that raises, a relative or non-string entry — every degenerate case yields the unwidened workspace. A broken authorization path can only ever refuse a write, never permit one.

**The default is the single-root wall.** `HostConfig.write_roots` defaults to absent, so a bare embedding keeps writes inside the workspace, full stop — the only honest answer for a host with nobody to ask. The seam exists for a host that can actually put the question to a person and remember the answer.

**The predicate is published.** `path_within` is exported on `noeta.sdk` so a host deciding what to authorize asks the question exactly the way the fence will answer it.

**A sandbox session's fence is lexical.** A container workspace root does not exist on the host, so canonicalisation is `normpath` rather than `realpath`: the fence still rejects `..` escapes and absolute escapes above the container work directory, and the container is the real isolation boundary.

**There is no denylist.** Nothing is permanently unwritable, and combined with unfenced reads a session can read any file the host process can read, including the host's own configuration and its provider key. That is the accepted posture for a single-owner deployment — the same trust assumption behind zero path allowlisting on workspace admission (`workspace-and-session-path.md`) and a caller that can self-elevate to `bypassPermissions`. It is not a claim that the boundary would hold for a multi-tenant deployment.

## Rationale

- **The asymmetry between read and write is the whole idea.** A fence costs something every time it stops legitimate work and buys something only when it stops damage. For writes the trade is worth it: a wrong write outside the workspace is irreversible and silent. For reads there is nothing to buy — the act is recoverable, and the same bytes are one `shell_run` away — so a refusal only produces a dead end the model cannot escalate.
- **Component-wise containment, stated once.** A string-prefix check would be a silent authorization bug (`/srv/app` admitting `/srv/app-old`), and the failure stays invisible until the moment it matters. One predicate, shared by the fence, the display helper and any host-side gate, keeps the question and the answer identical.
- **A resolver, not a value.** The shape is forced by the resume path: authorization can change *during* a suspended call, while the tool set must not change with it.
- **Fail-closed is not a detail.** The seam widens a destructive capability, so every ambiguity in it has to resolve toward refusal; otherwise a misconfigured host silently grants more than it meant to.

## Alternatives considered

1. **Fencing reads to the workspace, with a build-time allowlist widening reads to specific extra directories.** Rejected: the widening exists only because the general rule is wrong. Refusing a read buys no protection while a subprocess reads the same bytes, and it converts a recoverable observation into a refusal the model reports to a person who would have said yes.
2. **Authorizing each write call rather than a directory.** Rejected: work in a neighbouring checkout touches `src/`, then `tests/`, then a config file; per-call authorization asks about the same tree over and over. A directory covers everything beneath it, which is the granularity the work actually has.
3. **Binding the authorized directories into the tool set at construction.** Rejected: honouring a new authorization would mean rebuilding the tool set mid-conversation, which perturbs the stable prompt prefix and discards the KV cache.
4. **A string-prefix containment check.** Rejected: it silently admits sibling directories whose names share a prefix, and the resulting over-permission is undetectable from the outside.
5. **A permanent denylist of sensitive paths.** Rejected: with unfenced reads and an unsandboxed subprocess it delivers the appearance of a boundary without the boundary, while adding a list that must be maintained and will still be wrong.

## Consequences

- `noeta.runtime.workspace` owns the fence, the containment predicate and the per-call widening; the fs tools under the `fs` built-in consume them, and `HostConfig` is where a host wires the resolver.
- A session can read anything the host process can read. On a shared host, deploy accordingly.
- A host that wires the resolver owes it cheapness and totality: it runs on every write call, and raising from it silently narrows the fence rather than surfacing an error.
- The write fence and any host-side gate deciding whether to ask a person must read the same authorization data, or they drift into a state where one says yes and the other refuses.
