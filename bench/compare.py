#!/usr/bin/env python3
"""noeta and Claude Code on one harbor job: same model, same effort, same
tasks, same timeouts, one concurrency pool split evenly between the two.

    bench/compare.py run tb4 --k 1 --concurrency 8
    bench/compare.py run swe-rebench --k 3 --concurrency 16
    bench/compare.py run tb4 --tasks music-harmony photonic-waveguide-routing \\
        --agent-max-timeout-sec 1800           # a 30-minute cap instead of 8 h
    bench/compare.py summary <jobs-dir>/<job-name> [<job>...]

Run with harbor's Python (`harbor` on PATH or `--harbor <bin>`). Nothing is
uploaded: harbor's telemetry is switched off here, Claude Code runs with its
telemetry, error reporting, auto-updater and non-essential traffic off, and
`--upload` / `--launch` are refused. noeta sends nothing but model calls.

Credentials: NOETA_BASE_URL / NOETA_API_KEY (or NOETA_LIVE_*), read from the
environment or from NOETA_ENV_FILE (default `<repo>/.env`). Both agents get
the same gateway: noeta through its Anthropic wire, Claude Code as
ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# key -> (harbor registry id, tasks excluded). Runs read a local copy,
# <datasets-dir>/<key>, fetched once with `harbor download <id> -o <that dir>`:
# a run from the registry (`-d`) asks harbor's hub and records a download there.
DATASETS = {
    # 66 tasks; the three that need a GPU cannot run on a CPU host.
    "tb4": ("terminal-bench/terminal-bench@4.0.0",
            ["math-eval-grader", "jax-speedrun-gpu", "fp8-rmsnorm-gemm"]),
    # 111 tasks, issues from 2026-05-15 to 07-01.
    "swe-rebench": ("ibragim-badertdinov/swe-rebench-07-2026", []),
    # The cheap loop: short tasks, for smoke and gateway-concurrency probes.
    "tb21": ("terminal-bench/terminal-bench-2-1", []),
}
DEFAULT_MODEL = "opensource/deepseek_v4_1_flash"
# Model limits for the manifest noeta needs and Claude Code's output cap.
MODEL_LIMITS = {DEFAULT_MODEL: (1_000_000, 65_536)}
CLAUDE_CODE_VERSION = "2.1.312"
CLAUDE_CODE_PRIVACY_ENV = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}
# DeepSeek V4.1-Flash first-party list price, USD per 1M tokens, off-peak;
# peak hours (weekdays 01:00-04:00 and 06:00-10:00 UTC) cost twice as much.
PRICE = {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60}
PEAK_HOURS = set(range(1, 4)) | set(range(6, 10))


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.removeprefix("export ").strip()] = value.strip().strip("'\"")
    return out


def docker_proxy() -> str | None:
    """The proxy the Docker client gives containers, for pip at install."""
    config = Path(os.environ.get("DOCKER_CONFIG", Path.home() / ".docker")) / "config.json"
    try:
        proxies = json.loads(config.read_text("utf-8")).get("proxies", {}).get("default", {})
    except (OSError, ValueError):
        return None
    return proxies.get("httpsProxy") or proxies.get("httpProxy")


def run_env(args: argparse.Namespace, jobs_dir: Path) -> dict[str, str]:
    """The environment harbor runs with; every privacy switch is set here."""
    env = dict(os.environ)
    file_env = load_env_file(Path(os.environ.get("NOETA_ENV_FILE", REPO / ".env")))

    def pick(*names: str) -> str | None:
        for name in names:
            value = env.get(name) or file_env.get(name)
            if value:
                return value
        return None

    base_url = pick("NOETA_BASE_URL", "NOETA_LIVE_BASE_URL")
    api_key = pick("NOETA_API_KEY", "NOETA_LIVE_API_KEY")
    if not base_url or not api_key:
        sys.exit("NOETA_BASE_URL / NOETA_API_KEY (or NOETA_LIVE_*) unset, in env and in NOETA_ENV_FILE")
    context_window, max_output = MODEL_LIMITS.get(args.model, (None, None))
    context_window = args.context_window or context_window
    max_output = args.max_output_tokens or max_output
    env.update({
        "HARBOR_TELEMETRY": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": f"{REPO}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep),
        # harbor's host process never reads the operator's Claude Code config.
        "CLAUDE_CONFIG_DIR": str(jobs_dir / ".claude-config-throwaway"),
        "NOETA_PROVIDER": "anthropic",
        "NOETA_BASE_URL": base_url,
        "NOETA_API_KEY": api_key,
        "NOETA_EFFORT": args.effort,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": api_key,
        **CLAUDE_CODE_PRIVACY_ENV,
    })
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if context_window and max_output:
        env["NOETA_MODEL_CONTEXT_WINDOW"] = str(context_window)
        env["NOETA_MODEL_MAX_OUTPUT_TOKENS"] = str(max_output)
        # harbor's claude-code agent reads this from its own environment.
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output)
    proxy = env.get("NOETA_PROXY") or docker_proxy()
    if proxy:
        env["NOETA_PROXY"] = proxy
    return env


def build_wheel(out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("noeta-*.whl"):
        old.unlink()
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=REPO, check=True)
    return str(next(out.glob("noeta-*.whl")))


def local_dataset(args: argparse.Namespace) -> Path:
    """The directory holding the dataset's task directories."""
    root = Path(args.datasets_dir) / args.dataset
    candidates = [root, *sorted(p for p in root.glob("*") if p.is_dir())] if root.is_dir() else []
    for candidate in candidates:
        if any(candidate.glob("*/task.toml")):
            return candidate.resolve()
    registry_id = DATASETS[args.dataset][0]
    sys.exit(f"no local copy of {registry_id} under {root}; fetch it once:\n"
             f"  HARBOR_TELEMETRY=0 harbor download {registry_id} -o {root}")


