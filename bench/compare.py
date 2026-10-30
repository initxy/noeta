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


def _docker(*argv: str) -> str:
    return subprocess.run(["docker", *argv], capture_output=True, text=True).stdout


def _image_ids() -> set[str]:
    return set(_docker("images", "-a", "-q", "--no-trunc").split())


def _cache_records() -> list[dict[str, Any]]:
    """`docker buildx du --verbose`, one dict per build-cache record."""
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_parents = False
    for line in _docker("buildx", "du", "--verbose").splitlines():
        if line.startswith("ID:"):
            current = {"id": line.split(":", 1)[1].strip(), "parents": set(), "description": ""}
            records.append(current)
            in_parents = False
        elif current is None:
            continue
        elif line.startswith("Parents:"):
            in_parents = True
        elif in_parents and line.lstrip().startswith("- "):
            current["parents"].add(line.strip()[2:].strip())
        else:
            in_parents = False
            if line.startswith("Description:"):
                current["description"] = line.split(":", 1)[1].strip()
    return records


def _qualified(ref: str) -> str:
    """`swerebenchv2/x:tag` -> `docker.io/swerebenchv2/x:tag`, as BuildKit names a pull."""
    first = ref.split("/", 1)[0]
    if "/" not in ref:
        return f"docker.io/library/{ref}"
    if "." in first or ":" in first or first == "localhost":
        return ref
    return f"docker.io/{ref}"


def _pinned(ref: str) -> str:
    """`repo:tag@sha256:…` -> `repo@sha256:…`: docker looks a pull-by-digest up by digest."""
    if "@" not in ref:
        return ref
    name, digest = ref.split("@", 1)
    head, _, tag = name.rpartition(":")
    return f"{head if head and '/' not in tag else name}@{digest}"


def task_image_refs(task_dir: Path) -> list[str]:
    """The images a task's trials pull: the prebuilt agent / verifier images
    task.toml names, or, for a task harbor builds, the base images its
    Dockerfiles start FROM."""
    import tomllib

    refs: list[str] = []
    toml = task_dir / "task.toml"
    if toml.is_file():
        config = tomllib.loads(toml.read_text("utf-8"))
        for table in (config.get("environment", {}), config.get("verifier", {}).get("environment", {})):
            if isinstance(table, dict) and table.get("docker_image"):
                refs.append(str(table["docker_image"]))
    if refs:  # prebuilt images: harbor pulls them and builds nothing
        return refs
    for dockerfile in task_dir.glob("**/Dockerfile"):
        for line in dockerfile.read_text("utf-8", errors="replace").splitlines():
            words = line.split()
            if len(words) >= 2 and words[0].upper() == "FROM":
                ref = next((w for w in words[1:] if not w.startswith("--")), "")
                if ref and "$" not in ref and ref.lower() != "scratch":
                    refs.append(ref)
    return refs


class RunCleanup:
    """Removes, per task, what this run pulled for it — once every trial of
    the task has finished, and never anything else.

    "This run's" means: not on the host when the run started (image IDs and
    build-cache record IDs are recorded then) and named by the task (its
    `docker_image`s and Dockerfile `FROM` images; a build-cache record counts
    when its description is the pull of such an image, or its parent chain
    leads to one). An image a container still uses is never removed (`docker
    rmi` without `-f`). harbor itself removes each trial's containers,
    volumes and built image (`compose down --rmi local --volumes`, scoped to
    the trial's own compose project)."""

    def __init__(self, dataset_dir: Path, job_dir: Path, trials_per_task: int) -> None:
        self.job_dir = job_dir
        self.trials_per_task = trials_per_task
        self.images_before = _image_ids()
        self.cache_before = {r["id"] for r in _cache_records()}
        self.refs = {d.name: task_image_refs(d) for d in dataset_dir.iterdir() if (d / "task.toml").is_file()}
        self.cleaned: set[str] = set()
        self.log = job_dir.parent / f"{job_dir.name}.cleanup.log"

    def _note(self, text: str) -> None:
        with self.log.open("a", encoding="utf-8") as out:
            out.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {text}\n")

    def finished_tasks(self) -> set[str]:
        counts: dict[str, int] = defaultdict(int)
        for path in self.job_dir.glob("*/result.json"):
            try:
                result = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if result.get("finished_at"):
                counts[str(result.get("task_name", "")).split("/")[-1]] += 1
        return {task for task, n in counts.items() if n >= self.trials_per_task}

    def sweep(self, *, all_tasks: bool = False) -> None:
        tasks = set(self.refs) if all_tasks else self.finished_tasks()
        for task in sorted(tasks - self.cleaned):
            if task in self.refs:
                self.clean(task)
            self.cleaned.add(task)

    def clean(self, task: str) -> None:
        refs = self.refs[task]
        for ref in refs:
            image_id = _docker("image", "inspect", "-f", "{{.Id}}", _pinned(ref)).strip()
            if not image_id or image_id in self.images_before:
                continue
            if _docker("ps", "-a", "-q", "--filter", f"ancestor={image_id}").strip():
                self._note(f"{task}: keep {ref} (a container uses it)")
                continue
            subprocess.run(["docker", "rmi", image_id], capture_output=True)
            self._note(f"{task}: removed image {ref} {image_id}")
        pulls = [f"pulled from {_qualified(ref)}" for ref in refs]
        records = [r for r in _cache_records() if r["id"] not in self.cache_before]
        ours = {r["id"] for r in records if any(r["description"].startswith(p) for p in pulls)}
        grew = True
        while grew:  # records built on top of this task's base image
            more = {r["id"] for r in records if r["id"] not in ours and r["parents"] & ours}
            grew = bool(more)
            ours |= more
        left = set(ours)
        for _ in range(len(ours) + 1):  # leaves first: a record with a child cannot go
            if not left:
                break
            parents = {p for r in records if r["id"] in left for p in r["parents"]}
            for record_id in sorted(left - parents or left):
                subprocess.run(["docker", "buildx", "prune", "-f", "--filter", f"id={record_id}"],
                               capture_output=True)
            left &= {r["id"] for r in _cache_records()}
        if ours:
            self._note(f"{task}: removed {len(ours - left)} of {len(ours)} build-cache records"
                       + (f" (left: {sorted(left)})" if left else ""))

    def run_until(self, stop: threading.Event, every_s: float = 120) -> None:
        while not stop.wait(every_s):
            self.sweep()


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
    cleanup = None
    if not args.keep_task_images:
        cleanup = RunCleanup(local_dataset(args), args.jobs_dir / args.job_name,
                             args.k * len(args.agents))
        threading.Thread(target=cleanup.run_until, args=(stop,), daemon=True).start()
    try:
        code = subprocess.run([harbor, "run", "-c", str(config_path), "-y", *(args.harbor_args or [])],
                              env=env).returncode
    finally:
        stop.set()
        if cleanup is not None:
            cleanup.sweep(all_tasks=True)
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
    run.add_argument("--keep-task-images", action="store_true",
                     help="keep the task images this run pulled (default: remove each task's once its "
                          "trials finish; see RunCleanup)")
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
