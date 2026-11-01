#!/usr/bin/env python3
"""noeta and Claude Code on one harbor job: same model, same effort, same
tasks, same timeouts, one concurrency pool split evenly between the two.

    bench/compare.py run tb4 --k 1 --concurrency 8
    bench/compare.py run swe-rebench --k 2 --concurrency 16
    bench/compare.py run tb4 --tasks music-harmony photonic-waveguide-routing \\
        --agent-max-timeout-sec 1800           # a 30-minute cap instead of 8 h
    bench/compare.py summary <jobs-dir>/<job-name> [<job>...]

Run with harbor's Python (`harbor` on PATH or `--harbor <bin>`). Nothing is
uploaded: harbor's telemetry is switched off here, Claude Code runs with its
telemetry, error reporting, auto-updater, non-essential traffic and WebFetch
domain preflight off, and `--upload` / `--launch` are refused. noeta sends
nothing but model calls.

Credentials: NOETA_BASE_URL / NOETA_API_KEY (or NOETA_LIVE_*), read from the
environment or from NOETA_ENV_FILE (default `<repo>/.env`). Both agents get
the same gateway: noeta through its Anthropic wire, Claude Code as
ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY. The key is never printed, and is
replaced by `<redacted>` in everything under the job dir.

Docker: every image a trial uses is built under the trial's own compose
project by a buildx builder this run creates (`bench-<job>`), and harbor
removes each trial's project with its image when the trial ends; the builder,
with all it pulled, is removed when the run ends. Prebuilt task images are
reached through a run-local copy of the dataset whose Dockerfiles are just
`FROM <prebuilt image>`. Nothing the run did not create is removed.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
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
# Model limits: noeta's manifest and Claude Code's context / output caps.
MODEL_LIMITS = {DEFAULT_MODEL: (1_000_000, 65_536)}
CLAUDE_CODE_VERSION = "2.1.312"
CLAUDE_CODE_PRIVACY_ENV = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}
# Claude Code's per-run `--settings` layer: its WebFetch otherwise asks
# api.anthropic.com about every domain it fetches (not covered by the env above).
CLAUDE_CODE_SETTINGS = {"skipWebFetchPreflight": True}

# DeepSeek's own price for the model behind DEFAULT_MODEL (DeepSeek-V4.1-Flash,
# "deepseek-flash"), USD per 1M tokens, off-peak, from
# https://api-docs.deepseek.com/quick_start/pricing/ as read on 2026-10-30.
# Peak rates are twice these, "01:00 - 04:00 and 06:00 - 10:00 UTC, Monday
# through Friday, excluding Chinese public holidays" (holidays not modelled).
# The gateway's own pricing is unknown: an estimate, not a bill.
PRICES = {DEFAULT_MODEL: {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60}}
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/ (read 2026-10-30)"
PEAK_HOURS = set(range(1, 4)) | set(range(6, 10))

# Uploaded into noeta's sandboxes, so its install needs nothing from the task
# image: static (musl) uv and rg, and python-build-standalone CPython for glibc
# and musl images. name -> (url, sha256, member to extract or None to keep the
# archive). The sha256s are the projects' published ones.
_PBS = "https://github.com/astral-sh/python-build-standalone/releases/download/20260814/"
TOOLS = {
    "uv": ("https://github.com/astral-sh/uv/releases/download/0.12.5/uv-x86_64-unknown-linux-musl.tar.gz",
           "a4742988791c9aeae68c78150d6cba762062ad2a47e53738c2779d2b596bfcdb", "uv"),
    "rg": ("https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/"
           "ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz",
           "4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e", "rg"),
    "python-gnu": (_PBS + "cpython-3.12.14%2B20260814-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
                   "5acfa3e9ba26b51ae161c83aff278da915b590d22373a424b2ba55b8afe91fcc", None),
    "python-musl": (_PBS + "cpython-3.12.14%2B20260814-x86_64-unknown-linux-musl-install_only_stripped.tar.gz",
                    "25adbf9d865ada81c728ba9ee6cd042736b6b46dcd3a889198038da04eed9c4a", None),
}
TOOL_ENV = {"uv": "NOETA_UV_BIN", "rg": "NOETA_RG_BIN", "python-gnu": "NOETA_PYTHON_GNU",
            "python-musl": "NOETA_PYTHON_MUSL"}

REDACTED = "<redacted>"


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


def docker_proxies() -> dict[str, str]:
    """The proxies the Docker client gives containers (`~/.docker/config.json`)."""
    config = Path(os.environ.get("DOCKER_CONFIG", Path.home() / ".docker")) / "config.json"
    try:
        proxies = json.loads(config.read_text("utf-8")).get("proxies", {}).get("default", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in proxies.items() if isinstance(v, str) and v}


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
    env.update({
        "HARBOR_TELEMETRY": "0",
        # harbor imports LiteLLM, which otherwise downloads a model price map.
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
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
    context_window, max_output = model_limits(args)
    if context_window and max_output:
        env["NOETA_MODEL_CONTEXT_WINDOW"] = str(context_window)
        env["NOETA_MODEL_MAX_OUTPUT_TOKENS"] = str(max_output)
        # harbor's claude-code agent reads this one from its own environment.
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output)
    proxies = docker_proxies()
    proxy = env.get("NOETA_PROXY") or proxies.get("httpsProxy") or proxies.get("httpProxy")
    if proxy:
        env["NOETA_PROXY"] = proxy
    return env


def model_limits(args: argparse.Namespace) -> tuple[int | None, int | None]:
    context_window, max_output = MODEL_LIMITS.get(args.model, (None, None))
    return (args.context_window or context_window, args.max_output_tokens or max_output)


# ── secrets ──────────────────────────────────────────────────────────────────

def redact_tree(root: Path, secrets: list[str]) -> int:
    """Replace every secret in every file under `root` with `<redacted>`;
    returns the number of files changed. Agents print their environment
    sometimes, and harbor keeps what they print."""
    needles = [s.encode() for s in secrets if s and len(s) >= 8]
    if not needles or not root.exists():
        return 0
    changed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        new = data
        for needle in needles:
            new = new.replace(needle, REDACTED.encode())
        if new != data:
            path.write_bytes(new)
            changed += 1
    return changed


# ── provenance and tools ─────────────────────────────────────────────────────

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_tools(tools_dir: Path) -> dict[str, str]:
    """The uploaded tools, downloaded once and checked by sha256."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, (url, digest, member) in TOOLS.items():
        archive = tools_dir / f"{name}.tar.gz"
        if not archive.is_file() or sha256(archive) != digest:
            urllib.request.urlretrieve(url, archive)
        if sha256(archive) != digest:
            sys.exit(f"{url}: sha256 mismatch")
        if member is None:
            out[name] = str(archive)
            continue
        binary = tools_dir / name
        if not binary.is_file():
            with tarfile.open(archive) as tar:
                found = next(m for m in tar.getmembers() if m.name.endswith(f"/{member}"))
                extracted = tar.extractfile(found)
                assert extracted is not None
                binary.write_bytes(extracted.read())
            binary.chmod(0o755)
        out[name] = str(binary)
    return out


