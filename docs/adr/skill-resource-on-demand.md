# Skill resources are read on demand: the renderer emits only the skill's base directory, the model reads with the generic `read`

## Context

A skill is a `SKILL.md` body plus whatever files sit beside it — reference documents, checklists, scripts. Two-stage invocation (`model-driven-skill-invocation.md`) keeps the body out of the context until the model selects the skill; the third stage of progressive disclosure is the bundled files, which a given turn usually does not need at all. Whatever mechanism delivers them must leave the renderer pure: the composer's `semi_stable` bytes are a function of folded state, not of what happens to be on disk when compose runs.

## Decision

**The renderer never touches the disk and never inlines a resource.** An activated skill renders as exactly one message — its name, its one-line description, a base-directory line, then the body verbatim. A skill that names no resources and a skill that names ten render the same shape.

**The base-directory line is one line naming the skill's absolute base directory.** The renderer emits `Base directory for this skill: <source_path.parent>` ahead of the body. The path string is rendered as it was indexed — no canonicalisation, no disk read — so re-indexing the same tree reproduces the same bytes. A synthetic skill with no source path renders body-only, with no base-directory line.

**The model reads resources with the generic `read`.** It concatenates the base-directory line with a relative reference in the body (`references/foo.md`) into an absolute path and reads it. There is no dedicated skill-resource tool and no skill-root allowlist: `read` resolves a relative path under the workspace and an absolute path where it points, so a skill's bundled file is reachable by naming it.

**`run_skill_script` is separate.** Script execution keeps its own opt-in and approval path; reading a `.sh` yields its source text, never its output.

### The trade-off, stated plainly

Absolute paths enter the prompt, so the composed bytes are bound to the skill directory paths of the machine that composed them. On one machine the path is constant: the `semi_stable` segment stays cache-stable across steps and a resume re-derives the same prompt. Folding the same ledger against a skill directory at a different path would drift. `SkillDescription.source_path` is therefore part of the composed bytes, and any session that has activated a skill must fold against the same skill directory path to reproduce its prompt.

## Rationale

- **Eager loading destroys the third layer of progressive disclosure.** If naming a file in the body meant loading it, activating a resource-heavy skill would stuff every named file into the context whether or not the turn uses it — and the renderer would have to read the disk, so the same ledger would compose to different bytes as files changed underneath it.
- **A dedicated resource tool cannot give the model an error it can act on.** Its failure mode collapses "skill not discovered", "wrong relative path" and "file does not exist" into one vague message, so the model retries blindly instead of self-correcting. It is also one more narrow tool to learn, and the skill's own `SKILL.md` sits outside its allowlist. With the generic `read`, the error is the ordinary "not a file".
- **Naming a file in the body is the author's explicit intent.** It is a tighter and more meaningful scope than whatever happens to sit in the directory.

## Alternatives considered

1. **Eagerly load every file the body names into the context on activation.** Rejected: it violates progressive disclosure, bloats the context with material the turn may not touch, and a disk-reading renderer breaks compose determinism.
2. **List every file under the skill directory in the rendered message.** Rejected: incidental files and internal helpers land in the prompt, and the listing carries no authorial signal.
3. **Ship a dedicated `read_skill_resource` tool with its own resource allowlist.** Rejected: its one real advantage is machine-independent addressing, which the trade-off above already declines; the price is a vague error and one more tool in the model's surface.
4. **Add a build-time skill-root allowlist to `read`'s containment fence.** Rejected: a read is observation, not mutation, and a path check inside the tool is not the boundary that keeps a secret off a host; leaving reads unfenced (`workspace-write-authorization.md`) covers the skill case with no seam at all.
5. **Emit a relative base directory for in-workspace skills and an absolute one otherwise.** Rejected: it wires the workspace root into the renderer and produces a mix of path shapes the model has to reason about. Uniformly absolute is simplest.

## Consequences

- The base-directory line is emitted by `noeta.builtins.skills.impl.indexer`, which reads no resource bytes; resolution of an absolute read target lives in `noeta.builtins.fs.impl.read` over `noeta.runtime.workspace`.
- The skills wiring contributes no read-fence state, and there is no skill-root seam to keep in sync with the registry.
- The renderer's no-disk rule is load-bearing: reading resource bytes there would make the composed bytes vary with disk content, breaking the prompt cache and resume re-derivation together.
