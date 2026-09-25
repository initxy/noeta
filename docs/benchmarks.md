# Benchmarks

Noeta is a runtime, so we score an agent built on it —
[`noeta-agent`](https://github.com/initxy/noeta-agent), `main` preset, assembled only
from the public SDK — on the field's public benchmarks, same harness, same verifiers.

## Headline

| Benchmark | Scope | Measured on | Result | Field (public leaderboard, full set) |
|-----------|-------|-------------|--------|--------------------------------------|
| Terminal-Bench 2.1 | 40-task stratified sample | `noeta-sdk` 0.6.28, 2026-09-19, one run | first pass **24/40** (9 trials hit an infrastructure error); **33/40** best of up to three attempts per task | 58.7%–83.8% |
| SWE-bench Verified | 15-instance subset | `noeta-sdk` 0.6.10, 2026-08-09, one run; not re-measured since | **13/15** after re-running 4 setup timeouts; first pass 9/15 | not comparable to a 15-instance subset |

Both use Claude Opus 4.8 through a gateway (`model_hub/es1_orange_o48`),
Terminal-Bench at effort `xhigh`, SWE-bench at `high`.

::: warning A sample, measured once
Each number is one run over a sample, with no confidence interval. The
higher figure in each row counts re-runs of tasks the first pass did not
resolve; the re-runs are spelled out under [Method](#method). Together they
tell you roughly where the agent lands relative to the field, not its
leaderboard rank. See
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
| Agent | `noeta-agent` (`main` preset) on `noeta-sdk` 0.6.28 |
| Model | `model_hub/es1_orange_o48` (Claude Opus 4.8 via a gateway), reasoning effort `xhigh` |
| Command | `bench/run_benchmark.sh tb21-sample40` (task ids pinned in `TB_SAMPLE40`) |
| Date | 2026-09-19 |
| First pass | **24/40**; 9 trials ended in an infrastructure error (2 of them still passed their verifier) |
| Second attempt | all 16 unresolved tasks re-run: 7 more pass |
| Third attempt | 2 tasks re-run: both pass |
| Best of up to three attempts | **33/40** |

The 9 infrastructure errors on the first pass:

- **3 `llm_error` exits**, on tasks that hand the model an image. The provider
  answered 400 and the error body was not recorded, so the cause could not be
  read from the run; the SDK now keeps the body in the failure detail. Two of
  the three failed the same way when re-run.
- **6 harness timeouts** at the 900-second agent limit. One of them had already
  printed a finished answer when the harness killed it.

The re-runs were not limited to the errored tasks: of the 9 tasks the retries
recovered, 3 had errored and 6 had simply failed their verifier on the first
pass. Retrying only the errored tasks would give 27/40. Read 24/40 as the
first-try score and 33/40 as best-of-three, not as one clean run.

The same sample with Doubao at effort `xhigh` on the same day scored 29/40 on
its first pass, with 9 harness timeouts.

A task counts as resolved when its harbor verifier reports `X passed, 0 failed`, not when
the agent process exits cleanly.

**Earlier run (2026-08-10, `noeta-sdk` 0.6.10).** The figure quoted before this
page was updated — 33/40, easy 4/4, medium 20/24, hard 9/12 — came from the same
sample and model. Its job records show re-runs of unresolved tasks as well,
which the page did not say at the time; how the 33 split between first and
later attempts is not reconstructed here. Its 7 misses each
carried a real `N failed`: `build-cython-ext`, `chess-best-move`,
`count-dataset-tokens`, `dna-assembly`, `protein-assembly`, `raman-fitting`,
`video-processing`.

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
| Agent | `noeta-agent` 0.6.0 (`main` preset) on `noeta-sdk` 0.6.10 |
| Model | `opus4.8` (`model_hub/es1_orange_o48`), reasoning effort `high` |
| Subset | fixed 15 instances, one or two per repo across all 12 Verified repos (not skewed to django's 231 rows); pinned in `SWE_SUBSET15` |
| Command | `bench/run_benchmark.sh swe-15` |
| Date | 2026-08-09 |
| Result | **13/15**; misses `django-11820`, `requests-1724` |

The first pass resolved 9/15; 4 instances ended in a harness setup timeout
before the agent ran, and all 4 passed when re-run. It has not been measured
again since 0.6.10.
Fifteen instances are too few to compare with full-set scores, so the headline
does not place it against the leaderboard.

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
(17 entries, abridged to shipping CLIs and the reference agent); SWE-bench Verified
full-set results are at [swebench.com](https://www.swebench.com/). Context only — not
directly comparable to a sample.

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

- Not a full Terminal-Bench 2.1 (89) score — every row is a 40-task stratified sample,
  labelled as such.
- Not a full SWE-bench Verified (500) score — a fixed 15-instance subset.
- Not a statistic. Each result is a single run; there is no confidence interval, and a
  rerun of the same sample can move by several tasks.
- Not a first-try score, unless it says "first pass". The higher numbers count re-runs
  of tasks the first pass did not resolve.
- Not a ranked leaderboard position — the sample is placed in the field's band for
  context, not a head-to-head on identical tasks.
- Not a claim about *your* agent. The score belongs to one preset on one model; what the
  runtime contributes is the machinery underneath it, not the prompt.

## Next

- [Quickstart](start/quickstart.md)
- [Presets](reference/presets.md)
- [Why Noeta](why-noeta.md)
