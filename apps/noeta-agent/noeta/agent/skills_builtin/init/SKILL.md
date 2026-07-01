---
name: init
description: Survey the codebase and create or refresh a concise CONTEXT.md plus project docs
argument-hint: [extra focus, e.g. "only the runtime package"]
---

# Init project docs

Generate or refresh the repository's `CONTEXT.md` so future agents can be productive
fast. Keep it concise — only include what an agent would get wrong without it.

Extra instructions from the user: $ARGUMENTS

## Steps

1. Survey the project. Use `read` on the manifests you find with `grep`/`shell_run`:
   `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `README.md`, `Makefile`,
   CI config, any existing `CONTEXT.md`, `CLAUDE.md`, `AGENTS.md`, `.cursor/rules`,
   `docs/adr/`. Run `shell_run` for `git status` context if useful.
2. Detect and record only the non-obvious facts:
   - Build / test / lint commands that an agent cannot guess (custom scripts, flags,
     how to run a single test).
   - Languages, frameworks, package manager, monorepo / workspace layout.
   - Code-style rules that differ from language defaults.
   - Required env vars, setup steps, gotchas, key architectural decisions.
3. Read any existing `CONTEXT.md` first. Do NOT silently overwrite it — propose
   concrete diffs and explain why each change helps. For a fresh file, write a minimal
   one with `write_file`; for an existing file, use `replace_text` / `apply_patch` for
   targeted edits.
4. Exclude: file-by-file structure (discoverable by reading code), standard language
   conventions, generic advice ("write clean code"), and long references — link those
   with a path instead of inlining.

Every line must pass the test: "Would removing this make an agent make mistakes?"
If not, cut it. Do not invent sections like "Tips for Development" — only write facts
you actually found in files you read.

(fork agent: general-purpose)
