# Reads are unfenced; writes outside the workspace are authorized by the owner and remembered as grants

> **Status: the runtime half is live; the product half was never merged.**
> Shipped: reads are unfenced (`read` / `grep` resolve absolute paths anywhere),
> writes resolve inside the workspace root, and the `write_roots` seam exists
> for a host to widen them. **Not shipped**: the grants store, the trust guard,
> and the About panel described as the product surface — that work lives on an
> unmerged branch and was rolled off `main` on 2026-07-24, so there is at
> present no UI through which an owner grants a standing write root. Read the
> authorization model here as the design intent, not as a description of the
> current product.

## Context

Every fs tool resolved user-supplied paths through one `WorkspaceRoot`, which canonicalised the target (`realpath`) and asserted it lived under the session's workspace. A path that landed outside — absolute, `..`-rooted, or through a symlink — produced `WorkspaceEscape`, degraded to a `ToolResult(success=False, "path … resolves outside workspace …")`.

That wall was a hard-coded `if`, not a decision anyone could participate in. There was no approval path, no configuration, no per-session widening: the model saw a refusal string and the owner saw nothing at all. Asking to "look at how the neighbouring checkout does it" was simply impossible, and the only workaround was to close the session and reopen it against a different workspace.

The wall was also inconsistent with the rest of the product's stance on risk. `shell_run` — the tool that can read and write the entire filesystem through a subprocess — is *gated*: it pauses, the owner rules, and the ruling is remembered as a durable grant (`store/grants.py`, `host/trust_guard.py`). The file tools, which are strictly less powerful, got no gate and no grant, just a wall. So the codebase already contained the better answer; it had not been applied here. The fs module's own docstring conceded the point (§B19): containment is "about path resolution, not subprocess containment" — a tidiness fence that a shell command walks straight around.

One read-side exception had already accreted: `ReadFileTool.skill_roots`, a build-time allow-list injected by `_stage_read_fence`, widening `read` to the skill pack directories so the model could open the bundled references the renderer had just handed it an absolute path to. A special case carved out because the general rule was wrong.

## Decision

**Reads are not fenced.** `read` and `grep` resolve a relative path under the workspace exactly as before, and take an absolute path wherever it points (`resolve_anywhere`). A read is observation, not mutation; refusing it bought no protection, because `shell_run` reads the same bytes and because the path check was never the boundary that keeps a secret off a host. `skill_roots` and `_stage_read_fence` are deleted — the special case dissolves into the general rule.

`glob` keeps its workspace-relative pattern contract: its schema has no root argument, and adding one would move the stable prefix for a case `grep`'s `path` and `read`'s absolute form already cover.

**Writes outside the workspace pause for the owner and are remembered.** `edit` / `write` / `apply_patch` join the product's gated set. The gate is decided by *where the path points*, not by the tool name: a target inside the session workspace is free (that is nearly every write — the gate is not a tax on ordinary work), and a target that leaves it needs a standing `fs_write` grant. Without one the TrustGuard returns `require_approval`, the task suspends, and the owner gets an approval card.

**The unit of authorization is a directory, not a call.** An `fs_write` grant's scope is an absolute directory path covering everything beneath it, matched **component-wise** (`path_within`, i.e. `Path.is_relative_to`) after `realpath` — never string-prefix, so `/srv/app-old` is not covered by a grant on `/srv/app`, and no symlink launders a path into an authorized root. The empty scope is the unrestricted grant ("anywhere on this host"), which the write fence receives as the filesystem root.

**"Just this once" is a grant with a shorter life, not a second mechanism.** The approval card offers two answers: *allow for now* writes a grant with `source=session`, `source_id=<session id>`, deleted when the session is; *allow from now on* writes an ordinary durable manual row. Both are visible in the teammate's About panel and revocable there. Everything downstream — the guard, the write fence, the grants UI — sees one kind of row.

