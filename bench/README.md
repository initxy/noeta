# bench/ — Noeta on public benchmarks

Benchmark Noeta's `main` preset through [harbor](https://github.com/harbor-framework/harbor)
0.25, the official Terminal-Bench harness, and compare it with Claude Code on
the same model. Current targets:

| Benchmark | harbor dataset | Tasks | Agent cap |
|---|---|---|---|
| Terminal-Bench 4.0 | `terminal-bench/terminal-bench@4.0.0` | 63 of 66 (`math-eval-grader`, `jax-speedrun-gpu`, `fp8-rmsnorm-gemm` need a GPU and are excluded; report "63/66") | 8 h |
| SWE-rebench, July 2026 window | `ibragim-badertdinov/swe-rebench-07-2026` | 111 | 50 min |
| Terminal-Bench 2.1 (cheap loop, smoke) | `terminal-bench/terminal-bench-2-1` | 89 | 10 min – 3 h |

The reference agent is harbor's built-in `claude-code` agent pinned to Claude
Code **2.1.312**; the default model is `opensource/deepseek_v4_1_flash` through
the gateway's Anthropic Messages endpoint, effort `high`, for both agents.

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
  logs (Claude Code's retry notices, noeta's stderr).

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

## Publishing

After a scored run, summarize it in a results page under `docs/` (none is
published for 1.0 yet): the score, the model
id, the dataset **name@version**, the exact command, and — for SWE-bench — the
pinned instance-id list, and `provenance.json`. State honestly what was run and
what was not (TB4 "63/66"; an agent cap below the task's own is not
board-comparable). Publish the summary, never a job dir: agents' logs can hold
anything they printed.