def build_wheel(out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("noeta-*.whl"):
        old.unlink()
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=REPO, check=True)
    return str(next(out.glob("noeta-*.whl")))


def provenance(args: argparse.Namespace, harbor: str, env: dict[str, str], tasks: list[str]) -> dict[str, Any]:
    def run(*cmd: str) -> str:
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True).stdout.strip()

    wheel = env.get("NOETA_WHEEL")
    return {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),  # noqa: UP017 - runs on 3.10 too
        "git_commit": run("git", "rev-parse", "HEAD"),
        "git_dirty": bool(run("git", "status", "--porcelain")),
        "wheel": wheel and {"path": wheel, "sha256": sha256(Path(wheel))},
        "harbor": run(harbor, "--version"),
        "claude_code": CLAUDE_CODE_VERSION,
        "dataset": {"key": args.dataset, "registry_id": DATASETS[args.dataset][0],
                    "local_copy": str(local_dataset(args)), "tasks": tasks},
        "model": args.model, "effort": args.effort, "k": args.k, "concurrency": args.concurrency,
        "model_limits": model_limits(args),
        "agent_timeout_mult": args.agent_timeout_mult, "agent_max_timeout_sec": args.agent_max_timeout_sec,
        "price": {"usd_per_mtok_offpeak": PRICES.get(args.model), "source": PRICE_SOURCE,
                  "peak_hours_utc_weekdays": sorted(PEAK_HOURS)},
        "tools": {name: digest for name, (_, digest, _) in TOOLS.items()},
    }


# ── datasets ─────────────────────────────────────────────────────────────────

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


def selected_tasks(dataset_dir: Path, wanted: list[str] | None, excluded: list[str]) -> list[str]:
    names = sorted(d.name for d in dataset_dir.iterdir() if (d / "task.toml").is_file())
    if wanted:
        wanted = [w.rsplit("/", 1)[-1] for w in wanted]
        missing = sorted(set(wanted) - set(names))
        if missing:
            sys.exit(f"tasks not in the dataset: {missing}")
        names = [n for n in names if n in wanted]
    return [n for n in names if n not in excluded]


_DOCKER_IMAGE_LINE = re.compile(r"^\s*docker_image\s*=")


_COMPOSE_FILES = ("docker-compose.yaml", "docker-compose.yml")
_SIDECAR_DIR = ".bench-images"


