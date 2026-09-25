# Benchmarks

Noeta is a runtime, so we score an agent built on it —
[`noeta-agent`](https://github.com/initxy/noeta-agent), `main` preset, assembled only
from the public SDK — on the field's public benchmarks, same harness, same verifiers.

## Headline

| Benchmark | Scope | `noeta-agent` `main` (Claude Opus 4.8) | Field (public leaderboard) |
|-----------|-------|----------------------------------------|----------------------------|
| Terminal-Bench 2.1 | 40-task stratified sample | **82.5%** (33/40) | full-set board spans 58.7%–83.8% |
| SWE-bench Verified | 15-instance subset | **86.7%** (13/15) | field top ~79%, mid-pack ~66–77% |

On Terminal-Bench 2.1 the sample lands in the top band of the full-set leaderboard, just
under Claude Code + Fable 5 (83.8%) and Codex + GPT-5.5 (83.1%), above every listed
Claude Code on Opus/Sonnet and every Terminus 2 entry. Both runs use `Claude Opus 4.8`
(Terminal-Bench at `xhigh`, SWE-bench at `high`).

::: warning These are samples
A placement in the field's band, not full-set leaderboard entries. See
[what this does not claim](#what-this-does-not-claim).
:::

## Method

Runs go through [harbor](https://github.com/harbor-framework/harbor), the official
Terminal-Bench harness behind the public leaderboard. The agent is a harbor
`BaseInstalledAgent` (the same base class as harbor's `pi`, `codex`, `claude-code` and
`terminus` agents), installed into each task's container, driven headless by
`noeta-agent`'s `noeta run` CLI, and scored by each task's own verifier — the agent never
scores itself. Datasets are the official registry ones, pinned by digest. Adapter, setup
and commands live in
[`bench/`](https://github.com/initxy/noeta-agent/tree/main/bench).

### Terminal-Bench 2.1 (40-task stratified sample)

| Setting | Value |
|---|---|
| Harness | harbor 0.20.0, `terminal-bench/terminal-bench-2-1` @ `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a` |
| Agent | `noeta-agent` 0.6.0 (`main` preset), `noeta-sdk` / `noeta-runtime` ≥ 0.6.10 |
| Model | `opus4.8`, reasoning effort `xhigh` |
| Command | `NOETA_MODEL=opus4.8 NOETA_EFFORT=xhigh bench/run_benchmark.sh tb21-sample40` (task ids pinned in `TB_SAMPLE40`) |
| Date | 2026-08-10 |
| Result | **33/40 = 82.5%** — easy 4/4 (100%), medium 20/24 (83%), hard 9/12 (75%) |

A task counts as resolved when its harbor verifier reports `X passed, 0 failed`, not when
the agent process exits cleanly. The 7 misses each carry a real `N failed`:
`build-cython-ext`, `chess-best-move`, `count-dataset-tokens`, `dna-assembly`,
`protein-assembly`, `raman-fitting`, `video-processing`.

**Coverage.** Of the 89 tasks, 11 are left out for environment reasons, not capability:

- 7 ship a base image with Python < 3.12 (five `python:3.10` / `3.11`, two `qemu-*` on
  `debian:bullseye` = 3.9), below `noeta-agent`'s 3.12 floor. The adapter can provision
  3.12 with `uv` (the SWE-bench run does); they stay out only to keep this sample fixed.
- 4 cannot be scored in a bounded run: `make-mips-interpreter`, `make-doom-for-mips`,
  `install-windows-3-11` (multi-hour timeouts) and `polyglot-rust-c` (tagged
  `no-verified-solution` — the reference solution fails its own verifier).

The 40-task sample is drawn from the remaining 78, stratified by difficulty
(4 easy / 24 medium / 12 hard).

### SWE-bench Verified (15-instance subset)

| Setting | Value |
|---|---|
| Harness | harbor, `swe-bench/swe-bench-verified` (each instance is a harbor task `swe-bench/<instance_id>`) |
| Model | `opus4.8`, reasoning effort `high` |
| Subset | fixed 15 instances, one or two per repo across all 12 Verified repos (not skewed to django's 231 rows); pinned in `SWE_SUBSET15` |
| Command | `bench/run_benchmark.sh swe-15` |
| Date | 2026-08-09 |
| Result | **13/15 = 86.7%**; misses `django-11820`, `requests-1724` |

Verified images ship Python 3.9–3.11, so the adapter provisions a private 3.12 with `uv`.

```
django__django-10097   django__django-11820   django__django-13195
sympy__sympy-11618     sympy__sympy-13877     sphinx-doc__sphinx-10323
matplotlib__matplotlib-13989   scikit-learn__scikit-learn-10297
pydata__xarray-3095    astropy__astropy-12907   pytest-dev__pytest-10051
pylint-dev__pylint-4551   psf__requests-1724   pallets__flask-5014
mwaskom__seaborn-3069
```

### The field

Full-set scores from the official
[Terminal-Bench 2.1 leaderboard](https://www.tbench.ai/leaderboard/terminal-bench/2.1)
(17 entries, abridged to shipping CLIs and the reference agent); SWE-bench Verified from
[swebench.com](https://www.swebench.com/). Context only — not directly comparable to a
sample.

| Rank | Agent | Model | Effort | Terminal-Bench 2.1 |
|------|-------|-------|--------|--------------------|
| 1 | Claude Code | Fable 5 | xhigh | 83.8% ± 1.2% |
| 2 | Codex | GPT-5.5 | xhigh | 83.1% ± 1.1% |
| 3 | Terminus 2 | Fable 5 | high | 80.4% ± 1.2% |
| 5 | Claude Code | Opus 4.8 | high | 78.9% ± 1.3% |
| 7 | Terminus 2 | GPT-5.5 | xhigh | 78.0% ± 1.2% |
| 10 | Claude Code | Sonnet 5 | high | 74.6% ± 1.6% |
| 12 | Claude Code | Opus 4.7 | max | 68.9% ± 1.4% |
| 13 | Terminus 2 | Opus 4.7 | max | 66.1% ± 1.4% |
| 14 | Gemini CLI | Gemini 3 Pro | high | 65.8% ± 1.4% |
| 17 | Claude Code | GLM-5.1 | max | 58.7% ± 1.2% |

### Reproducing

Each number carries the model id, the pinned dataset, a copy-paste command and, for
SWE-bench, the full instance list. Docker, proxy, wheel and model-catalog setup are in
[`bench/README.md`](https://github.com/initxy/noeta-agent/blob/main/bench/README.md).
Dollar cost is not reported: the gateway's model id is not in the SDK's pricing catalog,
so only token totals exist. Benchmarks are not part of `make check` — they need Docker,
the `harbor` CLI, real credentials and real tokens.

## What this does not claim

- Not a full Terminal-Bench 2.1 (89) score — the 2026-08-10 row is a 40-task stratified
  sample, labelled as such.
- Not a full SWE-bench Verified (500) score — a fixed subset.
- Not a ranked leaderboard position — the 82.5% is a sample, placed in the field's band
  for context, not a head-to-head on identical tasks.
- Not a claim about *your* agent. The score belongs to one preset on one model; what the
  runtime contributes is the machinery underneath it, not the prompt.

## Next

- [Quickstart](start/quickstart.md)
- [Presets](reference/presets.md)
- [Why Noeta](why-noeta.md)