**The card proposes the enclosing repository.** The approval names the directory it would open: the target's nearest `.git` ancestor, else the target's parent. Ruling on the file's own parent would be safe and useless — work in a neighbouring checkout touches `src/` then `tests/`, and the owner would answer for the same repository twice.

**One authority, consulted twice.** The gate (`_fs_write_granted`, "do we pause?") and the write fence (`write_roots_for_task` → `HostConfig.write_roots` → the fs pack, "what may the tool resolve?") read the *same* grant rows. They cannot drift into a state where the gate says yes and the tool then refuses.

**The fence's widening is resolved per call, not bound at build time.** `WriteRootsResolver` is `task_id -> extra writable directories`, consulted by the write tools at invoke time from `ctx.metadata["task_id"]`. This is what lets a grant made *while the task is paused* take effect on the resumed call. Binding the roots into the tool set at construction would have required rebuilding the tool set to honour a new grant, which moves the stable prefix and breaks the KV-cache invariant the builder exists to protect. Every degenerate case (no resolver, no task id, a resolver that raises) yields the unwidened workspace, so a broken authorization path can only refuse, never permit.

**A sandbox-tier session is exempt from the gate.** Its fence is a *container* path and the container is the real isolation boundary; there is no host directory for the owner to rule on. The tool's lexical fence still refuses anything above the container workdir, exactly as before.

**No deny-list.** Nothing is permanently unwritable — not `~/.ssh`, not the product's own config. This was considered and declined by the maintainer (2026-07-22). Combined with unfenced reads it means a teammate can read any file the server process can read, including `noeta.config.json` and its provider key. That is the accepted posture for a single-owner deployment where the owner is `LOCAL_PRINCIPAL` (⊤), the same trust assumption that already justifies zero path allow-listing on workspace creation (`workspace-and-session-path.md`) and a client that can self-elevate to `bypassPermissions`. It is **not** a claim that the boundary would hold for a multi-tenant deployment; a hosted posture would have to revisit it.

## Rationale

- **The asymmetry between read and write is the whole idea.** A wall costs something every time it stops legitimate work, and buys something only when it stops damage. For writes the trade is worth it — a wrong write outside the workspace is irreversible and silent. For reads there is nothing to buy: the act is recoverable, and the same bytes are one `shell_run` away.
- **A refusal the model cannot escalate is a dead end; a pause is a conversation.** The old wall's failure mode was the model reporting "I can't" to a human who *could* have said yes. Routing the same condition to the person who owns the decision converts a dead end into a question, and remembering the answer means it is asked once.
- **The mechanism already existed.** Gate → suspend → durable ruling → grant is how `shell_run` has worked since T4/T5. Reusing it added no concept: `fs_write` is a new grant *tool*, `session` a new grant *source*. Modelling "allow for now" as a short-lived grant rather than an ephemeral per-call flag kept the resume path honest — a paused call finds its authorization in the store either way, with no in-memory state to lose across a restart.
- **Component-wise containment, stated once.** `path_within` is published on `noeta.sdk` precisely so the product's gate asks the question the way the runtime's fence answers it. A string-prefix check here would be a silent authorization bug (`/srv/app` admitting `/srv/app-old`), and the failure would be invisible until it mattered.
- **A resolver, not a value.** The seam's shape is forced by the resume path: authorization changes *during* a suspended call, and the tool set must not change with it.

## Consequences

- Sessions can read anywhere the server process can. On a shared host, deploy accordingly.
- A teammate's first write into a neighbouring repository costs one interruption; subsequent writes anywhere under it cost none.
- `HostConfig.write_roots` defaults to `None`, so the CLI and any bare SDK embedding keep the single-root wall — the widening exists only for a host that can actually ask a human.
- The three fs tools now appear in `DEFAULT_GATED_TOOLS`. Membership no longer means "always ask": `_fs_write_granted` waives the in-workspace case, as `task_update` already waived own-task progression.