def job_config(args: argparse.Namespace, harbor: str, env: dict[str, str]) -> dict[str, Any]:
    _, excluded = DATASETS[args.dataset]
    per_agent = max(1, args.concurrency // len(args.agents))
    cli = [harbor, "run", "-p", str(local_dataset(args)), "-a", "claude-code", "-m", args.model,
           "--effort", args.effort, "-k", str(args.k), "-n", str(args.concurrency),
           "--n-concurrent-agents", str(per_agent),
           "--agent-setup-timeout-multiplier", str(args.setup_timeout_mult),
           "--max-retries", "0", "-o", str(args.jobs_dir), "--job-name", args.job_name,
           "--print-config"]
    if args.agent_timeout_mult is not None:
        cli += ["--agent-timeout-multiplier", str(args.agent_timeout_mult)]
    for name in excluded:
        cli += ["-x", name]
    for name in args.tasks or []:
        cli += ["-i", name.rsplit("/", 1)[-1]]
    printed = subprocess.run(cli, env=env, check=True, capture_output=True, text=True).stdout
    config: dict[str, Any] = json.loads(printed[printed.index("{"):])
    template = config["agents"][0]
    agents = []
    for agent in args.agents:
        row = json.loads(json.dumps(template))
        if agent == "claude-code":
            row["kwargs"]["version"] = CLAUDE_CODE_VERSION
            row["env"] = dict(CLAUDE_CODE_PRIVACY_ENV)
            # harbor's agent, plus the same stop at the agent timeout as noeta
            row["name"] = None
            row["import_path"] = "bench.harbor_adapter:ClaudeCode"
        else:
            row["name"] = None
            row["import_path"] = "bench.harbor_adapter:Noeta"
        if args.agent_max_timeout_sec:
            row["max_timeout_sec"] = args.agent_max_timeout_sec
        agents.append(row)
    config["agents"] = agents
    return config


#: Task images the datasets pull. harbor removes the images it builds, not the
#: ones it pulls, and a full TB4 / SWE-rebench pass pulls far more than a
#: typical disk holds.
TASK_IMAGE_PREFIXES = ("harborframework/terminal-bench", "swerebenchv2/", "docker.io/swerebenchv2/")


def prune_task_images(stop: threading.Event, keep_build_cache_gb: float | None,
                      every_s: float = 600) -> None:
    """Every `every_s`, remove pulled task images no container uses (docker
    refuses to remove one in use). SWE-rebench base images arrive through
    BuildKit and live in the build cache instead; with `keep_build_cache_gb`
    that cache (the whole host's) is pruned down to that size."""
    while not stop.wait(every_s):
        if keep_build_cache_gb is not None:
            subprocess.run(["docker", "builder", "prune", "-f", "--keep-storage",
                            f"{keep_build_cache_gb}gb"], capture_output=True)
        listed = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
                                capture_output=True, text=True).stdout.split("\n")
        ids = {line.split()[-1] for line in listed
               if line.strip() and line.startswith(TASK_IMAGE_PREFIXES)}
        for image_id in ids:
            subprocess.run(["docker", "rmi", image_id], capture_output=True)