def mirror_compose(path: Path) -> list[str]:
    """Rewrite a task's compose file in place so every service is built in
    the trial's project instead of pulled: a service's `image: X` becomes a
    build of `<dir>/.bench-images/<service>/Dockerfile` = `FROM X`. `main`
    loses its `image` (harbor builds it from the environment's Dockerfile);
    a service that both builds and names an image loses the name (compose
    would tag the build with it). Everything else is kept. Returns the
    images now built from."""
    import yaml

    config = yaml.safe_load(path.read_text("utf-8")) or {}
    images: list[str] = []
    for name, service in (config.get("services") or {}).items():
        if not isinstance(service, dict) or "image" not in service:
            continue
        image = str(service.pop("image"))
        if name == "main" or "build" in service:
            continue
        context = path.parent / _SIDECAR_DIR / name
        context.mkdir(parents=True, exist_ok=True)
        (context / "Dockerfile").write_text(f"FROM {image}\n", "utf-8")
        service["build"] = {"context": f"./{_SIDECAR_DIR}/{name}"}
        service.pop("pull_policy", None)
        images.append(image)
    path.write_text(yaml.safe_dump(config, sort_keys=False), "utf-8")
    return images


def mirror_task(src: Path, dst: Path) -> list[str]:
    """Copy one task so that harbor builds, not pulls, its images: each
    `docker_image` in task.toml becomes a Dockerfile `FROM <that image>` (the
    agent environment's in `environment/`, the verifier's in `tests/`), and
    each compose service's image a build of `FROM <that image>`
    (`mirror_compose`), so every image lives in the trial's compose project
    and goes with it. Returns the images it now builds from."""
    import tomllib

    shutil.copytree(src, dst, symlinks=True)
    config = tomllib.loads((src / "task.toml").read_text("utf-8"))
    images = {
        "environment": (config.get("environment") or {}).get("docker_image"),
        "tests": ((config.get("verifier") or {}).get("environment") or {}).get("docker_image"),
    }
    built: list[str] = [i for i in images.values() if i]
    if built:
        lines = (src / "task.toml").read_text("utf-8").splitlines(keepends=True)
        (dst / "task.toml").write_text("".join(x for x in lines if not _DOCKER_IMAGE_LINE.match(x)), "utf-8")
    for subdir, image in images.items():
        if image:
            (dst / subdir).mkdir(exist_ok=True)
            (dst / subdir / "Dockerfile").write_text(f"FROM {image}\n", "utf-8")
    for subdir in ("environment", "tests"):
        for name in _COMPOSE_FILES:
            if (dst / subdir / name).is_file():
                built += mirror_compose(dst / subdir / name)
    return built


def mirror_dataset(src: Path, dst: Path, tasks: list[str]) -> dict[str, list[str]]:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    return {task: mirror_task(src / task, dst / task) for task in tasks}


# ── docker: the run's own builder ────────────────────────────────────────────

class RunBuilder:
    """A buildx builder that only this run uses. Everything it holds — pulled
    base images, build cache — is this run's, so pruning or removing it never
    touches anything else on the host.

    By default a docker-container builder (its state in a docker volume of its
    own). With `state_dir` (a new directory, e.g. on a disk with more room than
    docker's) the run starts its own buildkitd container with that directory
    as its state and attaches a remote builder to it; the directory is emptied
    and removed with the builder. A name already taken (builder, container,
    directory) stops the run: nothing of that name is touched. The buildkit
    image is removed at the end only if it was not on the host before."""

    IMAGE = "moby/buildkit:buildx-stable-1"

    def __init__(self, name: str, state_dir: Path | None = None) -> None:
        self.name = name
        self.state_dir = state_dir
        self.container = f"{name}-buildkitd" if state_dir is not None else None
        self.made: set[str] = set()  # "builder", "container", "state_dir", "image"

    @property
    def created(self) -> bool:
        return "builder" in self.made

    def _proxy_env(self) -> list[tuple[str, str]]:
        proxies = docker_proxies()
        return [(opt, proxies[key]) for key, opt in
                (("httpProxy", "http_proxy"), ("httpsProxy", "https_proxy"), ("noProxy", "no_proxy"))
                if proxies.get(key)]

    @staticmethod
    def _ok(*cmd: str) -> bool:
        return subprocess.run(list(cmd), capture_output=True).returncode == 0

    def _fail(self, why: str) -> None:
        self.remove()  # only what this run made so far
        sys.exit(f"could not create buildx builder {self.name}: {why}")

    def create(self) -> None:
        if self._ok("docker", "buildx", "inspect", self.name):
            sys.exit(f"a buildx builder named {self.name} exists; pick another --job-name")
        if self.container and self._ok("docker", "container", "inspect", self.container):
            sys.exit(f"a container named {self.container} exists; pick another --job-name")
        if self.state_dir is not None and self.state_dir.exists():
            sys.exit(f"builder state dir {self.state_dir} exists: it must be new (it is removed after)")
        image_was_here = self._ok("docker", "image", "inspect", self.IMAGE)
        if self.state_dir is None:
            cmd = ["docker", "buildx", "create", "--name", self.name, "--driver", "docker-container",
                   "--bootstrap"]
            for opt, value in self._proxy_env():
                cmd += ["--driver-opt", f'"env.{opt}={value}"']
        else:
            assert self.container is not None
            self.state_dir.mkdir(parents=True)
            self.made.add("state_dir")
            run = ["docker", "run", "-d", "--name", self.container, "--privileged",
                   "-v", f"{self.state_dir}:/var/lib/buildkit"]
            for opt, value in self._proxy_env():
                run += ["-e", f"{opt}={value}"]
            started = subprocess.run([*run, self.IMAGE], capture_output=True, text=True)
            if not image_was_here and self._ok("docker", "image", "inspect", self.IMAGE):
                self.made.add("image")
            if started.returncode != 0:
                self._fail(started.stderr.strip())
            self.made.add("container")
            cmd = ["docker", "buildx", "create", "--name", self.name, "--driver", "remote",
                   f"docker-container://{self.container}", "--bootstrap"]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if not image_was_here and self._ok("docker", "image", "inspect", self.IMAGE):
            self.made.add("image")
        if done.returncode != 0:
            # Never fall back to the default builder: its cache is the host's.
            if self._ok("docker", "buildx", "inspect", self.name):
                self.made.add("builder")  # half-made by this call: ours to remove
            self._fail(done.stderr.strip())
        self.made.add("builder")

    def prune(self) -> None:
        """Drop what no build of this run is using right now."""
        if self.created:
            subprocess.run(["docker", "buildx", "prune", "--builder", self.name, "-f"], capture_output=True)

    def remove(self) -> None:
        if "builder" in self.made:
            subprocess.run(["docker", "buildx", "rm", self.name], capture_output=True)
        if "container" in self.made and self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
        if "state_dir" in self.made and self.state_dir is not None and self.state_dir.is_dir():
            # buildkitd wrote it as root: empty it from a container, then drop it.
            subprocess.run(["docker", "run", "--rm", "-v", f"{self.state_dir}:/state", "--entrypoint", "sh",
                            self.IMAGE, "-c", "rm -rf /state/* /state/.[!.]*"], capture_output=True)
            shutil.rmtree(self.state_dir, ignore_errors=True)
        if "image" in self.made:
            # pulled by this run; docker refuses while anything uses it
            subprocess.run(["docker", "rmi", self.IMAGE], capture_output=True)
        self.made.clear()


