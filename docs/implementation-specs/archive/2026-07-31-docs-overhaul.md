# Docs & diagrams overhaul — implementation spec

Status: **Shipped** (2026-07-31). All ten diagrams rebuilt code-verified; site
nav/sidebar/home reworked; quickstart added; concepts rewritten; sdk/plugins/
architecture split; README (en+zh) rebuilt around three principle diagrams;
full zh mirrors including the two previously missing how-tos. Gates at ship:
`npm run docs:build` green, `pytest tests/test_docs_codeblocks.py` 7/7,
`make check` green. Accepted deviations from AC3: `reference/glossary.md`
stays a single page by design (633 lines, grouped + A–Z index); four
reference pages sit at 251–317 lines, within the "~250" latitude.

## Diagnosis

1. **Diagrams drifted and under-deliver.** The five archify diagrams under
   `docs/assets/diagrams/` no longer match the code (module lists, sublabels,
   emphasis), three stray hand-drawn SVGs (`docs/assets/architecture.svg`,
   `task-lifecycle.svg`, `turn-sequence.svg`) duplicate them inconsistently,
   and most doc pages have no diagram at all. `docs/assets/crash-resume.gif`
   is referenced nowhere.
2. **The site hides its own content.** The VitePress nav shows only
   Guide/API/GitHub; sidebars are path-scoped, so How-to, Concepts,
   Operations, and Architecture are undiscoverable from the landing page and
   from each other. The home page has feature cards but no doc map.
3. **Walls of text.** `reference/glossary.md` (761 lines),
   `reference/plugins.md` (506), `reference/sdk.md` (465),
   `architecture/overview.md` (314) are single monolithic pages.
4. **Prose assumes the reader already knows Noeta.** Pages open with insider
   vocabulary (fold, Policy, Decision, envelope) before defining it, have few
   examples, and no progressive path from "never seen this" to "expert".