def cmd_run(args: argparse.Namespace) -> int:
    harbor = args.harbor or shutil.which("harbor")
    if not harbor:
        sys.exit("harbor not found: put it on PATH or pass --harbor")
    if any(flag in (args.harbor_args or []) for flag in ("--upload", "--launch")):
        sys.exit("refusing --upload / --launch: results stay on this machine")
    args.jobs_dir = Path(args.jobs_dir).resolve()
    args.job_name = args.job_name or (
        f"{args.dataset}-k{args.k}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
    args.jobs_dir.mkdir(parents=True, exist_ok=True)
    env = run_env(args, args.jobs_dir)
    local_dataset(args)
    if "noeta" in args.agents and not env.get("NOETA_WHEEL"):
        env["NOETA_WHEEL"] = build_wheel(args.jobs_dir / ".wheel")
    config = job_config(args, harbor, env)
    config_path = args.jobs_dir / f"{args.job_name}.config.json"
    config_path.write_text(json.dumps(config, indent=2), "utf-8")
    print(f"job config: {config_path}")
    print(f"  model={args.model} effort={args.effort} k={args.k} concurrency={args.concurrency}"
          f" agents={','.join(args.agents)} wheel={env.get('NOETA_WHEEL', '-')}")
    if args.dry_run:
        return 0
    stop = threading.Event()
    if args.prune_images:
        threading.Thread(target=prune_task_images, args=(stop, args.keep_build_cache_gb),
                         daemon=True).start()
    try:
        code = subprocess.run([harbor, "run", "-c", str(config_path), "-y", *(args.harbor_args or [])],
                              env=env).returncode
    finally:
        stop.set()
    job = args.jobs_dir / args.job_name
    if job.is_dir():
        print(summarize([job]))
    return code


# ── summary ──────────────────────────────────────────────────────────────────

def _time(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _span(result: dict[str, Any], phase: str) -> float | None:
    block = result.get(phase) or {}
    start, end = _time(block.get("started_at")), _time(block.get("finished_at"))
    return (end - start).total_seconds() if start and end else None


_ERROR_LINE = re.compile(r"\b429\b|rate.?limit|too many requests|overloaded", re.IGNORECASE)


def api_errors(agent_dir: Path) -> int:
    """Gateway rate-limit / overload signals in an agent's logs: Claude Code's
    retry notices, noeta's stderr. Tool output is not counted."""
    count = 0
    for path in list(agent_dir.glob("*.txt")) + list(agent_dir.glob("*.json")):
        if path.name == "trajectory.json":
            continue
        for line in path.read_text(errors="replace").splitlines():
            if not _ERROR_LINE.search(line):
                continue
            if line.lstrip().startswith("{"):
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("type") != "system" and not payload.get("is_error"):
                    continue
            count += 1
    return count


def trial_rows(job: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(job.glob("*/result.json")):
        result = json.loads(path.read_text("utf-8"))
        agent_result = result.get("agent_result") or {}
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        error = (result.get("exception_info") or {}).get("exception_type")
        n_in = agent_result.get("n_input_tokens") or 0
        n_cache = agent_result.get("n_cache_tokens") or 0
        n_out = agent_result.get("n_output_tokens") or 0
        started = _time((result.get("agent_execution") or {}).get("started_at"))
        peak = started is not None and started.weekday() < 5 and started.hour in PEAK_HOURS
        cost = ((n_in - n_cache) * PRICE["cache_miss"] + n_cache * PRICE["cache_hit"]
                + n_out * PRICE["output"]) / 1e6
        rows.append({
            "agent": (result.get("agent_info") or {}).get("name", "?"),
            "task": result.get("task_name", "?").split("/")[-1],
            "trial": result.get("trial_name"),
            "reward": rewards.get("reward"),
            "error": error,
            "tokens_in": n_in, "tokens_cached": n_cache, "tokens_out": n_out,
            "tokens_reported": agent_result.get("n_input_tokens") is not None,
            "cost_offpeak": cost, "cost_list": cost * (2 if peak else 1),
            "setup_s": _span(result, "agent_setup"), "agent_s": _span(result, "agent_execution"),
            "verify_s": _span(result, "verifier"),
            "started": result.get("started_at"), "finished": result.get("finished_at"),
            "api_errors": api_errors(path.parent / "agent"),
        })
    return rows


def summarize(jobs: list[Path]) -> str:
    rows = [row for job in jobs for row in trial_rows(job)]
    if not rows:
        return "no finished trials"
    out = [f"# Summary: {', '.join(j.name for j in jobs)}", ""]
    starts = [_time(r["started"]) for r in rows if r["started"]]
    ends = [_time(r["finished"]) for r in rows if r["finished"]]
    if starts and ends:
        wall = (max(e for e in ends if e) - min(s for s in starts if s)).total_seconds()
        out.append(f"Wall time (first trial start to last trial end): {wall / 3600:.2f} h")
        out.append("")
    out += ["| agent | trials | passed | pass rate | errors* | pass rate w/o errors | tokens in | cached | out "
            "| cost off-peak | cost list (peak 2x) | mean agent min | sum agent h | 429/overload |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_agent[row["agent"]].append(row)
    for agent, items in sorted(by_agent.items()):
        passed = sum(1 for r in items if (r["reward"] or 0) > 0)
        infra = [r for r in items if r["reward"] is None]
        scored = len(items) - len(infra)
        agent_s = [r["agent_s"] for r in items if r["agent_s"] is not None]
        out.append(
            f"| {agent} | {len(items)} | {passed} | {100 * passed / len(items):.1f}% | {len(infra)} | "
            f"{(100 * passed / scored) if scored else 0:.1f}% | "
            f"{sum(r['tokens_in'] for r in items):,} | {sum(r['tokens_cached'] for r in items):,} | "
            f"{sum(r['tokens_out'] for r in items):,} | ${sum(r['cost_offpeak'] for r in items):.2f} | "
            f"${sum(r['cost_list'] for r in items):.2f} | "
            f"{(sum(agent_s) / len(agent_s) / 60) if agent_s else 0:.1f} | {sum(agent_s) / 3600:.2f} | "
            f"{sum(r['api_errors'] for r in items)} |")
    out += ["", "*errors: trials with no verifier reward (infrastructure failure); a trial that hit the "
            "agent timeout is still graded and counts as a normal pass/fail.", "",
            "## Per task", "",
            "| task | agent | reward | error | tokens in / cached / out | setup s | agent s | verify s |",
            "|---|---|---|---|---|---|---|---|"]
    for row in sorted(rows, key=lambda r: (r["task"], r["agent"], r["trial"] or "")):
        def num(value: float | None) -> str:
            return "-" if value is None else f"{value:.0f}"
        tokens = (f"{row['tokens_in']:,} / {row['tokens_cached']:,} / {row['tokens_out']:,}"
                  if row["tokens_reported"] else "not reported")
        out.append(f"| {row['task']} | {row['agent']} | {row['reward']} | {row['error'] or ''} | {tokens} | "
                   f"{num(row['setup_s'])} | {num(row['agent_s'])} | {num(row['verify_s'])} |")
    text = "\n".join(out) + "\n"
    target = jobs[0] / "summary.md"
    target.write_text(text, "utf-8")
    (jobs[0] / "summary.json").write_text(json.dumps(rows, indent=1), "utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run both agents on one dataset")
    run.add_argument("dataset", choices=sorted(DATASETS))
    run.add_argument("--k", type=int, default=1, help="attempts per task per agent")
    run.add_argument("--concurrency", type=int, default=8, help="trials at once, split evenly between agents")
    run.add_argument("--agents", default="noeta,claude-code",
                     type=lambda s: [a for a in s.split(",") if a])
    run.add_argument("--tasks", nargs="*", help="task names to include (default: the whole dataset)")
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--effort", default="high", help="both agents; DeepSeek V4.1 takes low/high/xhigh/max")
    run.add_argument("--context-window", type=int)
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument("--agent-timeout-mult", type=float, help="scale each task's agent timeout")
    run.add_argument("--agent-max-timeout-sec", type=float, help="cap each task's agent timeout")
    run.add_argument("--setup-timeout-mult", type=float, default=3.0)
    run.add_argument("--jobs-dir", default=str(REPO / "bench" / "jobs"))
    run.add_argument("--datasets-dir", default=str(REPO / "bench" / "datasets"))
    run.add_argument("--job-name")
    run.add_argument("--harbor", help="the harbor executable")
    run.add_argument("--dry-run", action="store_true", help="write the job config, run nothing")
    run.add_argument("--keep-build-cache-gb", type=float,
                     help="with --prune-images: also prune the host's docker build cache to this size")
    run.add_argument("--prune-images", action="store_true",
                     help="every 10 min, remove pulled TB / SWE-rebench task images no container uses")
    run.add_argument("harbor_args", nargs="*", help="after --: extra `harbor run` flags")
    summary = sub.add_parser("summary", help="summarize finished job dir(s)")
    summary.add_argument("jobs", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.command == "summary":
        print(summarize(args.jobs))
        return 0
    unknown = set(args.agents) - {"noeta", "claude-code"}
    if unknown:
        parser.error(f"unknown agents {sorted(unknown)}; use noeta and/or claude-code")
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
