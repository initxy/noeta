# Benchmarks

How noeta 1.0 compares with Claude Code on public coding benchmarks: what was run, the
results, what they do not show, and how to run them again. The full record (every run,
retries, the adapter, the Docker setup) is
[`bench/README.md`](https://github.com/initxy/noeta/blob/main/bench/README.md).

## Headline

noeta 1.0 (commit `34f8acee`) next to Claude Code 2.1.312 on the same model
(DeepSeek V4.1 Flash, effort `high`), same tasks, caps and machine, run through
[harbor](https://github.com/harbor-framework/harbor) between 2026-10-30 and 2026-11-01:

| Benchmark | noeta | Claude Code | noeta − Claude Code (SE) |
|---|---|---|---|
| SWE-rebench 07-2026, 111 tasks, 50 min, 2 runs each pooled | 50.5 % | 45.0 % | +5.4 (3.6) |
| same, 91 tasks without leaked answers | 51.1 % | 44.0 % | +7.1 (3.6) |
| same, one same-time run | 49.5 % | 42.3 % | +7.2 (3.8) |
| Terminal-Bench 4.0, 54 of 66 tasks, 2 h cap, 1 run | 3.7 % | 1.9 % | +1.9 (3.2) |

Cost per run of 111 SWE-rebench tasks: noeta $5.01–$5.77, Claude Code $5.70–$6.25. Per
Terminal-Bench run of 54 tasks: noeta $5.23, Claude Code $6.50. Costs are DeepSeek's list
price for DeepSeek-V4.1-Flash (read 2026-10-30), off-peak throughout.

The difference is the mean over tasks of noeta's pass rate minus Claude Code's, with its
standard error. One single run moves by about ±5 points (Claude Code scored 47.7 % in one
SWE-rebench run and 42.3 % in another), so read the pooled row.

## Why these benchmarks

| Board | Used | Why |
|---|---|---|
| SWE-rebench | yes, the July 2026 window (111 tasks) | fresh GitHub tasks after the model's training data, the same scaffold for every model, and cheap enough to run twice for both agents on one machine |
| Terminal-Bench 4.0 | yes, 54 of 66 tasks | the terminal-agent board's headline since 2.1 saturated; Claude Code is its reference agent |
| SWE-bench Verified | no | saturated (top scores near 97 %) |
| SWE-bench Pro | no | 731 large images per run is beyond one machine for two agents twice, and a fixed public set can leak into training data |

## Method

- **One machine** (16 cores, 62 GB, an NVIDIA T4 the GPU tasks could not use), harbor 0.25.0.
- **Both agents in one harbor job.** `bench/compare.py` interleaves their trials, so both see
  the same gateway load over time, and splits the concurrency evenly. Same model, effort,
  tasks, timeouts, and the same context window (1M) and output cap (65,536) for both.
- **Claude Code 2.1.312** is harbor's own `claude-code` agent with telemetry off and one
  addition: at the agent timeout its process tree inside the sandbox is stopped (harbor only
  cancels its `docker exec` client, so without this an agent keeps editing files while the
  verifier runs). noeta's adapter does the same.
- **noeta** is installed into its own virtual environment in each task container and run as
  `noeta -p "<instruction>" --output-format stream-json --verbose --permission-mode
  bypassPermissions --cwd "$(pwd)" --model <id> --effort <e>`, with a models file giving the
  model's real limits.
- **The request each agent sends** was recorded once through a local proxy: the same
  `max_tokens` (65536), `thinking` (`adaptive`), `output_config.effort` (`high`),
  `context_management`; neither sends `temperature` or `top_p`. noeta makes one extra call,
  a conversation-title call before the first turn.
- **Scoring:** one graded attempt per task per run (k=1). A trial that hit the agent timeout is
  graded; one that ended without a grade (install failure, unresolved container, network) was
  retried by harbor up to three times, which gives the agent a second attempt. A trial that
  never got a grade counts as a fail.
- **Tokens** come from noeta's own store (sub-agents and compaction included) and from Claude
  Code's session files (sub-agents included).
- **Nothing is uploaded.** harbor's telemetry is off, datasets are read from local copies, and
  the gateway key is redacted from every job file.

## Results

### SWE-rebench 07-2026 (111 tasks, 50 min cap)

All 111 tasks of `ibragim-badertdinov/swe-rebench-07-2026` at the dataset's own 50-minute agent
cap, 16 trials at a time.

| Run | Agent (noeta commit) | Passed | 50-min timeouts | Tokens in / cached / out | Cost |
|---|---|---|---|---|---|
| 1 | noeta `54121930` | 50 (45.0 %) | 59 | 326M / 317M / 5.4M | $5.58 |
| | Claude Code | 53 (47.7 %) | 63 | 381M / 370M / 6.0M | $6.25 |
| fix | noeta `34f8acee` | 57 (51.4 %) | 43 | 390M / 381M / 5.5M | $5.77 |
| r2 | noeta `34f8acee` | 55 (49.5 %) | 49 | 300M / 292M / 4.9M | $5.01 |
| | Claude Code | 47 (42.3 %) | 66 | 319M / 309M / 5.6M | $5.70 |

Paired differences per run: run 1 (noeta `54121930`) −2.7 (SE 4.3); noeta `34f8acee` of run
"fix" against run 1's Claude Code +3.6 (SE 5.1); r2 +7.2 (SE 3.8). Pooled k=2 (Claude Code's
runs 1 and r2, noeta's runs fix and r2): +5.4 (SE 3.6).

**What changed between the two noeta commits.** `54121930` handed commands only a short
allowlist of environment variables, so a task image's `CARGO_HOME`, `RUSTUP_HOME`,
`JAVA_HOME` and the like were lost; Claude Code passes the whole environment. `34f8acee`
passes the environment minus noeta's own credentials, and sends the token-budget notes only to
Claude models, as Claude Code does. After the fix, trials that set toolchain variables by
hand fell from 22 to 5, trials that hit "toolchain not found" from 7 to 2, and timeouts from
59 to 43.

### Terminal-Bench 4.0 (54 tasks, 2 h cap)

`terminal-bench/terminal-bench@4.0.0`, 54 of 66 tasks: 3 GPU tasks need a newer CUDA than the
machine's driver provides, 8 tasks' own reference solutions fail their tests, and one
reference solution fails on this machine. The agent cap is 2 h, below the tasks' own, so the
numbers are not comparable with the leaderboard. 8 trials at a time, k=1.

| Run | Agent (noeta commit) | Passed | 2-h timeouts | Tokens in / cached / out | Cost |
|---|---|---|---|---|---|
| 1 | noeta `54121930` | 2 (3.7 %) | 39 | 308M / 303M / 9.1M | $7.10 |
| | Claude Code | 1 (1.9 %) | 41 | 287M / 283M / 8.3M | $6.50 |
| fix | noeta `34f8acee` | 2 (3.7 %) | 37 | 176M / 172M / 6.9M | $5.23 |

Both agents are near zero: on this model at a 2-hour cap Terminal-Bench 4.0 does not separate
them.

### A larger private model (20 SWE-rebench tasks)

A larger private model (1M window, 128K output cap for both), 20 tasks drawn with
seed 0, one at a time, 50-minute cap:

| Run | Agent (noeta commit) | Passed | 50-min timeouts |
|---|---|---|---|
| 1 | noeta `54121930` | 6 (30 %) | 12 |
| | Claude Code | 10 (50 %) | 8 |
| fix | noeta `34f8acee` | 10 (50 %) | 5 |

Against run 1's Claude Code: noeta `54121930` −20 points (SE 11.7), noeta `34f8acee` 0
(SE 10.3; each passed two tasks the other did not). With n=20 neither is significant.

## Caveats

- **Small samples, one model.** The SWE-rebench gain is about 1.5 standard errors; a single run
  moves by ±5 points. The larger-model comparison has 20 tasks.
- **Leaked answers.** Agents may reach the internet, and the upstream fixes are public. A
  trial counts as leaked when a web or git call returned lines of the reference fix; 20 tasks
  were affected across the runs, for both agents, and the "without leaked answers" row leaves
  them out. Claude Code had the gateway's server-side `WebSearch`; noeta had no search key,
  only `WebFetch`.
- **Different instruction files.** Claude Code reads a repository's `CLAUDE.md` and
  `AGENTS.md`; noeta reads `NOETA.md` and, without one, `AGENTS.md`, never `CLAUDE.md`. In 34
  of 111 SWE-rebench tasks Claude Code read project instructions and noeta read none. This
  cuts both ways and was not equalized.
- **Retries.** Some tasks passed only on a harbor retry, for both agents; `bench/README.md`
  lists them. In Terminal-Bench, 9 of noeta's 13 retries were the same failure: the model
  returned a turn with neither text nor a tool call and noeta ended the turn as an error.
- **Rate limits.** Claude Code logs its retries on HTTP 429 / 529; noeta retries inside its
  provider client without logging them, so its count is unknown (not zero). No trial failed on
  a rate limit.
- **Not leaderboard numbers.** Terminal-Bench ran 54 of 66 tasks at a 2-hour cap instead of
  8 hours. Nothing was submitted to a leaderboard.

## Running it yourself

You need Docker, harbor 0.25.0 in any virtual environment, and gateway credentials in a
`.env` file (`NOETA_BASE_URL`, `NOETA_API_KEY`). Runs spend real tokens, which is why they are
not part of `make check`.

```bash
uv venv && uv pip install harbor==0.25.0
export NOETA_ENV_FILE=/path/to/.env
H=/path/to/venv/bin/harbor

# fetch a dataset once (runs read local copies only)
HARBOR_TELEMETRY=0 harbor download ibragim-badertdinov/swe-rebench-07-2026 -o bench/datasets/swe-rebench

# noeta next to Claude Code: a smoke run, then the full SWE-rebench at its 50 min cap
bench/compare.py run swe-rebench --tasks open-jarvis__OpenJarvis-465 \
  --agent-max-timeout-sec 1500 --concurrency 4 --harbor $H
bench/compare.py run swe-rebench --k 1 --concurrency 10 --harbor $H
bench/compare.py run tb4 --k 1 --concurrency 8 --agent-max-timeout-sec 7200 --harbor $H

bench/compare.py summary bench/jobs/<job-name>     # summarize a finished job again
```

Each job directory gets `summary.md` / `summary.json` (pass rates, paired differences,
tokens, cost, time, rate-limit signals) and `provenance.json` (the commit, whether the tree
was dirty, the wheel's hash, harbor's and Claude Code's versions). Publish the summary, never a
job directory: agents' logs can hold anything they printed.

noeta alone, one command per mode:

```bash
NOETA_EFFORT=high bench/run_benchmark.sh swe-rebench -k 3
NOETA_EFFORT=high bench/run_benchmark.sh tb4-full
bench/summarize.py --dataset bench/datasets/terminal-bench-2-1 bench/jobs/<job>
```

Give the model as `--model <model-id>` to `compare.py` or `NOETA_MODEL=<model-id>` to
`run_benchmark.sh`, using whatever id your provider or gateway gives DeepSeek V4.1 Flash; the
built-in default is the id these runs used and is unlikely to exist elsewhere. Pin the effort
too: unset, a request carries the model row's default effort (`high` for a
model whose row names none), and DeepSeek V4.1 refuses `medium`. A
model id the catalog does not know needs a models file
(`NOETA_MODELS_CONFIG`, or `NOETA_MODEL_CONTEXT_WINDOW` and `NOETA_MODEL_MAX_OUTPUT_TOKENS`
for the adapter to write one), or it runs at 128K / 16K. Restricted networks, Docker cleanup,
gateway concurrency and pinning a SWE-bench subset are covered in
[`bench/README.md`](https://github.com/initxy/noeta/blob/main/bench/README.md).