5. **No fast on-ramp.** `tutorials/first-agent.md` mixes quickstart and
   tutorial concerns; there is no 5-minute page. (`tests/test_docs_codeblocks.py`
   already lists `docs/tutorials/quickstart.md` as a runnable-snippet page —
   the hook exists, the page doesn't.)

## Goals / acceptance criteria

- **AC1** Every diagram is regenerated from a code-verified archify JSON;
  `node bin/archify.mjs validate` and `check` pass; SVGs are dual-theme
  (exported via `scripts/export-diagram-svg.mjs`); no stray/duplicate diagram
  assets remain.
- **AC2** Every section and page is reachable from (a) the top nav and (b) a
  global sidebar shown on all pages, in both locales; the home page carries a
  full doc map. `npm run docs:build` passes (dead-link check on).
- **AC3** No published page exceeds ~250 lines; big pages become a hub + focused
  subpages at URL-stable paths (hubs keep their existing file name; subpages are
  flat siblings).
- **AC4** Every concepts page opens with a plain-language paragraph + a diagram;
  reference pages carry runnable-style examples; how-to pages are single-task.
- **AC5** README (en+zh) is restructured for onboarding with ≥3 diagrams and a
  60-second quickstart; `pytest tests/test_docs_codeblocks.py` stays green.
- **AC6** Chinese mirrors exist for every published page (including previously
  missing `zh/how-to/use-sandbox.md`, `zh/how-to/docker-deployment.md`) and the
  zh nav/sidebar matches en.

## Information architecture (final file map, en)

```
docs/index.md                          home: hero + 6 features + quickstart + doc map
docs/tutorials/quickstart.md           NEW  5-minute offline start (runnable smoke blocks)
docs/tutorials/first-agent.md          rewrite: real agent w/ custom tool + permissions
docs/tutorials/ci-integration.md       polish
docs/how-to/{10 existing pages}        polish: task-focused, verified snippets
docs/concepts/index.md                 NEW  concepts hub: reading order + one-liners
docs/concepts/{8 existing pages}       rewrite, each embeds its diagram
docs/architecture/overview.md          hub: the tour, trimmed
docs/architecture/packages.md          NEW  split: two packages, namespace, import rules
docs/architecture/state-and-writers.md NEW  split: slices/single-writer/versioned fold
docs/architecture/extension-planes.md  NEW  split: 16 surfaces / 3 planes / builtins
docs/reference/sdk.md                  hub: import surface map + links
docs/reference/sdk-client.md           NEW  split: query / Client / QueryResult / WorkerLoop pointer
docs/reference/sdk-options.md          NEW  split: Options fields, permission modes
docs/reference/sdk-types.md            NEW  split: events, blocks, results, testing doubles
docs/reference/plugins.md              hub: what a plugin is + links
docs/reference/plugin-manifest.md      NEW  split: manifest shape, loading, versioning
docs/reference/plugin-surfaces.md      NEW  split: all 16 surfaces, one section each
docs/reference/{tools,presets,worker-loop,comparison}.md  polish
docs/reference/glossary.md             restructure: grouped by domain + top A–Z index
docs/operations/{troubleshooting,limitations}.md          polish
```

zh mirrors: same tree under `docs/zh/` (mirror every page above).

URL stability rule: never rename/move an existing page; splits keep the old
file as the hub. New pages are flat siblings (e.g. `reference/sdk-options.md`).

## Diagram plan

All live in `docs/assets/diagrams/` as `<name>.<mode>.json` + `<name>.html` +
`<name>.svg`. SVG filenames are the embedding contract — writers embed these
paths without waiting for the render. Every label must be verified against the
code (`packages/noeta-runtime/noeta/*`, `packages/noeta-sdk/noeta/*`); the
current runtime top level is: `agent context core execution observers policies
protocols read_models runtime storage testing tools`, and `noeta/builtins/` has
18 entries. Do not copy module lists from the old JSONs — they drifted.

| SVG | Mode | Story (one main path) | Embedded in |
|---|---|---|---|
| `architecture.svg` | architecture | Your code → `noeta.sdk` → Engine → storage backends; builtins reach the kernel only through the plugin loader (security boundary) | README, architecture/overview |
| `event-sourcing.svg` | dataflow | events appended → EventLog (+ContentStore for >4KB bodies) → fold → state slices → next decision | README, concepts/event-sourcing |
| `task-lifecycle.svg` | lifecycle | pending → running → suspended (human/timer/subtask/external) → terminal (completed/failed/canceled) | concepts/task-model, concepts/wake-resume |
| `engine-execution.workflow → engine-execution.svg` | workflow | one step: lease → fold → compose → LLM decide → dispatch tools → append events → loop or finish | concepts/engine-execution |
| `turn-sequence.svg` | sequence | Host code → Client → Engine → Provider → Tool → EventLog for one turn, with the return path | concepts/engine-execution, tutorials/first-agent |
| `crash-resume.svg` | workflow | Worker A killed mid-step → lease expires → Worker B folds the log → seals attempt → resumes exactly-once | README, concepts/fold-and-snapshot |
| `wake-resume.svg` | workflow | task suspends (3 wait kinds) → wake event arrives → durable match → resume | concepts/wake-resume |
| `context-composer.svg` | dataflow | folded state → ThreeSegmentComposer (stable prefix / dynamic / tail) → provider request, cache-hit annotation | concepts/composer-and-cache |
| `provider-neutrality.svg` | architecture | Engine ↔ one LLM protocol ↔ three adapters (Anthropic / OpenAI-compat / Responses) behind the "kernel never imports a vendor" boundary | concepts/provider-neutrality, how-to/configure-provider |
| `plugin-system.svg` | architecture | manifest → loader (dynamic ref) → registry → per-agent activation; 3 planes × 16 surfaces | architecture/extension-planes, reference/plugins |

Delete after replacement: `docs/assets/architecture.svg`,
`docs/assets/task-lifecycle.svg`, `docs/assets/turn-sequence.svg`,
`docs/assets/crash-resume.gif` (unreferenced).

Toolchain: archify skill at `/home/xiyang.dai/.claude/skills/archify/`
(`SKILL.md` + per-renderer READMEs + `schemas/` + `examples/`). Render/validate
with `node bin/archify.mjs …`; export dual-theme SVG with
`node scripts/export-diagram-svg.mjs <html> <svg>` (repo script, already
tested against the previous export format).

## Writing style contract (applies to every rewritten page)

1. Open with 2–4 plain-language sentences: what this page covers, when you
   need it. No insider terms before they are introduced or linked.
2. Diagram (if the page has one) right after the intro.
3. Progressive depth: the common case first, mechanics second, edge cases last.
4. Short paragraphs (≤4 sentences); one idea per section; concrete code over
   abstract description; show expected output for anything runnable.
5. Every page ends with 2–4 "next" links (Diátaxis-adjacent: tutorial ↔ how-to
   ↔ concept ↔ reference).
6. Canonical terms per CONTEXT.md / glossary; identifiers and API names in
   backticks; English only (zh mirrors are translations, not rewrites).

## Guardrails (enforced by tests — do not break)

- README.md must keep ≥1 `<!-- runnable: smoke -->` python block that runs
  green offline (see `tests/test_docs_codeblocks.py`); same for any runnable
  blocks added to `docs/tutorials/quickstart.md`.
- Never write the bare-`noeta` install path (`uv add noeta`,
  `pip install noeta`, `pypi.org/project/noeta`) outside an "Out of scope"
  heading — the published dists are `noeta-sdk` / `noeta-runtime`.
- Wake semantics wording: durable, single-worker, exactly-once. Banned:
  "at-most-once wake", "lost wake", "wake event is lost", affirmative
  "operator re-issue".
- No `packages/noeta/README.md`.
- Naming: canonical `Task` / `root_task_id`; never introduce `Run` /
  `Workflow` / `Session` / `Mutator` nouns for these concepts.

## Work split

Wave 1 (parallel, disjoint file ownership):

| Agent | Owns (writes only these) |
|---|---|
| D1 diagrams-structure | `docs/assets/diagrams/{architecture,plugin-system,provider-neutrality,event-sourcing,context-composer}.*` + deleting the four stray assets |
| D2 diagrams-flow | `docs/assets/diagrams/{task-lifecycle,engine-execution,turn-sequence,crash-resume,wake-resume}.*` |
| S site | `docs/.vitepress/config.ts`, `docs/index.md`, `docs/zh/index.md` |
| C concepts | `docs/concepts/*` |
| R reference | `docs/reference/*` |
| H guides | `docs/tutorials/*`, `docs/how-to/*`, `docs/architecture/*`, `docs/operations/*` |
| M readme | `README.md`, `README.zh-CN.md` |

Wave 2 (after wave 1): zh mirrors for all changed pages (2 agents), then
integration: `npm run docs:build`, `pytest tests/test_docs_codeblocks.py`,
link/asset sweep, final review against this spec.

## Verification

- Per diagram: `archify validate` + `archify check` + export succeeds.
- Site: `npm run docs:build` green (dead links fail the build).
- Snippets: `uv run pytest tests/test_docs_codeblocks.py -q` green.
- Whole-repo: `make check` untouched by docs-only changes; run once at the end.
