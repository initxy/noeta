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
  scripts set it themselves. They refuse `--upload` / `--launch`; never run
  `harbor upload` or submit to a leaderboard.
- Claude Code runs with `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`, and a
  per-trial config dir inside the trial's logs; harbor's own process gets a
  throwaway `CLAUDE_CONFIG_DIR` under the jobs dir, never `~/.claude`.
- noeta sends nothing but its model calls (no telemetry exists in the package);
  its `WebFetch` reaches the web only when the agent calls it for the task.
- `run_benchmark.sh`'s export of token usage to kaboo is opt-in (`NOETA_PI_EXPORT`).

## How the adapter works

1. **Install** — install the `noeta` CLI into the sandbox from a local wheel
   (`NOETA_WHEEL`; `compare.py` builds one from the checkout when unset), or
   `NOETA_SPEC` from an index. pip runs through `NOETA_PROXY` (default: the
   proxy the Docker client gives containers).
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
5. **Usage** — token counts go into harbor's `n_input_tokens` (all input,
   cached or not) / `n_cache_tokens` / `n_output_tokens`, the fields
   `claude-code` fills. They come from the final `result` line; when harbor
   kills the agent at its timeout there is none, and the per-message usage of
   the `assistant` lines is summed instead (`metadata.usage_source` says which).

Unlike `pi`, the `-m` string is **not** split on `/` — Noeta model ids can
contain a slash and the gateway decides routing.

## Prerequisites

```bash
uv venv && uv pip install harbor==0.25.0   # any venv; pass its harbor with --harbor
# Docker running.
export NOETA_ENV_FILE=/path/to/.env        # NOETA_BASE_URL / NOETA_API_KEY (or NOETA_LIVE_*)
```

### Restricted-network setup

On a machine that cannot reach the public internet directly, set up three things,
in order:

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

4. **ripgrep in the task image.** noeta shells `Grep`/`Glob` out to
   `rg`; without it those tools fail loud and the agent loses file search. Task
   images usually ship neither `rg` nor python3, so the adapter's `install`
   apt-get's `python3 python3-pip ripgrep` as root (best-effort — needs an
   apt-based image and the proxy above). Nothing to set; noted so a non-Debian
   task image is a known gap.

5. **The *verifier* container also needs a proxy — not just the agent.** The
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

TB2 task images are not uniform; the adapter absorbs three differences:

- **PEP 668 "externally-managed" Python.** Some Debian-based images mark the
  system Python as externally managed, so a bare `pip install` fails before the
  agent runs. The adapter installs with `--break-system-packages` (safe in a
  throwaway container).
- **WORKDIR is not always `/app`.** Most images use `/app`, but a few use
  `/workspace` or an `/app/<subdir>`. The adapter defaults the workspace to the
  container's own `$(pwd)` (the image's WORKDIR), matching how harbor's `pi`
  agent behaves; `NOETA_WORKSPACE` still overrides.
- **Python older than 3.11 — handled via a private 3.12.** noeta requires
  `>=3.11`, but some images ship older Python: a few TB2 images
  (`python:3.11`/`3.10`) and **all** SWE-bench Verified images (conda envs on
  Python 3.9–3.11). A bare `pip install` there fails ("requires a different
  Python: 3.11.x not in '>=3.12'") before the agent runs. When `install` detects
  `python3 < 3.12`, it `pip install`s `uv` with the image's own Python, has uv
  fetch a prebuilt 3.12 into a venv (`/opt/noeta312`), and installs noeta there;
  `run` invokes that venv's `noeta` by absolute path. **PATH is never touched**,
  so the agent's own shell tools and the task's verifier keep using the image's
  native interpreter — only the noeta CLI runs on 3.12. (uv, not
  `conda create python=3.12`: conda's solver peaks over the trial container's
  4 GB cap and gets OOM-killed; uv stays well under.)

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
get the same model, effort, tasks, timeouts and output cap
(`CLAUDE_CODE_MAX_OUTPUT_TOKENS` = the manifest's `max_output_tokens`).
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
# at 2 h of its 8 h (internal comparison, not board-comparable — say so)
bench/compare.py run swe-rebench --k 2 --concurrency 16 --harbor $H
bench/compare.py run tb4 --k 1 --concurrency 8 --agent-max-timeout-sec 7200 --harbor $H

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

### Disk: what a run removes, and only that

A full TB4 or SWE-rebench pass pulls far more image data than a typical disk
holds, and harbor removes only each trial's own compose project
(`docker compose down --rmi local --volumes --remove-orphans`: the trial's
containers, volumes and the image it built). So `compare.py` removes, task by
task once every trial of the task has finished (and for all tasks when the job
ends), what the run pulled for that task — and nothing else:

- it records the host's image IDs and build-cache record IDs when the run
  starts; anything already there is never touched;
- a task's images are the `docker_image`s its `task.toml` names (TB4's agent
  and verifier images) or, for a task harbor builds, the images its
  Dockerfiles start `FROM` (SWE-rebench's base images, which arrive through
  BuildKit and live in the build cache: the records whose description is the
  pull of that image, plus records built on top of them);
- removal is `docker rmi <id>` without `-f` (refused while a container uses
  it) and `docker buildx prune --filter id=<record>` per record; never an
  unfiltered `docker system prune` / `builder prune`.

Each removal is logged to `<jobs-dir>/<job-name>.cleanup.log`;
`--keep-task-images` turns it off. Peak disk is then the images of the tasks
in flight (TB4: up to ~4 GB per task with its verifier image).

### What the summary measures

`summary.md` / `summary.json` in the job dir, per agent:

- **pass rate** — trials with reward > 0 over all trials; and over trials that
  got a reward. A trial with no reward is an infrastructure error and is listed
  as one; a trial that hit the agent timeout is still graded (as harbor does).
- **tokens** in (all input, cached or not) / cached / out, from harbor's trial
  result: Claude Code's from its session file, noeta's from its stream (summed
  per message when the agent was killed at the timeout).
- **cost** at DeepSeek V4.1-Flash first-party list price, USD per 1M tokens:
  cache hit 0.003, cache miss 0.15, output 0.60 off-peak, twice that at peak
  (weekdays 01:00–04:00 and 06:00–10:00 UTC, by each trial's start). The agents'
  own cost fields are meaningless here (Claude Code prices the model as Claude)
  and the gateway's own pricing is unknown.
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
models manifest, telemetry off) and runs one mode:

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
It removes no task images; for a full pass of noeta alone use
`compare.py run … --agents noeta`, which does (see Disk above).

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
pinned instance-id list. State honestly what was run and what was not; usage /
cost in the JSON is token-derived and is approximate when the gateway model is
not in the SDK's pricing catalog.
