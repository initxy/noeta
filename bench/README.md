# bench/ — Noeta on public benchmarks

Benchmark Noeta's `main` preset through [harbor](https://github.com/harbor-framework/harbor)
0.25, the official Terminal-Bench harness, and compare it with Claude Code on
the same model. Current targets:

| Benchmark | harbor dataset | Tasks | Agent cap |
|---|---|---|---|
| Terminal-Bench 4.0 | `terminal-bench/terminal-bench@4.0.0` | 63 of 66 (`math-eval-grader`, `jax-speedrun-gpu`, `fp8-rmsnorm-gemm` need a GPU and are excluded; report "63/66"; the 2026-10 run scored 54, see [Results](#results-2026-10-30-to-2026-11-01)) | 8 h |
| SWE-rebench, July 2026 window | `ibragim-badertdinov/swe-rebench-07-2026` | 111 | 50 min |
| Terminal-Bench 2.1 (cheap loop, smoke) | `terminal-bench/terminal-bench-2-1` | 89 | 10 min – 3 h |

The reference agent is harbor's built-in `claude-code` agent pinned to Claude
Code **2.1.312**; the default model is `opensource/deepseek_v4_1_flash` through
the gateway's Anthropic Messages endpoint, effort `high`, for both agents.

## Why these benchmarks (checked 2026-11-03, again 2026-11-04)

Public coding-agent boards in use, and their latest versions, as of 2026-11-03:

| Board | Latest | Status | Used here |
|---|---|---|---|
| Terminal-Bench | 4.0 (tbench.ai) | the agent board's headline since 2.1 saturated (top models 87–92 % on 2.1); Claude Code is a listed agent | yes, 54 of 66 tasks |
| SWE-bench Verified | 500 tasks | saturated (top ~97 %), archived by some boards | no |
| SWE-bench Pro | public set, 731 tasks | the current headline board for repository-level fixes | no: 731 large images per run is beyond one machine for two agents twice, and a fixed public set can leak into training data; a candidate for a larger machine |
| SWE-rebench | monthly windows of fresh GitHub tasks | fresh by design (issues after the model's data), same scaffold for every model | yes, the July 2026 window (111 tasks), the newest harbor offered when chosen |

Terminal-Bench 4.0 measures the agent in a terminal, with Claude Code as its reference
agent; SWE-rebench measures repository fixes on tasks the model cannot have seen, at a cost
one machine can run twice for both agents. Checked on the boards' own pages on 2026-11-03 and again on 2026-11-04 (unchanged): tbench.ai names Terminal-Bench 4.0 as its
latest version; swe-rebench.com's current window is 2026-05-15 to 2026-07-01, "111 problems
from 65 repositories", the 111 tasks run here (harbor's `swe-rebench-07-2026`). The status
column (saturation, headline boards) comes from public summaries, which disagree on model
names and scores.

## What's here

| File | Role |
|------|------|
| `harbor_adapter.py` | `Noeta(BaseInstalledAgent)` — installs and invokes `noeta -p --output-format stream-json` inside harbor's sandbox. Modelled on harbor's own `pi.py`. |
| `compare.py` | noeta and Claude Code on one harbor job (same model, effort, tasks, timeouts, concurrency) and the comparison summary. |
| `run_benchmark.sh` | noeta alone, one command per run mode. |
| `summarize.py` | Turns a noeta-only job into a score: pass / fail / error, both denominators, per-difficulty split. |
| `datasets/` | Local copies of the public datasets (gitignored; harbor re-downloads on demand). |
| `jobs/` | harbor run artifacts (gitignored). |

## Why this is not in `make check`

`make check` verifies everything that can be verified offline: the `noeta -p`
CLI is unit-tested against a fake provider, and the import boundary is enforced.
Benchmark runs are different in kind — they need **Docker**, the **`harbor`
CLI**, and **real gateway credentials**, and they **spend real tokens**. They
are a manual gate you run on a machine you control, not part of the automated
suite.

## Nothing is uploaded

- harbor sends anonymous usage telemetry unless `HARBOR_TELEMETRY=0`; both
  scripts set it themselves, and `LITELLM_LOCAL_MODEL_COST_MAP=True` so the
  LiteLLM harbor imports downloads no price map. They refuse `--upload` /
  `--launch`; never run `harbor upload` or submit to a leaderboard.
- Datasets are read from local copies only: a registry run (`-d`) asks harbor's
  hub and records a download there. Fetch a copy once with
  `HARBOR_TELEMETRY=0 harbor download <id> -o bench/datasets/<key>`.
- Claude Code runs with `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`, and a
  `--settings` layer with `skipWebFetchPreflight: true` — without it 2.1.312's
  WebFetch asks `api.anthropic.com/api/web/domain_info` about every domain, and
  no environment switch stops that. Its config dir is per trial, inside the
  trial's logs; harbor's own process gets a throwaway `CLAUDE_CONFIG_DIR` under
  the jobs dir, never `~/.claude`. The other Anthropic endpoints in the 2.1.312
  binary are OAuth / claude.ai features (unused with an API key), event logging
  and Datadog / GrowthBook (telemetry and non-essential traffic, off above), and
  `downloads.claude.ai` (the auto-updater, off; harbor's install fetches CC from
  there). Network logs of the smoke runs showed CC reaching only the gateway
  and, during install, `downloads.claude.ai`.
- noeta sends nothing but its model calls (no telemetry exists in the package);
  its `WebFetch` reaches the web only when the agent calls it for the task.
- The gateway key: never printed; replaced by `<redacted>` in every file under
  the job dir every two minutes and when the run ends (agents sometimes print
  their environment). Still, a job dir is for this machine — do not share it or
  attach it to a report; publish the summary.

## How the adapter works

1. **Install** — noeta goes into its own venv under `/opt/noeta-bench`, never
   into the task's interpreter (a same-container verifier, SWE-rebench's and
   TB2.1's, must test the environment the task built) and never with the
   image's package manager. The adapter uploads a static uv and rg and a
   python-build-standalone CPython 3.12 for the image's libc (`compare.py`
   fetches them once, sha256-checked: `bench/compare.py tools <dir>`), makes
   the venv and installs the wheel (`NOETA_WHEEL`; `compare.py` builds one from
   the checkout) with its dependencies from PyPI through `NOETA_PROXY` (default:
   the proxy the Docker client gives containers). Only `rg` goes on noeta's
   `PATH`.
2. **Run** — `noeta -p "<instruction>" --output-format stream-json --verbose
   --permission-mode bypassPermissions --cwd "$(pwd)" --model <id> --effort <e>`,
   with `NOETA_PROVIDER` / `NOETA_BASE_URL` / `NOETA_API_KEY` (or the 0.6
   `LLM_BASE_URL` / `LLM_API_KEY`). The effort is harbor's `--effort` (as for
   `claude-code`), else `NOETA_EFFORT`; pin it — unset, noeta's Anthropic wire
   sends `medium`, which DeepSeek V4.1 refuses (it takes `low` / `high` /
   `xhigh` / `max`).
3. **Models manifest** — a gateway model id is unknown to noeta's catalog and
   would run at the 128k-context / 16k-output fallbacks. The adapter uploads a
   manifest: `NOETA_MODELS_CONFIG=<file>`, or a one-row manifest for the `-m`
   model from `NOETA_MODEL_CONTEXT_WINDOW` + `NOETA_MODEL_MAX_OUTPUT_TOKENS`.
   A row's `family` is the **wire** family (`anthropic` / `openai`), not the
   vendor; missing, it is filled from `NOETA_PROVIDER`'s wire, and any other
   value is refused before the trial starts.
4. **Network policy** — the adapter declares `NOETA_BASE_URL`'s host to harbor,
   so a task whose sandbox has no internet still allows the model calls (as
   harbor's `claude-code` agent does with `ANTHROPIC_BASE_URL`); no
   `--allow-agent-host` needed.
5. **End of run** — at the agent timeout harbor only cancels its `docker exec`
   client; the agent process tree is stopped too (STOP, TERM, KILL after 5 s,
   descendants included, also those reparented to PID 1 once their parent
   died), for noeta and for Claude Code alike.
6. **Usage** — token counts go into harbor's `n_input_tokens` (all input,
   cached or not) / `n_cache_tokens` / `n_output_tokens`, the fields
   `claude-code` fills. After the run (a timed-out one too) the adapter reads
   every model call of the task tree from noeta's own store: the main agent,
   its sub-agents and compaction summaries (`metadata.usage_source=store`). The
   stream is a fallback: its `result` line misses compaction, and without one
   only the main agent's messages are in it. Side calls (session title) are in
   neither agent's count.

Unlike `pi`, the `-m` string is **not** split on `/` — Noeta model ids can
contain a slash and the gateway decides routing.

## Prerequisites

```bash
uv venv && uv pip install harbor==0.25.0   # any venv; pass its harbor with --harbor
# Docker running.
export NOETA_ENV_FILE=/path/to/.env        # NOETA_BASE_URL / NOETA_API_KEY (or NOETA_LIVE_*)
```

### Restricted-network setup

On a machine that cannot reach the public internet directly:

1. **dockerd can't pull task images from docker.io.** Give the Docker *daemon*
   an HTTP proxy (this covers image pulls only):

   ```bash
   sudo mkdir -p /etc/systemd/system/docker.service.d
   sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'
   [Service]
   Environment="HTTP_PROXY=http://<proxy-host>:<port>"
   Environment="HTTPS_PROXY=http://<proxy-host>:<port>"
   Environment="NO_PROXY=.your-corp,localhost,127.0.0.1"
   EOF
   sudo systemctl daemon-reload && sudo systemctl restart docker
   docker pull hello-world      # verify
   ```

2. **pip inside the task container has no proxy of its own** (the daemon
   proxy above covers pulls only). The adapter passes `NOETA_PROXY` to the
   install commands; it must be **container-reachable** — a loopback proxy like
   `http://127.0.0.1:7897` is useless inside the container. Both scripts default
   it to the Docker client's proxy (point 5). Without one, dependency install is
   slow enough to blow past harbor's agent-setup timeout.

3. **`noeta` 1.0 isn't on PyPI yet.** `compare.py` builds a wheel from the
   checkout; for `run_benchmark.sh`, `uv build --wheel` (it picks the newest
   `dist/noeta-1*.whl`) or set `NOETA_WHEEL`.

4. **The *verifier* container also needs a proxy — not just the agent.** The
   adapter's `NOETA_PROXY` (point 2) only covers the *agent* exec. But harbor's
   verifier phase runs its own container step, and many TB2 tasks' `test.sh`
   downloads a toolchain at verify time (e.g. `curl -LsSf https://astral.sh/uv`
   to get `uv`/`pytest`). With no proxy there, that download hangs and the
   verifier scores **0 even when the agent solved the task** — a silent false
   negative. Fix it once, for *all* container processes (agent + verifier), via
   the Docker **client** config:

   ```bash
   mkdir -p ~/.docker
   cat > ~/.docker/config.json <<'EOF'
   {
     "proxies": {
       "default": {
         "httpProxy":  "http://<proxy-host>:<port>",
         "httpsProxy": "http://<proxy-host>:<port>",
         "noProxy":    ".your-corp,localhost,127.0.0.1,::1,10.0.0.0/8"
       }
     }
   }
   EOF
   ```

   Keep the LLM gateway host inside `noProxy` (it is reached from inside the
   container too), or the proxy will swallow the model calls.

`harbor` must be able to import the adapter as `bench.harbor_adapter`. The
script puts the repo root on `PYTHONPATH` for you; if you invoke `harbor`
directly, do the same:

```bash
export PYTHONPATH="$(git rev-parse --show-toplevel):$PYTHONPATH"
```

### Task-image quirks the adapter handles

Task images are not uniform; the private install (own CPython, venv, `rg`)
makes their Python version, PEP 668 marker, package manager and libc
irrelevant to noeta. One more difference: **WORKDIR is not always `/app`** —
the adapter defaults the workspace to the container's own `$(pwd)` (the
image's WORKDIR), as harbor's `pi` agent does; `NOETA_WORKSPACE` overrides.

### Don't smoke-test with `-l 1`

`-l N` takes the first N tasks *after filtering*, which on TB2 deterministically
lands on `make-mips-interpreter` — a multi-hour "hard" task that just times out.
Prove the loop with an explicit lightweight task instead:

```bash
bench/compare.py run tb21 --tasks fix-git --concurrency 2
```

## Running: noeta next to Claude Code

`compare.py` puts both agents in **one** harbor job: harbor interleaves their
trials (attempt, task, agent), so both see the same gateway load over time, and
`--concurrency` is split evenly between them (`--n-concurrent-agents`). Both
get the same model, effort, tasks and timeouts, and the same context window
and output cap: Claude Code gets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` and
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` set to the manifest's `context_window` /
`max_output_tokens` (2.1.312 assumes a 200k window for a model id it does not
know and honours the first variable for any non-Claude id; the second is
capped at 128k for such ids). Each job dir gets `provenance.json`: git commit
and whether the tree was dirty, the wheel's sha256, harbor's and Claude Code's
versions, the uploaded tools' sha256s, the price used.

One real request from each agent, recorded through a local proxy with these
settings (2026-10-30, `opensource/deepseek_v4_1_flash`):

| Field | Claude Code 2.1.312 | noeta |
|---|---|---|
| `max_tokens` | 65536 | 65536 |
| `thinking` | `adaptive`, display `updates` | `adaptive`, display `updates` |
| `output_config.effort` | `high` | `high` |
| `temperature` / `top_p` | not sent | not sent |
| `context_management` | `clear_thinking_20251015`, keep all | same |
| extra calls | none | a session-title call before the first turn (thinking off) |
Datasets are read from local copies under `--datasets-dir`
(`bench/datasets/<tb4|swe-rebench|tb21>`); fetch each once with the
`harbor download` line the script prints when one is missing.

```bash
export NOETA_ENV_FILE=/path/to/.env
H=/path/to/venv/bin/harbor

# smoke: two tasks per dataset, capped agent time
bench/compare.py run tb4 --tasks music-harmony photonic-waveguide-routing \
  --agent-max-timeout-sec 1800 --concurrency 4 --harbor $H
bench/compare.py run swe-rebench --tasks open-jarvis__OpenJarvis-465 \
  react-hook-form__react-hook-form-13464 --agent-max-timeout-sec 1500 --concurrency 4 --harbor $H

# full runs: SWE-rebench at its real 50 min cap; TB4 with the agent capped
# at 2 h of its 8 h (internal comparison, not board-comparable — say so);
# the builder's state on the larger disk; concurrency sized to the free space
S=/data00/.../bench/jobs/.buildkit
bench/compare.py run swe-rebench --k 1 --concurrency 10 --builder-state-dir $S --harbor $H
bench/compare.py run tb4 --k 1 --concurrency 8 --agent-max-timeout-sec 7200 --builder-state-dir $S --harbor $H

bench/compare.py summary bench/jobs/<job-name>     # re-summarize a finished job
```

Claude Code runs as `bench.harbor_adapter:ClaudeCode`: harbor's own
`claude-code` agent with one addition, the same one the noeta adapter has —
at the agent timeout the agent's process inside the sandbox is stopped. harbor
only cancels its `docker exec` client, so without this the agent keeps working
(and editing files) while a same-container verifier runs (SWE-rebench, TB2.1);
a smoke trial passed that way after its timeout.

`--dry-run` writes the job config (`<jobs-dir>/<job-name>.config.json`) and
stops.

### Docker: what a run creates, and what it removes

A run removes only what it can prove it created, and never prunes anything
unfiltered:

- **Builds** go through a buildx builder the run creates (`bench-<job>`,
  `BUILDX_BUILDER` for harbor's compose builds). Everything it pulls and caches
  is the run's; it is pruned only when the disk holding it (its state dir's, else docker's) drops below
  `--prune-below-gb` (default 6; a pruned cache makes the next trial of a task
  pull again) and removed whole when the run ends. The host's default builder
  is not used. A builder, container or state directory of the same name
  already there stops the run without touching it; a failed creation undoes
  only the steps that succeeded. The buildkit image the builder runs is
  removed at the end if it was not on the host before.
- **Prebuilt task images** are not pulled into the host's image store: the run
  reads a copy of the dataset (`<jobs-dir>/<job>.tasks/`) in which TB4's agent
  and verifier images are Dockerfiles of one line, `FROM <prebuilt image>`,
  and every compose service's `image: X` (the databases, queues and services
  of the 11 TB4 tasks with a `docker-compose.yaml`) is a build of `FROM X`
  under `.bench-images/<service>/` — every service and every other setting
  kept. So harbor builds each image inside the trial's own compose project —
  the same bytes, plus compose's labels — and its `down --rmi local --volumes`
  removes it with the trial. Builds take longer than pulls; both agents get
  `--build-timeout-mult` (default 3) on the environment build timeout.
- **Leftovers**: an image two trials built alike can stay dangling, and
  anonymous volumes outlive their containers. Removed once the trial has
  finished, only when compose's `com.docker.compose.project` label (or the
  image name) is one of this job's trial projects, or `docker events` showed
  the volume mounted into this job's container (however short-lived);
  `docker rmi` / `docker volume rm` without `-f`. Logged to
  `<jobs-dir>/<job>.cleanup.log`.

