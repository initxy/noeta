# A session pins one absolute workspace path into durable state; permission mode is a per-turn, non-durable knob

## Context

One host process serves many working directories and many providers at once, and each session picks its own run configuration. A session must also resume back to the directory it was created against, whatever happens to the host's configuration in between.

## Decision

**Workspace, provider and permission mode are session-level, not process-level.** The host is a table of providers and a default workspace; a session overrides both when it starts.

**A session pins the full absolute path.** `TaskHostBoundPayload.workspace_dir` carries the resolved absolute path, written once at task creation. Every later turn folds that path, and resume reads only it — no name resolution, no directory pool, no lookup outside the event log. The Engine cache is keyed on that absolute path and on the provider name, so two sessions on different directories or providers never share an Engine.

**Path admission has no allowlist.** The host hands over any absolute directory; the only requirement is that it is an existing directory.

**Permission collapses to three modes, switchable per turn, never persisted.** The modes are `default`, `acceptEdits` and `bypassPermissions`: `default` gates every tool declaring a non-low risk level, `acceptEdits` exempts the three edit-class tools, `bypassPermissions` gates nothing. The value arrives with each turn and overrides the host's explicit `require_approval_tools`. It is not written to the event log — resume does not re-run the permission gate, since recorded approvals and denials are read back from the log — so the mode passed at resume cannot change the outcome. A process-local carrier keyed by task id (`note_turn_permission` on the resolver) hands the value across the asynchronous boundary between the request thread that seeds a turn and the thread that later resolves its Engine.

**Provider is a name; the instance never enters the log.** `SdkHost.providers` is a name → instance table plus a `default_provider`; a single-provider caller supplies one instance and it folds into a one-entry table. The selector crossing any wire is the provider's name, and the name folds into the existing model binding (`ModelBoundPayload.provider`) rather than opening a separate binding event. The model catalog carries model spec, price and capability only, and no provider shape.

**Skills merge across tiers; memory is a single global layer.** Skills resolve built-in < global < workspace-local, with the workspace-local tier winning, so the workspace tier follows the session's directory. Memory is pinned to one global root (`~/.noeta/memories`) and does not drift with the session workspace; a host that needs to split the store injects a per-task memory-root resolver instead.

## Rationale

- **Resume depends only on the event log.** An absolute path in `TaskHostBound` is reconstructible from persisted data alone. Anything else a session could be pinned to — a registry entry, a symlink, a hashed directory name under a pool — lives outside the log, so it can be renamed or cleaned up, and a resumed session then either fails to find its directory or silently runs against the wrong one. That failure is invisible and destructive, so the path itself is the durable unit and any grouping of sessions by directory stays off the resume path.
- **Sinking the three run-config dimensions to the session** is what lets one process serve many directories and providers; keying the Engine cache on them keeps the sharing honest.
- **Provider folds into the model binding** because provider and model are chosen and switched together — one binding event for one pair. Only the name is recorded, because the instance carries connection details and secrets that must never reach the log.
- **Permission is not durable** because it is never replayed; recording it would be provenance for a decision the resume path does not make.
- **Memory is one layer** because its value is remembering across contexts; slicing it by workspace weakens exactly that and forces a decision at write time about which layer a new memory lands in.
- **Zero path allowlisting is a deliberate opening** under a single-owner trust assumption — the same assumption that lets a caller self-elevate to `bypassPermissions`. Fine-grained directory authorization is the first thing a multi-tenant deployment has to add.

## Alternatives considered

1. **A fresh host per session.** Rejected: it duplicates the provider table and the rest of the heavy machinery per conversation. The workspace is passed once at start, welded into durable state, and folded each turn instead.
2. **A workspace as a named subdirectory of a base pool, with only the name recorded.** Rejected: resume then depends on a name → directory mapping that is not in the log, so the mapping can go missing or resolve elsewhere.
3. **A separate provider-binding event, or pinning the provider → model mapping into the model catalog.** Rejected: the pair reuses one fold path, and putting provider shape into the catalog would break its neutrality (`provider-neutral.md`).
4. **Splitting memory into a project layer and a global layer.** Rejected: it forces a per-write decision about which layer receives a new memory, for a split that reduces what memory is for.
5. **Persisting the per-turn permission mode as a new event type, or fixing the mode at task creation.** Rejected: the mode is never re-judged on resume, so it has nothing to record; and fixing it at creation means changing it requires a new session.
6. **A directory allowlist for workspace admission.** Rejected under the single-owner trust assumption; it is the boundary a hosted deployment has to reintroduce.

## Consequences

- The pieces land in `noeta.client.host` (the providers table, workspace resolution, the Engine cache key), `noeta.execution.driver` and `noeta.execution.resolver` (welding the path, folding it, the per-turn carriers), and `noeta.protocols.events` (`TaskHostBoundPayload.workspace_dir`, `ModelBoundPayload.provider`).
- `workspace_dir` is omitted from the canonical event form when absent, so a recording without a session workspace folds to the host's default directory.
- The per-turn carriers are process-local: a host restart between turns loses them, and the next turn supplies its own value.
- Multi-tenancy has to revisit path admission and the single global memory root together.
