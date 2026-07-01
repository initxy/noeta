---
name: simplify
description: Clean up the changed code for reuse, simplification, and efficiency, then apply the fixes
argument-hint: [scope, e.g. a file path or "since main"]
---

# Simplify changes

Review the changed code purely for quality — reuse, simplification, and efficiency —
and apply the cleanups. This is NOT a bug hunt; do not change behavior.

Scope from the user (default to uncommitted changes if empty): $ARGUMENTS

## Steps

1. Read the change. Run `git_status` and `git_diff` to see what's in scope, then
   `read` the surrounding code for context.
2. Look for, in order of value:
   - **Reuse** — duplicated logic that an existing helper / function already covers.
     `grep` the codebase for an existing utility before writing anything new.
   - **Simplification** — dead code, redundant branches, needless intermediate
     variables, over-abstraction, comments that restate the code.
   - **Efficiency** — obvious redundant work (repeated I/O, re-computation in a loop,
     unnecessary allocations) where the cleaner form is also faster.
3. Apply each cleanup with `replace_text` or `apply_patch`, keeping diffs minimal and
   behavior identical. Match the surrounding code's existing style and conventions.
4. After editing, re-run `git_diff` to confirm the result is genuinely smaller / clearer,
   and run the project's tests with `shell_run` to confirm behavior is unchanged.

Only touch code that's already in scope — do not opportunistically rewrite unrelated
files. If a candidate cleanup would change behavior, skip it and note it instead of
applying it. End with a short summary of what you simplified and why.

(fork agent: main)
