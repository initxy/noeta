## Communication

- Keep answers concise and direct; lead with the conclusion and the next step.
- Don't add unrelated explanation; when unsure, state your assumptions and the risks.
- Don't narrate what you're about to do or recap what you just did; report only what changes what the reader does next.
- Conversation may happen in whatever language the other side uses; everything committed to the repo (see Language) is English.

## Language

- All repository artifacts are written in English: docs (`CONTEXT.md`, `docs/adr/`, `docs/specs/`), code comments, and identifiers. The only exceptions are the Chinese mirrors — `README.zh-CN.md`, `docs/zh/**`, `docs/releasing.zh.md`.
- Keep technical terms in their canonical English form and spell them consistently throughout: code identifiers (function / class / file / variable names), API / library / tool / command names, file paths, and fixed architecture terms (module, interface, seam, adapter, deep module, etc.). Don't invent alternate names or switch between synonyms.
- Write clear, idiomatic English prose — not word-for-word translation from another language.
- Pick one spelling per term and use it everywhere; don't mix variants.
- Naming is enforced by `scripts/lint-naming.py` (in `make check`); the banned names and the canonical `Task` / `root_task_id` are in `CONTEXT.md`'s `Naming:` entries.

## Context / ADR

- Read `CONTEXT.md` first whenever you touch domain concepts, system boundaries, or stable conventions. A package with its own `AGENTS.md` keeps its module-only concepts there.
- Read `docs/adr/` (start at `docs/adr/index.md`) first whenever a long-term architecture trade-off is involved; never rely on an ADR marked `superseded`, and read the ADRs listed under its `Refined by`.
- Code is the single source of truth for "what is." When docs and code disagree, trust the code and fix the docs.
- One fact, one home: never state the same fact in two docs — link instead. A change that invalidates a `CONTEXT.md` entry or ADR updates it in the same task.
- Write docs lean: a `CONTEXT.md` or `AGENTS.md` line exists only if code can't tell an agent the same thing quickly; point at code by repo path instead of restating it. An ADR only for a shipped decision that is costly to reverse; changing one is a new ADR that refines or supersedes it, never an amendment in place. `scripts/check-docs.sh` holds the budgets and formats — trim, don't raise them. When unsure, don't write it.
- Code comments carry only constraints the code can't show — never a `docs/specs/` reference (one-shot artifacts). Code needn't cite ADRs; where a comment names one it is plain prose, not a contract (`docs/adr/006-doc-code-link-direction.md`).
- Archived specs (`docs/specs/archive/`) are construction history — never read them as a description of the current system.

## Task flow

- Before starting, run `make check`. Red that isn't yours → report it before anything else.
- When the idea isn't clear yet, shape before code: ask one question at a time, each with a recommended answer and its reasoning; look up what code and docs can answer instead of asking. Converged = no open decision that would change the implementation path, boundary, or acceptance criteria. ("Just recommend" → answer straight, flagging the assumptions.)
- Shaping red lines: no implementation code and no spec before the design converges; a finished spec doesn't slide into implementation — starting is a separate decision.
- Record decisions in the spec's `Decisions` as they settle — not yet in `CONTEXT.md` or `docs/adr/`; those are distilled at review wrap-up from what survived. One-off details are not persisted.
- Write the spec per `docs/specs/TEMPLATE.md` at `docs/specs/<YYYY-MM-DD>-<slug>.md`: `active` if implementation starts now, `proposed` if queued. A terminal status (`done` / `superseded` / `dropped`) and the move into `docs/specs/archive/` are one action.
- A change with no open decision (a bug fix with clear expected behavior, a small change inside one module) needs no spec: implement directly and put the why in the commit message.
- Implement against the spec: flip it `active` (set `owner` when others may work in parallel); tick Plan tasks as they complete; log progress, decision changes, and friction in its Progress log. For large tasks with clean boundaries, split work across subagents while the main agent integrates and verifies.
- For architecture improvements, refactoring direction, and module design, start from the existing decisions (`docs/adr/`) and the engineering constraints below.
- done = every acceptance criterion met + all automated gates green. Both required.
- Before merge, review per `docs/reviewing.md`; merging archives the spec and distills long-term knowledge.
- Resume across sessions from the spec alone; write no separate handoff doc. A spec-less change that spans sessions leaves its state in the branch's commit messages.

## Verbs

- install: `make install` (uv sync; first time)
- test: `make test`
- lint: `make lint`
- build: `make build` (both packages' wheels and sdists into `dist/`)
- e2e: `make e2e` — `-m live` tests against a real LLM / sandbox container; needs credentials, never part of `check`
- docs: `make docs` (VitePress dev server; `npm ci` first)
- check: `make check` — ruff, pytest with coverage ≥ 85, mypy --strict on both packages, `scripts/lint-naming.py`, `lint-imports`, `scripts/check-docs.sh`
- release: tag push per `docs/releasing.md` — approval gate

## Gates

- Automated gates (required for done): `make check`. CI's test job runs the same target; CI-only on top: the Postgres parameters of the storage contract tests (`NOETA_TEST_POSTGRES_DSN`), the Python 3.11 / 3.12 / 3.13 matrix, and the fresh-venv install smoke (`pytest -m install_smoke`).
- `make check` can't judge model-facing prompt text or provider wire behavior. A change there also runs the relevant `make e2e` tests, and states that the benchmark was not rerun.
- Approval gates (the maintainer signs off before running): release, data migration or deletion on a host's store. Merging a change under `docs/` publishes the docs site (`docs.yml` deploys on push to `main`).

## Engineering constraints

- Feature work changes behavior; maintenance work changes structure. Never mix them in one diff.
- Prefer existing patterns in the repo, keep changes focused, and avoid unrelated refactors.
- Architecturally, prefer deep modules: a small interface hiding a substantial implementation.
- The interface is the test surface; don't introduce a seam without a real need to substitute the implementation.
- Respect the two structural rules the repo is built on: `noeta.sdk` is the sole public surface, and the kernel carries no capability implementation — nothing statically imports `noeta.builtins` (`CONTEXT.md`: Public surface, Microkernel rule).
- Name things for what they do, never a metaphor — in code, doc terms, and file slugs alike.
- Run verification matched to the risk; if you can't verify, say why.

## Release

- A merged behavior change to `packages/noeta-runtime` or `packages/noeta-sdk` should be followed by a release; the published packages must not lag `main`.
- Scope the release per package: bump only the ones whose source changed. One tag publishes both, but each publish job is gated on the tag version, so an unbumped package skips cleanly.
- Bumps are patch by default — minor/major is the maintainer's explicit call.
- Follow `docs/releasing.md`. Human sign-off = pushing the `vX.Y.Z` tag.

## Maintenance

- After a large feature merges, run a scoped gc over the changed area; periodically run a global gc — both per `docs/gc.md`.
- When `make check` prints `gc due`, finish the task first, then tell the user and offer a scoped gc.
- Every gc change leaves behavior unchanged; never mix gc into a feature diff.