So disk use peaks at the images of the trials in flight plus the builder's
cache, and returns to where it was (measured: within 0.1 GB). One daemon can
run several `compare.py` jobs at once (each has its own builder and trial
names).

`--builder-state-dir <dir>` puts the builder's state in `<dir>/<builder>` (a
new directory, emptied and removed with the builder) through a buildkitd
container the run starts — use a disk with room. Measured peak per task in
flight, one trial, 60 s agent cap:

| Task (compressed image size) | builder in a docker volume | builder state on another disk |
|---|---|---|
| SWE-rebench `cloudflare__vinext-1371` (0.89 GB) | 5.9 GB on docker's disk | 2.0 GB on docker's disk + 3.5 GB on the other |
| TB4 `html-js-filter` (0.06 GB agent + 1.0 GB verifier image) | 1.8 GB | — |

Two trials of one task share most of it (same layers). Size the concurrency
to the free space on docker's disk: SWE-rebench images are 0.95 GB compressed
at the median (p90 1.4 GB; seven tasks 2.4–6.5 GB), TB4 tasks 0.27 GB (agent +
verifier; four tasks 4.2–7.5 GB).

### What the summary measures

`summary.md` / `summary.json` in the job dir (`summary --out` for several jobs),
per agent:

- **pass rate** — trials with reward ≥ 1 over all trials. *setup failed*
  (the agent did not install) counts in the denominator: it is the agent's
  failure. *infra errors* (no grade for another reason) are shown apart, with
  the pass rate without them. A trial that hit the agent timeout is graded.