class TrialLeftovers:
    """What harbor can leave behind of this run's trials, removed once each
    trial has finished — only things provably this run's.

    harbor removes a trial's compose project (`down --rmi local --volumes`),
    but an image two trials built alike can stay dangling, and anonymous
    volumes outlive their containers. Ownership: compose labels every image
    it builds with its project, and a project is named after the trial
    (`<trial>__env`, `<trial>__verifier__…`, the trial's random suffix
    included); a volume counts when it was seen mounted in such a container.
    Removal is `docker rmi <id>` / `docker volume rm <name>` without `-f`,
    which docker refuses while anything uses it (or, for an image, while
    another name refers to it)."""

    def __init__(self, job_dir: Path) -> None:
        self.job_dir = job_dir
        self.volumes: dict[str, str] = {}  # volume -> trial
        self.names: dict[str, str] = {}  # container id -> name, from docker events
        self.lock = threading.Lock()
        self.log = job_dir.parent / f"{job_dir.name}.cleanup.log"

    def _note(self, text: str) -> None:
        with self.log.open("a", encoding="utf-8") as out:
            out.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {text}\n")

    def trials(self) -> dict[str, bool]:
        """This job's trials (lower-cased, as compose names them) -> finished."""
        out = {}
        if self.job_dir.is_dir():
            for d in self.job_dir.iterdir():
                if d.is_dir():
                    done = False
                    with contextlib.suppress(OSError, ValueError):
                        done = bool(json.loads((d / "result.json").read_text("utf-8")).get("finished_at"))
                    out[d.name.lower()] = done
        return out

    @staticmethod
    def owner(name: str, trials: dict[str, bool]) -> str | None:
        return next((t for t in trials if name.startswith(f"{t}__")), None)

    def event(self, line: str) -> None:
        """One `docker events` JSON line: remember container names, and every
        volume mounted into one of this job's containers — even a container
        that lives a second."""
        try:
            ev = json.loads(line)
        except ValueError:
            return
        attributes = (ev.get("Actor") or {}).get("Attributes") or {}
        if ev.get("Type") == "container" and ev.get("Action") == "create":
            self.names[(ev.get("Actor") or {}).get("ID", "")] = attributes.get("name", "")
        elif ev.get("Type") == "volume" and ev.get("Action") == "mount":
            name = self.names.get(attributes.get("container", ""), "")
            trial = self.owner(name, self.trials()) if name else None
            if trial is not None:
                with self.lock:
                    self.volumes.setdefault((ev.get("Actor") or {}).get("ID", ""), trial)

    def follow(self, stop: threading.Event) -> subprocess.Popen[str]:
        """Read `docker events` (container creates, volume mounts) until stopped."""
        proc = subprocess.Popen(
            ["docker", "events", "--format", "{{json .}}", "--filter", "type=container",
             "--filter", "type=volume", "--filter", "event=create", "--filter", "event=mount"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

        def read() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                if stop.is_set():
                    break
                self.event(line)

        threading.Thread(target=read, daemon=True).start()
        return proc

    def clean(self, *, everything: bool = False) -> None:
        with self.lock:
            self._clean(everything)

    def _clean(self, everything: bool) -> None:
        trials = self.trials()
        done = {t for t, finished in trials.items() if finished or everything}
        listed = subprocess.run(
            ["docker", "images", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True).stdout.splitlines()
        for line in listed:
            image_id, ref = [*line.split("\t"), ""][:2]
            project = ""
            if ref == "<none>:<none>":
                project = subprocess.run(
                    ["docker", "image", "inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}',
                     image_id], capture_output=True, text=True).stdout.strip()
            if ref == "<none>:<none>":  # dangling: ours only by the compose label
                trial, target = self.owner(project, trials), image_id
            else:  # a name: ours only when the name is a trial's (drops that name alone)
                trial, target = self.owner(ref, trials), ref
            if trial in done and subprocess.run(["docker", "rmi", target], capture_output=True).returncode == 0:
                self._note(f"{trial}: removed image {target} ({image_id}, project {project})")
        for volume, trial in list(self.volumes.items()):
            if trial in done:
                if subprocess.run(["docker", "volume", "rm", volume], capture_output=True).returncode == 0:
                    self._note(f"{trial}: removed volume {volume}")
                    del self.volumes[volume]


def free_gb(path: str = "/var/lib/docker") -> float:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return shutil.disk_usage("/").free / 1e9


def background(stop: threading.Event, builder: RunBuilder, leftovers: TrialLeftovers, job_dir: Path,
               secrets: list[str], prune_below_gb: float, every_s: float = 120) -> None:
    """Every two minutes: remove finished trials' leftovers, redact the key,
    and prune the run's builder only when the disk holding its cache runs
    low — its state dir's, else docker's (a pruned cache makes the next
    trial of a task pull its image again)."""
    cache_disk = str(builder.state_dir) if builder.state_dir is not None else "/var/lib/docker"
    while not stop.wait(every_s):
        leftovers.clean()
        redact_tree(job_dir, secrets)
        if free_gb(cache_disk) < prune_below_gb:
            builder.prune()


# ── run ──────────────────────────────────────────────────────────────────────

def job_config(args: argparse.Namespace, harbor: str, env: dict[str, str], dataset_dir: Path) -> dict[str, Any]:
    per_agent = max(1, args.concurrency // len(args.agents))
    cli = [harbor, "run", "-p", str(dataset_dir), "-a", "claude-code", "-m", args.model,
           "--effort", args.effort, "-k", str(args.k), "-n", str(args.concurrency),
           "--n-concurrent-agents", str(per_agent),
           "--agent-setup-timeout-multiplier", str(args.setup_timeout_mult),
           # every image is built (FROM a prebuilt one, pulled by the run's builder):
           # the same longer build allowance for both agents
           "--environment-build-timeout-multiplier", str(args.build_timeout_mult),
           "--max-retries", "0", "-o", str(args.jobs_dir), "--job-name", args.job_name,
           "--print-config"]
    if args.agent_timeout_mult is not None:
        cli += ["--agent-timeout-multiplier", str(args.agent_timeout_mult)]
    printed = subprocess.run(cli, env=env, check=True, capture_output=True, text=True).stdout
    config: dict[str, Any] = json.loads(printed[printed.index("{"):])
    template = config["agents"][0]
    context_window, max_output = model_limits(args)
    agents = []
    for agent in args.agents:
        row = json.loads(json.dumps(template))
        row["name"] = None
        if agent == "claude-code":
            # harbor's agent, plus the same stop at the agent timeout as noeta
            row["import_path"] = "bench.harbor_adapter:ClaudeCode"
            row["kwargs"]["version"] = CLAUDE_CODE_VERSION
            row["kwargs"]["config"] = dict(CLAUDE_CODE_SETTINGS)
            row["env"] = dict(CLAUDE_CODE_PRIVACY_ENV)
            if context_window:
                # An unknown model's window is otherwise 200k; 2.1.312 honours
                # this for any model id that is not a Claude one.
                row["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context_window)
            if max_output:
                row["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output)
        elif agent == "oracle":
            # harbor's reference solution: checks that a task (and its mirror) grades
            row = {"name": "oracle"}
        else:
            row["import_path"] = "bench.harbor_adapter:Noeta"
        if args.agent_max_timeout_sec:
            row["max_timeout_sec"] = args.agent_max_timeout_sec
        agents.append(row)
    config["agents"] = agents
    return config


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
    job_dir = args.jobs_dir / args.job_name
    env = run_env(args, args.jobs_dir)
    secrets = [env["NOETA_API_KEY"]]
    source = local_dataset(args)
    tasks = selected_tasks(source, args.tasks, [*DATASETS[args.dataset][1], *(args.exclude or [])])
    mirror = args.jobs_dir / f"{args.job_name}.tasks"
    mirror_dataset(source, mirror, tasks)
    if "noeta" in args.agents:
        for name, path in ensure_tools(args.jobs_dir / ".tools").items():
            env[TOOL_ENV[name]] = path
        if not env.get("NOETA_WHEEL"):
            env["NOETA_WHEEL"] = build_wheel(args.jobs_dir / ".wheel")
    builder_name = re.sub(r"[^a-z0-9-]", "-", f"bench-{args.job_name}".lower())[:60]
    builder = RunBuilder(builder_name, Path(args.builder_state_dir).resolve() / builder_name
                         if args.builder_state_dir else None)
    env["BUILDX_BUILDER"] = builder.name
    config = job_config(args, harbor, env, mirror)
    config_path = args.jobs_dir / f"{args.job_name}.config.json"
    config_path.write_text(json.dumps(config, indent=2), "utf-8")
    record = provenance(args, harbor, env, tasks)
    print(f"job config: {config_path}")
    print(f"  model={args.model} effort={args.effort} k={args.k} concurrency={args.concurrency}"
          f" agents={','.join(args.agents)} tasks={len(tasks)} wheel={env.get('NOETA_WHEEL', '-')}")
    if args.dry_run:
        return 0
    builder.create()
    leftovers = TrialLeftovers(job_dir)
    stop = threading.Event()
    events = leftovers.follow(stop)
    worker = threading.Thread(target=background,
                              args=(stop, builder, leftovers, job_dir, secrets, args.prune_below_gb),
                              daemon=True)
    worker.start()
    try:
        code = subprocess.run([harbor, "run", "-c", str(config_path), "-y", *(args.harbor_args or [])],
                              env=env).returncode
    finally:
        stop.set()
        worker.join(timeout=600)  # one cleaner at a time
        builder.remove()
        leftovers.clean(everything=True)
        events.terminate()
        if job_dir.is_dir():
            (job_dir / "provenance.json").write_text(json.dumps(record, indent=1), "utf-8")
            redact_tree(job_dir, secrets)
    if job_dir.is_dir():
        print(summarize([job_dir], model=args.model))
    return code


# ── summary ──────────────────────────────────────────────────────────────────

def _time(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _span(result: dict[str, Any], phase: str) -> float | None:
    block = result.get(phase) or {}
    start, end = _time(block.get("started_at")), _time(block.get("finished_at"))
    return (end - start).total_seconds() if start and end else None


def peak_fraction(start: dt.datetime | None, end: dt.datetime | None) -> float:
    """The share of [start, end] in DeepSeek's peak hours (weekdays, UTC)."""
    if start is None or end is None or end <= start:
        return 0.0
    utc = dt.timezone.utc  # noqa: UP017 - runs on 3.10 too
    start, end = start.astimezone(utc), end.astimezone(utc)
    peak, t, step = 0.0, start, dt.timedelta(minutes=1)
    while t < end:
        nxt = min(t + step, end)
        if t.weekday() < 5 and t.hour in PEAK_HOURS:
            peak += (nxt - t).total_seconds()
        t = nxt
    return peak / (end - start).total_seconds()


def cost(n_in: int, n_cache: int, n_out: int, model: str, peak_share: float) -> float | None:
    """USD at DeepSeek's list price; cache writes cost as misses (DeepSeek
    charges nothing extra for them). None for a model without a price."""
    price = PRICES.get(model)
    if price is None:
        return None
    base = ((n_in - n_cache) * price["cache_miss"] + n_cache * price["cache_hit"]
            + n_out * price["output"]) / 1e6
    return base * (1 + peak_share)


_ERROR_LINE = re.compile(r"\b(429|529)\b|rate[ _-]?limit(?!er\b|ers\b)|too many requests|overloaded", re.IGNORECASE)


def api_errors(agent_dir: Path) -> int:
    """Gateway rate-limit / overload signals in an agent's logs: Claude Code's
    `api_retry` events for HTTP 429 / 529 (or a rate-limit / overload error),
    an error result whose error text names one when no retry event came before
    it (a retry exhaustion is counted once, by its retries), and plain (non-JSON)
    stderr lines. Other stream events are not counted: their text holds token
    counts and the model's thinking, where "429" is just a number. noeta's
    retries are not logged, so its count covers only what it printed on stderr
    and is not a measurement of its retries."""
    count = 0
    for path in list(agent_dir.glob("*.txt")) + list(agent_dir.glob("*.json")):
        if path.name in ("trajectory.json", "noeta-usage.json"):
            continue
        retries = 0
        for line in path.read_text(errors="replace").splitlines():
            if not line.lstrip().startswith("{"):
                count += bool(_ERROR_LINE.search(line))
                continue
            if '"api_retry"' not in line and '"is_error"' not in line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "system" and payload.get("subtype") == "api_retry":
                status = payload.get("error_status")
                if status in (429, 529) or _ERROR_LINE.search(str(payload.get("error") or "")):
                    retries += 1
                    count += 1
            elif payload.get("type") == "result" and payload.get("is_error") and not retries:
                error_text = json.dumps([payload.get("result"), payload.get("errors")])
                count += bool(_ERROR_LINE.search(error_text))
    return count


def outcome(reward: float | None, error: str | None) -> str:
    """pass / fail (graded; a timed-out trial is graded too) / setup-failed
    (the agent could not install — the agent's failure, counted as a fail) /
    infra (no grade for another reason)."""
    if error and error.startswith("AgentSetup"):
        return "setup-failed"
    if reward is None:
        return "infra"
    return "pass" if reward >= 1 else "fail"


def trial_rows(job: Path, model: str = DEFAULT_MODEL) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(job.glob("*/result.json")):
        result = json.loads(path.read_text("utf-8"))
        agent_result = result.get("agent_result") or {}
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        error = (result.get("exception_info") or {}).get("exception_type")
        n_in = agent_result.get("n_input_tokens") or 0
        n_cache = agent_result.get("n_cache_tokens") or 0
        n_out = agent_result.get("n_output_tokens") or 0
        execution = result.get("agent_execution") or {}
        share = peak_fraction(_time(execution.get("started_at")), _time(execution.get("finished_at")))
        reward = rewards.get("reward")
        rows.append({
            "agent": (result.get("agent_info") or {}).get("name", "?"),
            "task": result.get("task_name", "?").split("/")[-1],
            "trial": result.get("trial_name"),
            "reward": reward,
            "outcome": outcome(reward, error),
            "error": error,
            "tokens_in": n_in, "tokens_cached": n_cache, "tokens_out": n_out,
            "tokens_reported": bool(agent_result.get("n_input_tokens")),
            "usage_source": (agent_result.get("metadata") or {}).get("usage_source"),
            "cost_offpeak": cost(n_in, n_cache, n_out, model, 0.0),
            "cost_list": cost(n_in, n_cache, n_out, model, share),
            "setup_s": _span(result, "agent_setup"), "agent_s": _span(result, "agent_execution"),
            "verify_s": _span(result, "verifier"),
            "started": result.get("started_at"), "finished": result.get("finished_at"),
            "api_errors": api_errors(path.parent / "agent"),
        })
    return rows


def paired_difference(rows: list[dict[str, Any]], a: str, b: str) -> tuple[float, float, int] | None:
    """Mean over tasks of (a's mean pass - b's mean pass) on tasks both ran,
    with its standard error and the number of tasks."""
    by: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        by[(row["task"], row["agent"])].append(1.0 if row["outcome"] == "pass" else 0.0)
    tasks = sorted({t for t, _ in by if (t, a) in by and (t, b) in by})
    if len(tasks) < 2:
        return None
    diffs = [sum(by[(t, a)]) / len(by[(t, a)]) - sum(by[(t, b)]) / len(by[(t, b)]) for t in tasks]
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)
    return mean, math.sqrt(var / len(diffs)), len(tasks)


def summarize(jobs: list[Path], model: str = DEFAULT_MODEL, out_dir: Path | None = None) -> str:
    rows = [row for job in jobs for row in trial_rows(job, model)]
    if not rows:
        return "no finished trials"

    def money(value: float | None) -> str:
        return "n/a" if value is None else f"${value:.2f}"

    out = [f"# Summary: {', '.join(j.name for j in jobs)}", ""]
    starts = [t for t in (_time(r["started"]) for r in rows) if t]
    ends = [t for t in (_time(r["finished"]) for r in rows) if t]
    if starts and ends:
        out += [f"Wall time (first trial start to last trial end): {(max(ends) - min(starts)).total_seconds() / 3600:.2f} h", ""]
    out += ["| agent | trials | passed | pass rate | setup failed | infra errors | pass rate w/o infra "
            "| no tokens | tokens in | cached | out | cost off-peak | cost list | mean agent min "
            "| sum agent h | 429/overload |",
            "|" + "---|" * 16]
    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_agent[row["agent"]].append(row)
    for agent, items in sorted(by_agent.items()):
        count = {k: sum(1 for r in items if r["outcome"] == k) for k in ("pass", "setup-failed", "infra")}
        scored = len(items) - count["infra"]
        agent_s = [r["agent_s"] for r in items if r["agent_s"] is not None]
        costs = [r["cost_offpeak"] for r in items]
        lists = [r["cost_list"] for r in items]
        out.append(
            f"| {agent} | {len(items)} | {count['pass']} | {100 * count['pass'] / len(items):.1f}% | "
            f"{count['setup-failed']} | {count['infra']} | "
            f"{(100 * count['pass'] / scored) if scored else 0:.1f}% | "
            f"{sum(1 for r in items if not r['tokens_reported'])} | "
            f"{sum(r['tokens_in'] for r in items):,} | {sum(r['tokens_cached'] for r in items):,} | "
            f"{sum(r['tokens_out'] for r in items):,} | "
            f"{money(None if None in costs else sum(costs))} | {money(None if None in lists else sum(lists))} | "
            f"{(sum(agent_s) / len(agent_s) / 60) if agent_s else 0:.1f} | {sum(agent_s) / 3600:.2f} | "
            f"{sum(r['api_errors'] for r in items)} |")
    agents = sorted(by_agent)
    if len(agents) == 2:
        paired = paired_difference(rows, agents[1], agents[0])
        if paired:
            mean, se, n = paired
            out += ["", f"Paired difference over {n} tasks ({agents[1]} minus {agents[0]}, per-task mean pass): "
                    f"{100 * mean:+.1f} points, standard error {100 * se:.1f}."]
    out += ["", "A pass is reward ≥ 1; a trial that hit the agent timeout is still graded. *setup failed*: "
            "the agent did not install (its failure, counted in the pass rate's denominator). *infra errors*: "
            "no grade for another reason. *no tokens*: trials that reported no usage (their tokens count as 0). "
            "noeta's agent time includes reading its usage from its store after the run (seconds). "
            f"Cost: DeepSeek list price ({PRICE_SOURCE}), peak hours at twice the rate, by the share of each trial's "
            "agent time in them; an estimate, not the gateway's bill.", "",
            "## Per task", "",
            "| task | agent | outcome | reward | error | tokens in / cached / out | usage from | setup s | agent s | verify s |",
            "|" + "---|" * 10]
    for row in sorted(rows, key=lambda r: (r["task"], r["agent"], r["trial"] or "")):
        def num(value: float | None) -> str:
            return "-" if value is None else f"{value:.0f}"
        tokens = (f"{row['tokens_in']:,} / {row['tokens_cached']:,} / {row['tokens_out']:,}"
                  if row["tokens_reported"] else "not reported")
        out.append(f"| {row['task']} | {row['agent']} | {row['outcome']} | {row['reward']} | {row['error'] or ''} | "
                   f"{tokens} | {row['usage_source'] or ''} | "
                   f"{num(row['setup_s'])} | {num(row['agent_s'])} | {num(row['verify_s'])} |")
    text = "\n".join(out) + "\n"
    target = out_dir or jobs[0]
    target.mkdir(parents=True, exist_ok=True)
    (target / "summary.md").write_text(text, "utf-8")
    (target / "summary.json").write_text(json.dumps(rows, indent=1), "utf-8")
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
    run.add_argument("--exclude", nargs="*", help="task names to leave out (e.g. to run large images apart)")
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--effort", default="high", help="both agents; DeepSeek V4.1 takes low/high/xhigh/max")
    run.add_argument("--context-window", type=int)
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument("--agent-timeout-mult", type=float, help="scale each task's agent timeout")
    run.add_argument("--agent-max-timeout-sec", type=float, help="cap each task's agent timeout")
    run.add_argument("--setup-timeout-mult", type=float, default=3.0)
    run.add_argument("--build-timeout-mult", type=float, default=3.0,
                     help="environment build timeout multiplier (pulls of large images)")
    run.add_argument("--prune-below-gb", type=float, default=6.0,
                     help="prune the run's builder cache when the disk holding it has less free")
    run.add_argument("--jobs-dir", default=str(REPO / "bench" / "jobs"))
    run.add_argument("--datasets-dir", default=str(REPO / "bench" / "datasets"))
    run.add_argument("--job-name")
    run.add_argument("--harbor", help="the harbor executable")
    run.add_argument("--dry-run", action="store_true", help="write the job config, run nothing")
    run.add_argument("--builder-state-dir",
                     help="keep the run's buildx builder state in <dir>/<builder> (a disk with room); "
                          "default: a docker volume of the builder's own")
    run.add_argument("harbor_args", nargs="*", help="after --: extra `harbor run` flags")
    summary = sub.add_parser("summary", help="summarize finished job dir(s)")
    summary.add_argument("jobs", nargs="+", type=Path)
    summary.add_argument("--model", default=DEFAULT_MODEL, help="the model the jobs ran (prices it)")
    summary.add_argument("--out", type=Path, help="where to write summary.md/json (default: the first job)")
    tools = sub.add_parser("tools", help="fetch the tools noeta's sandboxes get; print an `export` line")
    tools.add_argument("dir", type=Path)
    redact = sub.add_parser("redact", help="replace the gateway key in every file under a directory")
    redact.add_argument("dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "summary":
        print(summarize(args.jobs, args.model, args.out))
        return 0
    if args.command == "tools":
        found = ensure_tools(args.dir)
        print("export " + " ".join(f"{TOOL_ENV[k]}={v}" for k, v in found.items()))
        return 0
    if args.command == "redact":
        file_env = load_env_file(Path(os.environ.get("NOETA_ENV_FILE", REPO / ".env")))
        keys = [os.environ.get(k) or file_env.get(k) or ""
                for k in ("NOETA_API_KEY", "NOETA_LIVE_API_KEY", "LLM_API_KEY", "ANTHROPIC_API_KEY")]
        redact_tree(args.dir, keys)
        return 0
    unknown = set(args.agents) - {"noeta", "claude-code", "oracle"}
    if unknown:
        parser.error(f"unknown agents {sorted(unknown)}; use noeta, claude-code and/or oracle")
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
