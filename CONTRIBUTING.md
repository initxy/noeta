# Contributing

Noeta is a small, AI-native agent runtime — its primary user is "the agent that
reads and edits the code." The contribution flow is deliberately lightweight.

## Read AGENTS.md first

Before changing code, read the root [`AGENTS.md`](AGENTS.md). It holds the
project's working conventions: how to communicate, the language rules for docs
and terminology, and the engineering constraints (prefer existing patterns,
favor deep modules behind small interfaces, don't introduce a seam without a
real need to substitute the implementation, run verification matched to the risk
of the change).

Claude Code users: the root [`CLAUDE.md`](CLAUDE.md) imports the same
conventions via `@AGENTS.md`.

## Hard rules

Three rules are load-bearing. A change that pushes against one should stop and
read the matching decision file first:

- **Provider-neutral** — every external provider (LLM / storage / observability)
  enters through an adapter implementing a Noeta-shape Protocol. No vendor's
  wire vocabulary becomes the internal contract
  ([`docs/adr/provider-neutral.md`](docs/adr/provider-neutral.md)).
- **Runtime / SDK boundary** — `noeta-runtime` is the pure engine; `noeta.sdk`
  is the only public surface, and it forwards in-process without exposing engine
  internals
  ([`docs/adr/library-sdk-architecture.md`](docs/adr/library-sdk-architecture.md)).
- **Nothing statically imports `noeta.builtins`** — every official capability is
  a built-in plugin, reachable only through the plugin loader's dynamic `ref`
  resolution. `lint-imports` fails on a static import
  ([`docs/adr/package-layout.md`](docs/adr/package-layout.md)).

## Architecture decisions

Cross-module architectural trade-offs live as decision files under
[`docs/adr/`](docs/adr/). Each one describes the live design: what the system
does, the invariant that shape protects, and which alternatives were weighed and
rejected. Read the relevant file before changing a subsystem — the rationale is
the part the code cannot tell you. When a change moves a decision, update
`docs/adr/` and the glossary [`CONTEXT.md`](CONTEXT.md) in lockstep. Term
definitions belong in [`CONTEXT.md`](CONTEXT.md); the file format and the index
of decisions are in [`docs/adr/index.md`](docs/adr/index.md).

## Verify with `make check`

`make check` is the local gate. It mirrors
[`.github/workflows/ci.yml`](.github/workflows/ci.yml), minus what needs CI
infrastructure:

```bash
make install   # uv sync: workspace toolchain, kernel + dev group
make check     # the gate
```

`make check` is four steps, and any one of them failing fails the gate:

1. `pytest -n auto` with coverage over `noeta`, failing under 85%.
2. `mypy --strict` on `packages/noeta-runtime/noeta/protocols`.
3. `scripts/lint-naming.py` — the banned class names.
4. `lint-imports --config .importlinter` — the import topology contracts.

Two other targets exist for tighter loops: `make test` runs the suite with
coverage but no threshold, and `make lint` runs the static checks only — `ruff`
plus the naming and import-topology lints, no tests.

Two CI steps have no local equivalent — don't chase them:

- **Postgres storage contract tests.** CI runs the pytest step with
  `NOETA_TEST_POSTGRES_DSN` pointing at a Postgres service, which enables the
  `postgres` parameter of the storage contract suites. Without that variable
  those parameters skip.
- **Fresh-venv install smoke.** A separate CI job builds both wheels into a
  clean virtualenv on Linux and macOS and imports them
  (`pytest -m install_smoke tests/test_install_smoke.py`).

The SDK examples under [`examples/`](examples/) are covered by
`tests/test_examples_smoke.py`: each one must import cleanly, expose a `run()`
entrypoint, and name what it demonstrates in its module docstring (the literal
phrase `Demonstrated SDK capability`). Keep them working when you change the
SDK's public surface.

## AI-assisted contributions

Noeta is an AI-native project; contributions written with or by agents are
first-class and welcome. Two requirements keep that workable:

- **A human owner.** Every PR has a person who has read the change, understands
  it, and can answer review questions about it. "The agent wrote it" is not an
  answer.
- **Verification evidence.** The PR shows its `make check` result and notes
  anything that couldn't be verified and why (see the PR template).

There is no disclosure requirement — how a change was produced matters less
than whether someone can stand behind it.
