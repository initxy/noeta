# Benchmarks

Noeta is a runtime, not a coding agent — so the honest way to measure it is to
score an agent *built on it* against the same public benchmarks the field runs,
on the same harness, judged by the same verifiers.

That agent is [`noeta-agent`](https://github.com/initxy/noeta-agent), the
reference product built on `noeta-sdk` / `noeta-runtime`. Its `main` preset is
assembled entirely from the public SDK surface — the tools, the policy, the
context composer, the event ledger on this site. Every number below therefore
exercises the runtime end to end: the composer that builds each prompt, the
tool dispatch path, the guards, the fold that rebuilds state between steps.

The runs go through [harbor](https://github.com/harbor-framework/harbor), the
official Terminal-Bench harness — the same one behind the public leaderboard.
The adapter, the harness setup, and the exact commands live in the
[`bench/`](https://github.com/initxy/noeta-agent/tree/main/bench) directory of
the `noeta-agent` repository.

## Headline

| Benchmark | Scope | `noeta-agent` `main` (Claude Opus 4.8) | Field (public leaderboard) |
|-----------|-------|----------------------------------------|----------------------------|
| Terminal-Bench 2.1 | 40-task stratified sample | **82.5%** (33/40) | full-set board spans 58.7%–83.8% |
| SWE-bench Verified | 15-instance subset | **86.7%** (13/15) | field top ~79%, mid-pack ~66–77% |

On Terminal-Bench 2.1 the agent resolves **82.5%** of the sample — the top band
of the full-set leaderboard (58.7%–83.8%), just under Claude Code + Fable 5
(83.8%) and Codex + GPT-5.5 (83.1%), above every listed Claude Code on
Opus/Sonnet and every Terminus 2 entry. On a 15-instance SWE-bench Verified
subset it resolves **13/15**. Both run `Claude Opus 4.8` (the terminal board at
`xhigh`, SWE-bench at `high`).

These are **samples**, labelled as such — a placement in the field's band, not
full-set leaderboard entries. See [What this does not claim](#what-this-does-not-claim).

## Terminal-Bench 2.1 (40-task stratified sample)

- **Harness:** harbor 0.20.0, dataset `terminal-bench/terminal-bench-2-1`
  @ `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`
- **Agent:** `noeta-agent` 0.6.0 (`main` preset), deps `noeta-sdk` /
  `noeta-runtime` ≥ 0.6.10
- **Model:** `opus4.8`, reasoning effort `xhigh`
- **Command:** `NOETA_MODEL=opus4.8 NOETA_EFFORT=xhigh bench/run_benchmark.sh
  tb21-sample40` (the 40 task ids are pinned in that script's `TB_SAMPLE40`
  array, so the sample is re-runnable verbatim)

| Date | Scope | Resolved |
|------|-------|----------|
| 2026-08-10 | 40-task stratified sample (4 easy / 24 medium / 12 hard) | **33/40 = 82.5%** |

Resolved by difficulty:

| Difficulty | Resolved |
|------------|----------|
| easy | 4/4 (100%) |
| medium | 20/24 (83%) |
| hard | 9/12 (75%) |

**How the score is read.** The verdict of record is each task's own harbor
verifier (`X passed, 0 failed`), not whether the agent *process* exited
cleanly. The 7 misses are genuine — each carries a real `N failed` in its
verifier output (`build-cython-ext`, `chess-best-move`, `count-dataset-tokens`,
`dna-assembly`, `protein-assembly`, `raman-fitting`, `video-processing`).

## SWE-bench Verified (fixed subset)

- **Harness:** harbor, dataset `swe-bench/swe-bench-verified` (the adapter
  converts Verified into harbor tasks; each is named `swe-bench/<instance_id>`)
- **Model:** `opus4.8`, reasoning effort `high`
- **Subset:** a fixed 15-instance set (not the full 500), one or two per repo
  across all 12 repos in Verified so the sample is not skewed to django's 231
  rows. Pinned in `run_benchmark.sh`'s `SWE_SUBSET15`.
- **Command:** `bench/run_benchmark.sh swe-15`

| Date | Subset size | Resolved |
|------|-------------|----------|
| 2026-08-09 | 15 | **13/15 = 86.7%** |

Two genuine misses (`django-11820`, `requests-1724`); the other 13 resolved.
SWE-bench Verified images ship Python 3.9–3.11, below `noeta-agent`'s 3.12
floor, so the adapter provisions a private 3.12 with `uv` before running.

Subset instance ids:

```
django__django-10097   django__django-11820   django__django-13195
sympy__sympy-11618     sympy__sympy-13877     sphinx-doc__sphinx-10323
matplotlib__matplotlib-13989   scikit-learn__scikit-learn-10297
pydata__xarray-3095    astropy__astropy-12907   pytest-dev__pytest-10051
pylint-dev__pylint-4551   psf__requests-1724   pallets__flask-5014
mwaskom__seaborn-3069
```

## What the harness covers

The agent runs on harbor exactly as the field's agents do — it is a
`BaseInstalledAgent` (the same base class as harbor's `pi`, `codex`,
`claude-code`, and `terminus` agents), installed into each task's container,
driven headless by the `noeta run` CLI, and scored by harbor's own per-task
verifier. Nothing about the scoring path is Noeta-specific.

- **Same datasets** — the official `terminal-bench/terminal-bench-2-1` and
  `swe-bench/swe-bench-verified` registry datasets, pinned by digest.
- **Same verifiers** — each task's own `test.sh` / verifier decides pass/fail;
  the agent never scores itself.
- **Same comparison surface** — Terminal-Bench 2.1's public leaderboard is the
  reference for "the field" below.

### Coverage and exclusions

The 89-task TB2.1 set has a handful of tasks this harness cannot score for
reasons that are environment, not capability — stated here rather than hidden:

- **7 tasks** ship a base image with Python < 3.12 (five `python:3.10` /
  `3.11` images, plus two `qemu-*` tasks on `debian:bullseye` = 3.9), below
  `noeta-agent`'s 3.12 floor. The adapter can provision a private 3.12 with
  `uv` (the SWE-bench run uses that path), so this is not a hard limit; they
  stay out of *this* sample only so its composition stays fixed.
- **4 tasks** are not scoreable in a bounded run: `make-mips-interpreter`,
  `make-doom-for-mips`, `install-windows-3-11` (multi-hour timeout black holes)
  and `polyglot-rust-c` (tagged `no-verified-solution` — even the reference
  solution fails its own verifier). Excluded.

The 40-task sample is drawn from the remaining 78, stratified by difficulty.

## The field (public leaderboard, for context)

Terminal-Bench 2.1 numbers from the official leaderboard
([tbench.ai](https://www.tbench.ai/leaderboard/terminal-bench/2.1)); SWE-bench
Verified from [swebench.com](https://www.swebench.com/). These are full-set
scores — cited for context, **not** directly comparable to a sample.

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

Source: [tbench.ai](https://www.tbench.ai/leaderboard/terminal-bench/2.1) (17
entries; abridged above to the shipping CLIs + reference agent). The board
spans **58.7%–83.8%**; the **82.5%** sample lands in the top band. Read it as
"in the top band of the shipping agents," not a ranked position on identical
tasks.

## Reproducibility

Every published number carries enough to re-run the identical evaluation: the
model id, the pinned dataset `name@version`, a copy-pasteable command, and —
for SWE-bench — the full instance-id list. The setup the harness needs (Docker,
proxy, wheel, model catalogue) is documented in
[`bench/README.md`](https://github.com/initxy/noeta-agent/blob/main/bench/README.md).

Cost is left unpriced: the run went through a gateway whose model id is not in
the SDK's pricing catalogue, so token totals are reported but dollar cost is
not fabricated.

Benchmark runs are deliberately **not** part of `make check`. They need Docker,
the `harbor` CLI, and real gateway credentials, and they spend real tokens — a
manual gate you run on a machine you control, not part of the automated suite.

## What this does not claim

- Not a full Terminal-Bench 2.1 (89) score — the 2026-08-10 row is a 40-task
  stratified sample, labelled as such.
- Not a full SWE-bench Verified (500) score — a fixed subset.
- Not a ranked leaderboard position — the 82.5% is a sample, placed in the
  field's band for context, not a head-to-head on identical tasks.
- Not a claim about *your* agent. The score belongs to one preset on one model;
  what the runtime contributes is the machinery underneath it, not the prompt.

## Next

- [Quickstart](tutorials/quickstart.md) — run an agent in five minutes
- [Presets](reference/presets.md) — what the built-in agents are wired with
- [Comparison](reference/comparison.md) — Noeta next to other agent frameworks
