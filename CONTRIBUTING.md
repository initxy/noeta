# Contributing

noeta is written by people and coding agents together; the working agreement both follow is
[`AGENTS.md`](AGENTS.md). Read it first — it is short, needs no particular tool, and says how
work is shaped, specified, verified and reviewed. This page is the part a first-time
contributor needs on top.

## Set up and verify

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
make install   # uv sync --all-extras: the package, every extra, the dev tools
make check     # the gate; CI's test job runs this same target
```

`make check` runs, and fails on any of:

1. `make lint`: `ruff`, `mypy --strict` (settings in `pyproject.toml`), `scripts/lint-naming.py`
   (banned names, see `CONTEXT.md`), `scripts/lint-layout.py` (where code goes) and
   `lint-imports` (the layer contracts in `pyproject.toml` `[tool.importlinter]`).
2. `pytest -n auto` with coverage over `noeta`, failing under 85%.
3. `make example`: `examples/hello-plugin` installed as its own package, and the single-file
   example plugins, each with its tests.
4. `scripts/check-docs.sh`: doc hygiene — spec statuses, ADR status lines, the `AGENTS.md` /
   `CONTEXT.md` budgets, code paths and `docs/` links that no longer resolve.

Run `make check` unpiped (or redirected to a file): a pipe can hide the coverage gate's output.

Other targets, never part of `check`:

| Target | What it does | Needs |
|---|---|---|
| `make test` / `make lint` | the suite with coverage but no threshold / the static checks only | — |
| `make e2e` | `-m live` tests against a real model (providers, ACP) | `NOETA_LIVE_*` in `.env` (see `.env.example`) |
| `make eval` | prompt-regression scenarios against a real model, compared to `evals/baseline.json` (`make eval-baseline` re-records it) | same |
| `make parity` | noeta replayed through Claude Code's recorded scenarios; every request difference in `parity/report.md` (`scripts/parity/README.md`) | the recordings |
| `make cluster` | worker processes on one Postgres under random SIGKILL / SIGSTOP (`tests/cluster`) | docker, or `NOETA_TEST_POSTGRES_DSN` |
| `make build` | wheel and sdist into `dist/` | — |

CI adds what needs infrastructure: the Postgres parameters of the store contract tests
(`NOETA_TEST_POSTGRES_DSN`), Python 3.11 / 3.12 / 3.13, and a `cluster` job.

A change to model-facing text (prompts, tool descriptions, reminders) or provider wire behavior
also runs `make eval` and `make parity`, and the relevant `make e2e` tests: `make check` cannot
judge those.

## Where code goes

The package is layered and imports only downward: `noeta/cli` > `noeta/agents` >
`noeta/plugins/<name>` | `noeta/backends` | `noeta/host` > `noeta/toolkit` > `noeta/core`.
The kernel (`core`) carries no capability; a plugin imports only `core` and `toolkit`; two
plugins never import each other; front ends import no plugin. `CONTEXT.md` ("Layers", "Where
code goes") is the rule, and the lints enforce it. [How it works](docs/how-it-works/index.md)
on the docs site explains the design.

## How a change flows

- **Small, clear changes** (a bug fix with obvious expected behavior, a change inside one
  module): implement directly, with a test that fails before; the why goes in the commit
  message.
- **Anything with an open decision**: open an issue first. Once the approach is agreed it
  becomes a spec in `docs/specs/<YYYY-MM-DD>-<slug>.md` (format in
  [`docs/specs/TEMPLATE.md`](docs/specs/TEMPLATE.md)), implemented against its Plan, with
  progress and decision changes logged in it. `AGENTS.md` ("Task flow") has the details.
- **Decisions that are costly to reverse** are distilled into [`docs/adr/`](docs/adr/index.md)
  when the work merges; changing one is a new ADR that refines or supersedes it.
- **Structure and behavior never share a commit.** A refactor (moving, splitting, renaming)
  changes no behavior and says so; a behavior change comes with its test. Mixing them makes
  both unreviewable.
- **One fact, one home.** Docs link instead of repeating each other; when code and docs
  disagree, the code is right and the doc is fixed in the same change.

Everything committed is English, except the docs site's Chinese pages under `docs/zh/` (`AGENTS.md`, Language). Reviews follow [`docs/reviewing.md`](docs/reviewing.md).

## AI-assisted contributions

Contributions written with or by agents are welcome. Two requirements keep that workable:

- **A human owner.** Every pull request has a person who has read the change, understands it
  and can answer review questions about it.
- **Verification evidence.** The pull request shows its `make check` result and says what could
  not be verified and why.

Submissions that show no understanding of the change are closed without detailed review.

## Conduct and security

Participation follows the [Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities
privately as [SECURITY.md](SECURITY.md) describes, never in a public issue.
