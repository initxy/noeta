# On-demand skill resource reads: the renderer gives only the resource location, the model reads it with the generic `read`, no eager loading

## Context

This decision builds on the two-stage skill invocation of `model-driven-skill-invocation.md`: at startup only the menu is listed, and the body is rendered only after the model selects a skill. It corrects one behavior that deviated from progressive disclosure: previously, activating a skill would eagerly load the full contents of every file named in its body into the context. The final form aligns with Claude Code: the renderer emits one line with the absolute base directory, and the model reads with the generic `read`.

An early version built a dedicated tool `read_skill_resource` for this; that dedicated tool was later retired in favor of loosening the generic `read`'s fence to the skill root. What follows describes the final, post-retirement form.

## Decision

**The renderer no longer injects content or touches the disk (the core reversal, still in effect).** `render()` no longer reads named files in full and injects them into the semi-stable segment. The body carries only a "where to read" hook for the model. A skill = one message (body + hook), not "a body message + one inline message per resource." A skill that names no resources renders byte-identically to before.

**The hook = one line with the absolute base directory; the resource manifest is discarded.** `render()` inserts a line before the skill body: `Base directory for this skill: <source_path.parent>` (early versions used a relative-path manifest; that whole manifest was later removed and replaced by this base-directory line). The model concatenates this line with a relative link in the body like `references/foo.md` into an absolute path and feeds it to `read`. The path string is rendered verbatim — no `resolve()`, no disk read (re-indexing the same tree still yields the same bytes). A synthetic skill with an empty `source_path` renders only the body, without a base-directory line.

**The generic `read`'s fence is loosened to the skill root; the dedicated tool is retired.** `ReadFileTool` gains an internal field `skill_roots` (not in `input_schema`, so the tool schema / stable hash is unchanged, the rendered prompt bytes stay equal, the stable-prefix prompt cache still holds, and resume rebuilds the same tool set), injected at assembly time by `resolve_skill_roots(registry)` (the absolute realpath of each skill root that has a `source_path`). When a path escapes the workspace, `resolve_readable` takes the **absolute** target and re-checks it against the skill-root allowlist, allowing it through on a match. The loosening applies **only to `read`** (write-side tools still hold a single-root hard wall) and **only to absolute paths** (relative paths still resolve within the workspace). The early `read_skill_resource` tool, together with `resolve_skill_resources` / `build_skill_resource_wiring`, is deleted entirely.

**The containment check is still realpath-based.** The target is passed through `os.path.realpath` before being compared against the skill roots, so a symlink that sits under a skill root but points outside it is still rejected (symlink-escape protection is preserved). Binary / over-budget files are handled by `read`'s existing behavior (non-UTF-8 errors, over-inline-budget offloads to an artifact + summary), which is friendlier than the dedicated tool's "hard reject reads over 64KB."

### Trade-off (must be stated plainly)

**Absolute paths entering the prompt → stable-byte rendering is bound to the same set of skill paths.** The reason absolute paths weren't emitted early on was precisely to keep the rendered bytes identical across machines and paths. The final form reverses this: the base-directory line contains `/Users/.../...`, so on a single machine the path is constant and the rendered bytes are stable (the stable-prefix prompt cache still holds, and resume folds the event log back to the same prompt); only folding the event log on another machine or another path would drift. For noeta-agent's single-user, single-machine positioning, this scenario basically doesn't occur, so the cost is acceptable. `SkillDescription.source_path` now **enters** the rendered bytes (the early "source_path doesn't enter the bytes" invariant is explicitly reversed here); any session that has activated a skill must fold against the same skill directory path to reproduce the same prompt.

### Red lines (do not break)

- **The renderer must not touch the disk**: it renders only the `source_path.parent` path string and never reads resource bytes (otherwise the rendered bytes would vary with disk content, breaking the stable-prefix prompt cache and fold/resume reproducibility).
- **The loosening is only for `read`, only for absolute paths, only to the skill root**: write-side tools (`write` / `edit` / `apply_patch`) never touch `skill_roots`; a relative path never lands at a skill root; the realpath containment check must not be removed.
- **`run_skill_script` is unaffected**: script execution still goes through its own opt-in + approval path; this decision changes only the "read" side. Reading a `.sh` yields its source text, not its run output.

## Rationale

- **Eager loading breaks the third layer of progressive disclosure.** Claude Code's style is "the body mentions a file → the model reads it when needed." noeta had at one point become "the body mentions a file → eagerly load all its contents into the context regardless of whether this turn uses it" — activating a resource-heavy skill would stuff all its named resources into the context at once.
- **The motive for the early dedicated tool was right, but testing exposed two UX problems.** (1) The model couldn't self-correct after hitting a wall: when a file didn't exist, the tool only returned a vague `'skill'/'references/foo.md' is not a discovered skill resource`, which couldn't distinguish "skill not discovered / wrong path / file doesn't exist" and led to repeated trial and error. (2) It was one more narrow tool the model had to learn, and `SKILL.md` itself could never be read (it was excluded from the resource allowlist).
- **Retiring it aligns with Claude Code.** Claude Code has no dedicated skill-resource tool; referenced files are all read with the generic `Read`, relying on (1) injecting one absolute base-directory line next to `SKILL.md` and (2) `Read` having no hard sandbox. noeta grew that extra tool because it built a single-root hard wall for `read` plus the "rendered bytes identical across machines" invariant — neither of which is necessary for the single-user, single-machine positioning. After retirement, the error reverts to the standard "file doesn't exist / not a file," and the model can self-correct.

## Alternatives considered

1. **Eagerly load named files into the context on activation (the old deviation this decision corrects).** Rejected: it violates progressive disclosure, bloats the context, and the renderer reading the disk breaks determinism.
2. **List every file under the skill directory (an early form).** Rejected: it would stuff `.DS_Store` / internal helper files into the prompt; naming in the body = the author's explicit intent, which is a tighter scope.
3. **Keep the dedicated `read_skill_resource`.** Rejected: on the single-machine positioning, its only real value (machine-independent addressing) doesn't apply, yet it costs a vague error + one more tool for the model to learn.
4. **Base-directory line: relative for in-workspace skills, absolute for out-of-workspace ones.** Rejected: it would wire the workspace root into the renderer and produce a mix of relative/absolute paths, deviating from Claude Code's "uniformly absolute" and adding coupling to the renderer. Uniformly absolute is the simplest.

## Consequences

- The landing points are `noeta.context.skills.indexer` (`render()` emits the base-directory line, doesn't read the disk), `noeta.tools.fs.read` / `noeta.tools.fs._workspace` (`ReadFileTool.skill_roots` internal field; `resolve_readable` takes the absolute target and re-checks it against the skill-root allowlist).
- The assembly-time injection lands in `noeta.execution.skills` (`resolve_skill_roots`; the early `resolve_skill_resources` / `build_skill_resource_wiring` are deleted).
- Costs and watch-outs: `source_path` enters the rendered bytes, so a session that has activated a skill must fold against the same skill directory path to reproduce the prompt; the red lines (the renderer doesn't touch the disk, the loosening is only for read/absolute paths/skill root, the realpath containment check is preserved, `run_skill_script` is unaffected) must all be held when changing the related code.
