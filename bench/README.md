# bench/ — Noeta on public benchmarks

Benchmark Noeta's `main` preset on **Terminal-Bench 2.1** and **SWE-bench
Verified** through [harbor](https://github.com/harbor-framework/harbor), the
official Terminal-Bench harness. This is how "is Noeta any good, and how
does it compare to the field?" becomes a number an outsider can check. The
public TB2.1 leaderboard's own reference agent is **Terminus 2**; harbor also
ships a `pi` installed agent, and this adapter is modelled on that `pi.py`
(both are `BaseInstalledAgent` subclasses).

## What's here

| File | Role |
|------|------|
| `harbor_adapter.py` | `Noeta(BaseInstalledAgent)` — installs and invokes `noeta -p --output-format stream-json` inside harbor's sandbox. Modelled on harbor's own `pi.py`. |
| `run_benchmark.sh` | One command per run mode; sources `.env` and sets sandbox plumbing, so a run is just `NOETA_MODEL` + `NOETA_EFFORT`. |
| `summarize.py` | Turns a job into a score: pass / fail / error, both denominators, per-difficulty split. |
| `datasets/` | Local copies of the public datasets (gitignored; harbor re-downloads on demand). |
| `jobs/` | harbor run artifacts (gitignored). |

## Why this is not in `make check`

`make check` verifies everything that can be verified offline: the `noeta -p`
CLI is unit-tested against a fake provider, and the import boundary is enforced.
Benchmark runs are different in kind — they need **Docker**, the **`harbor`
CLI**, and **real gateway credentials**, and they **spend real tokens** (an
opus-tier full SWE-bench run is hundreds of USD and hours of wall-clock). They
are a manual gate you run on a machine you control, not part of the automated
suite.

## How the adapter works

It mirrors harbor's `pi` agent, with two differences that come from Noeta
reaching its model through a gateway rather than a passthrough provider:

1. **Install** — install the `noeta` CLI into the sandbox. Until `noeta` 1.0
   is on PyPI, build a wheel locally (`uv build --wheel` → `dist/`) and point
   `NOETA_WHEEL` at it; the adapter uploads that wheel into the container and
   `pip install`s it. With `NOETA_WHEEL` unset it falls back to installing
   `NOETA_SPEC` (default `noeta`) from the configured index.
2. **Run** — invoke `noeta -p "<instruction>" --output-format stream-json --verbose
   --cwd "$(pwd)" --model <id>`,
   forwarding `NOETA_PROVIDER` / `NOETA_BASE_URL` / `NOETA_API_KEY` (or the 0.6
   `LLM_BASE_URL` / `LLM_API_KEY`) into the container. `noeta -p`
   drives the `main` agent autonomously (`bypassPermissions`, `write_mode=
   apply`) to a terminal and streams Claude Code's headless messages.
3. **Parse** — read token usage from the final `result` message into the trial context.

The model defaults to `opus4.8`; harbor's `-m` overrides it. Unlike `pi`, the
`-m` string is **not** split on `/` — Noeta model ids can contain a slash and
the gateway decides routing.

## Prerequisites

```bash
pip install harbor            # or: uv tool install harbor
# Docker running.
export LLM_BASE_URL=...        # the same gateway the product server uses
export LLM_API_KEY=...
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

2. **pip inside the task container has no proxy** (the daemon proxy above does
   not reach container processes). The adapter forwards a proxy into the
   install/run commands, but it must be **container-reachable** — a loopback
   proxy like `http://127.0.0.1:7897` from your shell is useless inside the
   container. Set `NOETA_PROXY` to a real proxy host:

   ```bash
   export NOETA_PROXY="http://<proxy-host>:<port>"
   ```

   (Without it, dependency install is slow enough to blow past harbor's
   agent-setup timeout.)

3. **`noeta` 1.0 isn't on PyPI yet.** Build a wheel and point `NOETA_WHEEL`
   at it:

   ```bash
   uv build --wheel
   export NOETA_WHEEL="$(pwd)/dist/noeta_agent-$(uv version --short 2>/dev/null || echo 0.6.0)-py3-none-any.whl"
   ```

3b. **Point the sandbox at the real model catalogue.** `models.json` is
   gitignored and not bundled in the wheel, so inside the sandbox the CLI has no
   catalogue and degrades every model to conservative fallbacks — most
   consequentially `max_output_tokens=16384`, far below what the product
   declares (the gateway model is 128000). That silently caps long answers and
   depresses scores for a reason that is config, not capability. Upload the
   catalogue by
   pointing `NOETA_MODELS_CONFIG` at the local file; the adapter uploads it into
   the sandbox and sets `MODELS_CONFIG`. It is pure model metadata (no keys/urls),
   so it leaks nothing into a throwaway container.

   ```bash
   export NOETA_MODELS_CONFIG="$(pwd)/models.json"
   ```

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
harbor run -d terminal-bench/terminal-bench-2-1 -a bench.harbor_adapter:Noeta \
  -m opus4.8 --agent-setup-timeout-multiplier 3 \
  --n-concurrent 1 -i terminal-bench/fix-git
```

### Keep concurrency low on the gateway model

A gateway model can return an empty body on its non-streaming path under load,
which surfaces as `llm_error` / `llm_empty_response` — a gateway-side failure,
not a capability failure, that still scores 0. Keep a scored run at
`--n-concurrent 1–2`.

## Running

The script sources `.env` and sets the sandbox plumbing (wheel, proxy, model
catalogue) itself, so a run is just the **model and effort** you want — nothing
else to export:

```bash
# Prove the loop first (one task, cheap):
NOETA_MODEL=opus4.8 NOETA_EFFORT=high \
  bench/run_benchmark.sh smoke-tb

# Scored runs (real tokens). TB2.1 is the current public leaderboard:
NOETA_MODEL=opus4.8 NOETA_EFFORT=high \
  bench/run_benchmark.sh tb21-sample40    # pinned 40-task stratified sample
NOETA_MODEL=opus4.8 NOETA_EFFORT=xhigh \
  bench/run_benchmark.sh tb21-full        # full 89-set (match the board's xhigh)
NOETA_MODEL=opus4.8 NOETA_EFFORT=high NOETA_CONCURRENCY=1 \
  bench/run_benchmark.sh swe-15           # SWE-bench Verified 15-instance subset
```

Modes: `smoke-tb` · `smoke-swe` · `tb21-sample40` · `tb21-full` · `tb3-full` ·
`aider-full` · `swe-15` · `swe-60` · `swe-full` · `swe-pro-full` · `swe-subset`.
`preflight` prints the resolved model / effort / concurrency / wheel before it
spends anything — check that line first.

**Effort.** The public TB2.1 leaderboard has an Effort column; its top entries
run `xhigh` (ranks 1–2) or `max`, and none below `high`. For a
leaderboard-comparable number use `NOETA_EFFORT=xhigh`; `high` is the safe
default.

**Concurrency.** Default 3. Keep it at **1–2 for SWE-bench**: those images need
a private 3.12 built with `uv`, and parallel toolchain fetches trip agent-setup
timeouts (raise `NOETA_SETUP_TIMEOUT_MULT=5` too).

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

Each id becomes a `-i/--include-task-name` filter. Record the full id list in
`docs/benchmarks.md` alongside the score. TB2.1 is small enough (89 tasks) to
run in full, so it needs no subset.

## Publishing

After a scored run, summarize into `docs/benchmarks.md`: the score, the model
id, the dataset **name@version**, the exact command, and — for SWE-bench — the
pinned instance-id list. State honestly what was run and what was not; usage /
cost in the JSON is token-derived and is approximate when the gateway model is
not in the SDK's pricing catalog.