- **paired difference** — over tasks both agents ran, the mean of the
  per-task pass-rate difference and its standard error.
- **tokens** in (all input, cached or not) / cached / out, from harbor's trial
  result: Claude Code's from its session files (sub-agents included), noeta's
  from its store (sub-agents and compaction included). *no tokens* counts the
  trials that reported none (their tokens are 0 in the totals).
- **cost** at DeepSeek's own list price for DeepSeek-V4.1-Flash
  (<https://api-docs.deepseek.com/quick_start/pricing/>, read 2026-10-30), USD
  per 1M tokens off-peak: cache hit 0.003, cache miss 0.15, output 0.60; peak
  (01:00–04:00 and 06:00–10:00 UTC, Monday to Friday, excluding Chinese public
  holidays — holidays not modelled) is twice that, applied by the share of each
  trial's agent time in peak hours. An estimate, not the gateway's bill; other
  models get no cost.
- **time** — wall time of the job, mean and summed agent time; setup, agent and
  verifier seconds per trial.
- **429 / overload** — gateway rate-limit or overload signals in the agents'
  logs: Claude Code's `api_retry` events for HTTP 429 / 529, error results
  naming one, and plain stderr lines. noeta's stream has no retry event; a
  retry counts only when noeta prints it on stderr, so its count is a lower
  bound.

### Gateway concurrency

Measured 2026-10-30 (03:00 UTC) against `opensource/deepseek_v4_1_flash` on
the gateway's Anthropic endpoint, effort `high`:

| Load | Requests | Errors | p50 / p90 latency |
|---|---|---|---|
| 8 parallel callers, 2 min, ~15k-token prompts | 158 | 0 | 4.8 / 14 s |
| 16 parallel callers | 256 | 0 | 5.3 / 22 s |
| 32 parallel callers | 423 | 0 | 5.4 / 22 s |
| 64 parallel callers | 172 | 0 | 50 / 122 s |
| 16 agents (8 noeta + 8 Claude Code) on TB2.1, 12 min cap | — | 0 rate-limit / overload signals | — |

No 429 at any level, but past 32 parallel calls the gateway queues: throughput
halves and latency grows tenfold. An agent is waiting on a tool part of the
time, so up to ~32 concurrent agents stay under that knee; the host (16 cores,
31 GB) is the tighter limit. Re-measure before a long run; the gateway is
shared.

## Running noeta alone

`run_benchmark.sh` sources `.env`, sets the sandbox plumbing (wheel, proxy,
models manifest, uploaded tools, telemetry off), reads datasets from
`NOETA_DATASETS_DIR` (default `bench/datasets/<key>`; it refuses a registry id),
redacts the key from the jobs dir after each run, and runs one mode:

```bash
NOETA_EFFORT=high bench/run_benchmark.sh tb4-full
NOETA_EFFORT=high NOETA_CONCURRENCY=8 bench/run_benchmark.sh swe-rebench -k 3
NOETA_EFFORT=high bench/run_benchmark.sh smoke-tb      # one TB2.1 task
```

Modes: `tb4-full` · `swe-rebench` · `smoke-tb` · `smoke-swe` · `tb-quick6` ·
`tb21-sample40` · `tb21-full` · `tb3-full` · `aider-full` · `swe-15` · `swe-60` ·
`swe-full` · `swe-pro-full` · `swe-subset`; `tb4-full` and `swe-rebench` pass any
further arguments to `harbor run`. `preflight` prints the resolved model /
effort / concurrency / wheel before it spends anything — check that line first.
It pulls prebuilt task images into the host's image store and removes none;
for a full pass of noeta alone use `compare.py run … --agents noeta`, which
builds them under the trials' own projects (see Docker above).

### Reading the result

`summarize.py` turns a job into a score:

```bash
bench/summarize.py --dataset bench/datasets/terminal-bench-2-1 bench/jobs/<job>
```

It reports pass / fail / **error** separately (errors are gateway/timeout infra,
never counted as passes) and both denominators: strict (errors = fail) and fair
(errors excluded).

## The SWE-bench subset must be pinned, not just sized

`-l 50` takes "the first 50 after filtering" — cheap, but not reproducible and
not citable. For a **published** number, name the exact instances so anyone can
re-run the identical set:

```bash
export SWE_INCLUDE_GLOBS="django__django-13741 astropy__astropy-14182 ..."
bench/run_benchmark.sh swe-subset
```

Each id becomes a `-i/--include-task-name` filter. Record the full id list with
the score when publishing (below). TB2.1 is small enough (89 tasks) to
run in full, so it needs no subset.

## Results (2026-10-30 to 2026-11-01)

One machine (16 cores, 62 GB, an NVIDIA T4 the GPU tasks could not use), harbor 0.25.0,
Claude Code **2.1.312** (harbor's `claude-code` agent, telemetry off, process
tree stopped at the cap), model `opensource/deepseek_v4_1_flash` through the
gateway's Anthropic endpoint for both agents, effort `high`, 1M context window
and 65,536 output tokens for both. Each run is one graded attempt per task
(k=1; harbor may retry an attempt that ended without a grade, below);
noeta and Claude Code trials of one job are interleaved, so they share the
gateway's load over time. Two noeta commits:

- **54121930** — the commit first benchmarked. Its `Bash` handed commands only a
  short allowlist of environment variables (`PATH`, `HOME`, `LANG`, …), so the
  image's `CARGO_HOME`, `RUSTUP_HOME`, `JAVA_HOME` and the like were lost; Claude
  Code passes the whole environment.
- **34f8acee** — main after the fixes found by these runs. Two changes the model
  sees: commands inherit the environment minus noeta's own credentials
  (`GIT_EDITOR=true` as Claude Code), and the `<total_tokens>` budget notes go
  only to Claude models, as Claude Code 2.1.312 does (784c0cd8; DeepSeek got
  them at 54121930). Also a `-p` race that could drop the final result line.
  The gains below are those two changes together; only the first is traced to
  individual tasks.

Cost is DeepSeek's list price for DeepSeek-V4.1-Flash
(<https://api-docs.deepseek.com/quick_start/pricing/>, read 2026-10-30; see
[What the summary measures](#what-the-summary-measures)); every run below fell
in off-peak hours, so list and off-peak cost agree.

Rate limits are Claude Code's `api_retry` events (HTTP 429 / 529). noeta
retries inside its provider client without logging it, so its rate-limit
count cannot be observed (it is not zero, it is unknown). No trial failed on a
rate limit.

Harbor retried a trial that ended without a grade (up to 3 times, 5 / 10 /
20 min apart; a timeout is graded and never retried); a retried trial starts
over in a new container, so a retry gives the agent a second attempt. Most
causes were the environment: Claude Code's install (`apt-get install nodejs`
through the proxy failing, or the install timing out), containers harbor could
not resolve, and network errors. Not all: in TB4, 9 of noeta's 13 retries were
the same failure mid-run — the model returned a turn with neither text nor a
tool call and noeta ended the turn as an error (exit 1): `sglang-qwen-burst` in
run 1 (after 6.8 min, 23 rounds) and in the rerun (43 min, 45 rounds), and
`vllm-deepseek-streaming` in the rerun (25 min, 54 rounds), each retried 3
times; a fix is open separately. Claude Code's `gotd__td-1759` in SWE-rebench
run 1 exited non-zero after writing its trajectory and passed on the retry;
noeta's `kestra-io__kestra-16119` and `intellectronica__ruler-556` (run 1)
exited non-zero for a reason the logs do not show.

| run | retries, noeta | retries, Claude Code | passed only after a retry |
|---|---|---|---|
| SWE-rebench 1 | 2 (non-zero exit) | 2 (install timeout, non-zero exit) | noeta `kestra-io__kestra-16119`; Claude Code `floci-io__floci-1142`, `gotd__td-1759` |
| SWE-rebench fix | 0 | — | — |
| SWE-rebench r2 | 8 (container not resolved 7, non-zero exit 1) | 19 (install non-zero exit 10, install timeout 5, container not resolved 4) | noeta `oxc-project__oxc-22623`, `pydantic__pydantic-ai-5580`, `react-hook-form__react-hook-form-13464`, `vuejs__core-14877`; Claude Code `helidon-io__helidon-11979`, `pydantic__pydantic-ai-5580`, `react-hook-form__react-hook-form-13464`, `vuejs__core-14877` |
| TB4 1 | 7 (non-zero exit 5, network 1, container 1) | 10 (network 5, non-zero exit 3, install timeout 2) | — |
| TB4 fix | 7 (non-zero exit 6, container 1) | — | — |
| larger model 1 | 0 | 0 | — |
| larger model fix | 4 (container not resolved) | — | noeta `kubernetes-sigs__kueue-11619`, `withcoral__coral-1143` |

### SWE-rebench 07-2026 (111 tasks, 50 min cap)

All 111 tasks of `ibragim-badertdinov/swe-rebench-07-2026`, at the dataset's
own 50-minute agent cap.

**Headline — noeta 34f8acee against Claude Code, two attempts per task each
(k=2, pooled):**

| | noeta 34f8acee | Claude Code 2.1.312 | paired difference (SE) |
|---|---|---|---|
| all 111 tasks | 50.5 % | 45.0 % | **+5.4 (3.6)** |
| same-time pair only (run r2, k=1) | 49.5 % | 42.3 % | +7.2 (3.8) |
| 91 tasks without leaked answers, k=2 | 51.1 % | 44.0 % | +7.1 (3.6) |
| 91 tasks without leaked answers, r2 only | 50.5 % | 42.9 % | +7.7 (3.9) |

The paired difference is the mean over tasks of noeta's pass rate minus Claude
Code's, with its standard error. Pooled k=2: Claude Code's trials of runs 1 and
r2, noeta's of runs fix and r2. One k=1 run moves by about ±5 points (Claude
Code scored 47.7 % in run 1 and 42.3 % in r2), so read the pooled row.

Runs:

| run | agents (noeta commit) | started (UTC) | wall | passed | 50-min timeouts | tokens in / cached / out | cost |
|---|---|---|---|---|---|---|---|
| 1 `rb-ds41` | noeta 54121930 | 10-30 14:37 | 11.6 h | 50 (45.0 %) | 59 | 326M / 317M / 5.4M | $5.58 |
| | Claude Code | | | 53 (47.7 %) | 63 | 381M / 370M / 6.0M | $6.25 |
| fix `rb-ds41-fix` | noeta 34f8acee | 10-31 03:52 | 5.4 h | 57 (51.4 %) | 43 | 390M / 381M / 5.5M | $5.77 |
| r2 `rb-ds41-r2` | noeta 34f8acee | 10-31 21:29 | 11.6 h | 55 (49.5 %) | 49 | 300M / 292M / 4.9M | $5.01 |
| | Claude Code | | | 47 (42.3 %) | 66 | 319M / 309M / 5.6M | $5.70 |

Concurrency 16 trials in each run; every trial was graded. Run 1 shared the gateway with the
larger-model run (1 trial) and, from 17:08, the TB4 run (8); run fix was started
while those two still ran so the gateway load matched; r2 ran beside the TB4
noeta rerun (8).
Rate limits: Claude Code 0 in every run; noeta's are not logged.

Paired differences per run: run 1 (noeta 54121930) −2.7 (SE 4.3); noeta
34f8acee of run fix against run 1's Claude Code +3.6 (SE 5.1); r2 +7.2 (SE 3.8).

What the fix changed (noeta, run 1 → run fix): trials that set toolchain
variables by hand 22 → 5 (the rest are deliberate, such as `GOFLAGS=-mod=mod`),
trials that hit "toolchain not found" 7 → 2, trials that timed out without
changing a file 17 → 7, timeouts 59 → 43. The original losses it explains:
`gleam-lang__gleam-5797` (cargo could not find its toolchain; a hand-set
`CARGO_HOME` sent it to the network; it submitted code that did not compile) and
`helidon-io__helidon-11979`, both passed after the fix. Claude Code never set
such variables in run 1.

**Leaked answers.** Agents may reach the internet, and the upstream fix is
public. A trial counts as leaked when one of these calls returned lines of
the reference fix: a `WebFetch` / `WebSearch` or a shell `curl`, `wget`, `gh`,
`git ls-remote` / `show` / `log` on a pull-request, commit, compare, `.patch`
or `.diff` URL of the task's own upstream repo; a web call naming the PR
number and the repo; a `git clone` / `fetch` / `pull` of that repo; or any
shell command that names the path of such a clone (`cat`, `grep`, `git log
-p`, …). Lines counted are the fix's
distinct added lines of at least 20 characters; the trial is leaked when at
least min(3, max(1, a quarter of them)) came back. A task is left out when
either agent leaked in any run. Both agents do it; across the three runs 20
tasks are affected:
`HKUDS__nanobot-4129`, `Martian-Engineering__lossless-claw-814`,
`apache__dubbo-go-3357`, `cloudflare__vinext-1837`, `floci-io__floci-1325`,
`kubernetes-sigs__kueue-11559`, `kubernetes-sigs__kueue-11619`,
`langchain4j__langchain4j-5315`, `langchain4j__langchain4j-5367`,
`marimo-team__marimo-9766`, `matrixorigin__matrixone-24632`,
`mikro-orm__mikro-orm-7799`, `mui__base-ui-4849`,
`pydantic__pydantic-ai-5580`, `pydantic__pydantic-ai-5842`,
`ubugeeei-prod__vize-1508`, `ubugeeei-prod__vize-774`,
`vuejs__core-14897`, `withcoral__coral-1186`,
`woodpecker-ci__woodpecker-6623`. Claude Code had the gateway's server-side
`WebSearch`; noeta had no search key, only `WebFetch`.

**Project instruction files differ.** Claude Code reads the repository's
`CLAUDE.md` (and `.claude/rules`) and `AGENTS.md`; noeta reads `NOETA.md` and,
in a directory without one, `AGENTS.md`, never `CLAUDE.md`. In run 1 Claude
Code loaded project instructions in 73 of 111 tasks: 56 had a `CLAUDE.md` (22
of them also an `AGENTS.md`, which noeta reads too), 17 only an `AGENTS.md`.
So in 34 tasks Claude Code read project instructions and noeta read none;
in the 22 with both files Claude Code also read `CLAUDE.md` on top of the
`AGENTS.md` noeta saw. It cuts both ways: on `Soju06__codex-lb-744` the repo's
OpenSpec workflow was in `CLAUDE.md`, and on `kubernetes-sigs__kueue-11619`
Claude Code followed the repo's instructions, stopped after 37 s to wait for
an acknowledgement and scored 0. Not equalized in these runs.

### Terminal-Bench 4.0 (54 tasks, 2 h cap)

`terminal-bench/terminal-bench@4.0.0`, 54 of 66 tasks scored: the 3 GPU tasks
(`math-eval-grader`, `jax-speedrun-gpu`, `fp8-rmsnorm-gemm`; they need CUDA
≥ 13.2, which the machine's driver cannot provide), 8 compose tasks whose own
reference solution fails the tests (on the mirror and on the original images:
`cumulative-layout-shift`, `heat-pump-warranty`, `intrastat-meldung`,
`payments-pipeline-fix`, `legacy-utility-triage`, `live-database-cutover`,
`freight-dispatch-shift`, `medical-claims-processing`) and `cad-model`
(reference solution fails on this machine) are left out. The agent cap is
2 h, below the tasks' own caps, so the numbers are not leaderboard-comparable.
Concurrency 8 trials, k=1.

| run | agent (noeta commit) | wall | passed | 2-h timeouts | tokens in / cached / out | cost |
|---|---|---|---|---|---|---|
| `tb4-ds41` | noeta 54121930 | 28.3 h | 2 (3.7 %) | 39 | 308M / 303M / 9.1M | $7.10 |
| | Claude Code | | 1 (1.9 %) | 41 | 287M / 283M / 8.3M | $6.50 |
| `tb4-ds41-fix` | noeta 34f8acee | 13.8 h | 2 (3.7 %) | 37 | 176M / 172M / 6.9M | $5.23 |

Run 1 started 10-30 17:08 beside SWE-rebench run 1; the rerun started 10-31
21:28 beside SWE-rebench run r2. Paired difference, run 1: +1.9 (SE 1.9); the
rerun's noeta against run 1's Claude Code: +1.9 (SE 3.2). Ungraded trials,
counted as fails: in run 1 Claude Code's `wdm-design` never ran (its install
failed on all 4 attempts: `apt-get` through the proxy got HTTP 500) and its `distributed-dedup` timed out with
no grade; in the rerun noeta's `distributed-dedup`, `uefi-bootkit` and
`vba-userform-port` timed out with no grade (their verifier did not finish). Both agents are near zero: on this model at a 2-hour cap Terminal-Bench 4.0 does not separate them.
The failures that did not time out are graded fails (some tests pass, some do
not); every scored task passes with its reference solution.

### A larger private model (20 SWE-rebench tasks)

A larger private gateway model (1M window, 128K output cap for both), 20 tasks
drawn with seed 0 from the 104 SWE-rebench tasks whose image is not large,
concurrency 1, 50-minute cap. No list price for this model; no leaked answers
in either run.

| run | agent (noeta commit) | started (UTC) | wall | passed | 50-min timeouts | tokens in / cached / out |
|---|---|---|---|---|---|---|
| 1 `rb-ad1max` | noeta 54121930 | 10-30 14:38 | 29.7 h | 6 (30 %) | 12 | 83M / 81M / 1.2M |
| | Claude Code | | | 10 (50 %) | 8 | 107M / 103M / 0.8M |
| fix `rb-ad1max-fix` | noeta 34f8acee | 11-01 09:11 | 11.1 h | 10 (50 %) | 5 | 114M / 110M / 0.8M |

Paired difference against run 1's Claude Code: noeta 54121930 −20 points
(SE 11.7), noeta 34f8acee 0 (SE 10.3; each passed 2 tasks the other did not);
n=20, so neither is significant. Before the fix noeta wrote 810 output tokens
per model call against Claude Code's 540; after it, 533. The rerun did not
share the gateway with as much load as run 1 (beside the TB4 rerun for its
first two hours, then alone; run 1 ran beside SWE-rebench run 1 and the TB4
run), so part of its shorter time per round (23 s against Claude Code's 27 s
in run 1) may be the gateway. Claude Code hit 20 rate-limit retries in run 1
(HTTP 429, in 2 trials, both failed); noeta's retries are not logged, so its
count is unknown. No trial failed on a rate limit.

## Publishing

After a scored run, summarize it in a results page under `docs/` (none is
published for 1.0 yet): the score, the model
id, the dataset **name@version**, the exact command, and — for SWE-bench — the
pinned instance-id list, and `provenance.json`. State honestly what was run and
what was not (TB4 "63/66"; an agent cap below the task's own is not
board-comparable). Publish the summary, never a job dir: agents' logs can hold
anything they printed.
