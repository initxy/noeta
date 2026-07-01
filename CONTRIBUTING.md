# Contributing

Noeta is a small, AI-native agent runtime — its primary user is "the agent that
reads and edits the code." The contribution flow is intentionally lightweight.

## Read AGENTS.md first

Before changing code, read the root [`AGENTS.md`](AGENTS.md). It captures the
project's working conventions: how to communicate, the language rules for docs
and terminology, and the engineering constraints (prefer existing patterns,
favor deep modules behind small interfaces, don't introduce a seam without a
real need, run verification matched to the risk of the change).

Claude Code users: the root [`CLAUDE.md`](CLAUDE.md) imports the same
conventions via `@AGENTS.md`.

## Hard rules

Two rules are load-bearing — a change that breaks one should stop and read the
matching decision first:

- **Provider-neutral** — external providers (LLM / storage / observability) are
  adapted to Noeta-shape internal protocols; no single vendor's shape becomes the
  internal contract (`docs/adr/provider-neutral.md`).
- **Product / shell boundary** — the runtime engine stays free of
  product-specific assembly (`docs/adr/library-sdk-architecture.md`).

## Architecture decisions

Long-term architectural trade-offs live as decision files under
[`docs/adr/`](docs/adr/). Each spells out what was decided, why, and why the
alternatives were rejected (Chesterton's fence) — read the relevant one before
changing things so you don't re-walk an already-rejected path. When you change a
decision, update `docs/adr/` and the glossary [`CONTEXT.md`](CONTEXT.md) in
lockstep. Term definitions are in [`CONTEXT.md`](CONTEXT.md); the decision
format is in [`docs/adr/README.md`](docs/adr/README.md).

## Run the tests + lints after changes

```bash
uv sync
uv run pytest
uv run lint-imports --config .importlinter
uv run python scripts/lint-naming.py
```

Each SDK example (see [`examples/`](examples/)) ships with a smoke test; keep the
examples runnable when you change the SDK's public surface.
