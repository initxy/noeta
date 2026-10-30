"""A harbor `BaseInstalledAgent` that runs Noeta 1.0's `noeta -p` in the task
container.

Modelled on harbor's own `pi` agent: install the CLI into the sandbox, invoke
it once with the task instruction, tee its JSON lines to `/logs/agent/`, and
parse token usage back out of the final summary line.

Credentials reach the sandbox as `NOETA_PROVIDER` / `NOETA_BASE_URL` /
`NOETA_API_KEY` (read from harbor's environment). A gateway configured the 0.6
way (`LLM_BASE_URL` / `LLM_API_KEY`, an OpenAI-Responses gateway) is mapped to
`openai_responses` at `<LLM_BASE_URL>/responses`. The base URL's host is
declared to harbor, so a task whose sandbox has no internet still lets the
model calls through (harbor's `claude-code` agent does the same with
`ANTHROPIC_BASE_URL`).

    harbor run -d terminal-bench/terminal-bench@4.0.0 -a bench.harbor_adapter:Noeta \
      -m <model> --effort high
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.agent_auth_resolution import ResolvedModelConnection
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.claude_code import ClaudeCode as HarborClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# Default model. harbor's -m overrides it; this is the default so a
# bare `-a bench.harbor_adapter:Noeta` runs the intended model.
_DEFAULT_MODEL = "opus4.8"

# The package to install into the sandbox. Two paths:
#   - NOETA_WHEEL=/abs/path/to/noeta_agent-*.whl  → upload that local wheel and
#     install it. This is how you benchmark UNRELEASED local code (`uv build
#     --wheel` produces it under dist/). Preferred until noeta 1.0 is on PyPI.
#   - otherwise install NOETA_AGENT_SPEC from the configured index (e.g.
#     "noeta==1.0.0"). Only works once the package is published.
_LOCAL_WHEEL = os.environ.get("NOETA_WHEEL")
_PACKAGE_SPEC = os.environ.get("NOETA_SPEC", "noeta")

# Directory the uploaded wheel lands in inside the sandbox. The wheel keeps its
# original filename there — pip rejects a renamed wheel ("Invalid wheel
# filename") because it parses name-version-tags out of the filename itself.
_REMOTE_WHEEL_DIR = "/tmp/noeta_wheel"

# The working directory Noeta's file/shell tools act on inside the sandbox.
# Most task images use /app, but not all: a handful set a different WORKDIR
# (e.g. /workspace, /app/<subdir>), and pointing Noeta at a non-existent /app
# makes the run start in the wrong place before the agent
# runs. harbor's own `pi` agent sidesteps this by never naming a directory — it
# execs in the container's default WORKDIR. We do the same: when NOETA_WORKSPACE
# is unset, resolve the workspace to the container's `pwd` at run time (the
# image's WORKDIR) rather than assuming /app. An explicit NOETA_WORKSPACE still
# overrides, for an environment that puts the task somewhere the shell can't
# infer.
_WORKSPACE = os.environ.get("NOETA_WORKSPACE")  # None → use the container's pwd

_OUTPUT_FILENAME = "noeta.json"

# Where install() records the absolute path of the `noeta` entry point it ended
# up with, for run() to read back. On a Python >=3.12 image this is just
# `noeta` on PATH; on an older image (SWE-bench's are conda envs on Python
# 3.9–3.11) install() provisions a private 3.12 environment and writes that
# env's `noeta` path here instead. Decoupling install from run through a file
# keeps run() from having to re-derive which interpreter won.
_REMOTE_BIN_MARKER = "/tmp/noeta_bin"

# A model the shipped catalog does not know (any gateway id) runs at noeta's
# conservative fallbacks (context 128000, max output 16384) unless a models
# manifest names its real limits. The manifest is uploaded into the sandbox and
# named by NOETA_MODELS_FILE. Two ways to give one:
#   - NOETA_MODELS_CONFIG=<file>: `{"models": [{"id", "context_window",
#     "max_output_tokens", "family"?, "efforts"?}]}`;
#   - NOETA_MODEL_CONTEXT_WINDOW + NOETA_MODEL_MAX_OUTPUT_TOKENS: a one-row
#     manifest for the `-m` model is written per trial.
# `family` is the wire family (`anthropic` / `openai`), not the vendor: noeta
# refuses any other value at start-up (a DeepSeek model behind an Anthropic
# Messages gateway is `anthropic`). A row without one gets the family of
# NOETA_PROVIDER's wire. The manifest is pure model metadata (no key or URL).
_REMOTE_MODELS_CONFIG = "/tmp/noeta_models.json"
_REMOTE_STORE = "sqlite:////tmp/noeta-bench/tasks.db"
_WIRE_FAMILY = {"anthropic": "anthropic", "openai_compat": "openai",
                "openai_responses": "openai"}

# Reasoning effort passed to `noeta -p --effort`: harbor's `--effort` (the
# same flag the claude-code agent takes), else NOETA_EFFORT. Unset, noeta's
# Anthropic wire sends `medium`, which some gateway models refuse (DeepSeek
# V4.1 takes low / high / xhigh / max) — pin it for a scored run.
_EFFORT = os.environ.get("NOETA_EFFORT")


def wire_family(provider: str | None) -> str:
    """The models-manifest `family` for a noeta provider name."""
    family = _WIRE_FAMILY.get(provider or "anthropic")
    if family is None:
        raise RuntimeError(f"NOETA_PROVIDER={provider!r}: expected one of {sorted(_WIRE_FAMILY)}")
    return family


def models_manifest(model: str, provider: str | None, effort: str | None,
                    env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The manifest to upload, rows checked and missing families filled in;
    None when no manifest is configured."""
    env = dict(os.environ) if env is None else env
    family = wire_family(provider)
    path = env.get("NOETA_MODELS_CONFIG")
    if path:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise RuntimeError(f"NOETA_MODELS_CONFIG does not exist: {source}")
        data = json.loads(source.read_text("utf-8"))
        rows = data.get("models", data) if isinstance(data, dict) else data
    elif env.get("NOETA_MODEL_CONTEXT_WINDOW") and env.get("NOETA_MODEL_MAX_OUTPUT_TOKENS"):
        rows = [{"id": model,
                 "context_window": int(env["NOETA_MODEL_CONTEXT_WINDOW"]),
                 "max_output_tokens": int(env["NOETA_MODEL_MAX_OUTPUT_TOKENS"]),
                 "efforts": [effort] if effort else []}]
    else:
        return None
    if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("id") for r in rows):
        raise RuntimeError("models manifest: expected a list of rows with an `id`")
    out = []
    for row in rows:
        row = {**row, "family": row.get("family") or family}
        if row["family"] not in ("anthropic", "openai"):
            raise RuntimeError(f"models manifest row {row['id']!r}: family {row['family']!r} is not "
                               "a wire family (`anthropic` / `openai`)")
        out.append(row)
    return {"models": out}


def usage_totals(lines: list[str]) -> tuple[dict[str, int], str] | None:
    """Token totals from `noeta -p --output-format stream-json` output.

    The final `result` line's `modelUsage` covers the task and its sub-agents.
    A run harbor killed at its timeout has no `result` line; then the usage of
    each `assistant` message is summed, once per message (a message may span
    several lines). Returns `({input, cache_read, cache_creation, output},
    source)` or None when nothing parses."""
    result: dict[str, Any] | None = None
    per_message: dict[tuple[Any, ...], dict[str, Any]] = {}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "result" and isinstance(payload.get("modelUsage"), dict):
            result = payload["modelUsage"]
        elif payload.get("type") == "assistant" and isinstance(payload.get("message"), dict):
            message = payload["message"]
            usage = message.get("usage")
            if isinstance(usage, dict):
                key = (payload.get("session_id"), payload.get("parent_tool_use_id"),
                       message.get("id") or payload.get("uuid"))
                per_message[key] = usage
    if result is not None:
        rows = [m for m in result.values() if isinstance(m, dict)]

        def total(key: str) -> int:
            return sum(int(m.get(key, 0) or 0) for m in rows)

        return ({"input": total("inputTokens"), "cache_read": total("cacheReadInputTokens"),
                 "cache_creation": total("cacheCreationInputTokens"),
                 "output": total("outputTokens")}, "result")
    if per_message:
        def summed(key: str) -> int:
            return sum(int(u.get(key, 0) or 0) for u in per_message.values())

        return ({"input": summed("input_tokens"), "cache_read": summed("cache_read_input_tokens"),
                 "cache_creation": summed("cache_creation_input_tokens"),
                 "output": summed("output_tokens")}, "per_message")
    return None


def _proxy_env() -> dict[str, str]:
    """HTTP(S) proxy vars to forward into the sandbox.

    The container has no proxy of its own: dockerd's proxy only covers image
    pulls, not processes inside the container. Without this, pip reaches
    pythonhosted.org over a slow NAT path (measured ~25x slower — a 400 KB wheel
    took 19 s vs 0.8 s via proxy), so installing noeta's dependency tree
    (psycopg[binary], agent-sandbox, …) blows past harbor's agent-setup timeout.

    Two rewrites make a host proxy usable *inside* a container:

    - A loopback proxy (``127.0.0.1``/``localhost`` — common for a local proxy
      like clash on :7897) is meaningless in the container, where loopback is
      the container itself: pip gets ``Connection refused``. Rewrite it to
      ``NOETA_PROXY`` when set, else drop it so pip falls back to direct egress.
    - ``NO_PROXY`` is carried so the in-container LLM gateway call (an internal
      host) still bypasses the proxy.

    ``NOETA_PROXY`` lets the operator name a container-reachable proxy
    explicitly (e.g. a corp relay ``http://<proxy-host>:<port>``); it overrides
    whatever loopback value the shell exports.
    """
    override = os.environ.get("NOETA_PROXY")
    env: dict[str, str] = {}

    def _fix(value: str) -> str | None:
        if override:
            return override
        if "127.0.0.1" in value or "localhost" in value:
            # Loopback is unreachable from inside the container; without an
            # override, better to go direct than to hang on a dead proxy.
            return None
        return value

    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        fixed = _fix(raw)
        if fixed:
            env[key] = fixed

    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


async def stop_agent_process(agent: BaseInstalledAgent, environment: BaseEnvironment,
                             pattern: str) -> None:
    """End the agent's CLI process inside the sandbox.

    harbor's agent timeout cancels the `docker exec` client, not the process it
    started: the agent keeps running — and editing the task — while the
    verifier runs (SWE-rebench and TB2.1 verify in the same container). TERM,
    then KILL after 5 s, every process whose command line matches `pattern`
    (a grep regex written so it does not match this script itself), as the
    end of a normal run would. Best-effort."""
    script = (
        f"pat={shlex.quote(pattern)}; "
        'hit() { for p in /proc/[0-9]*; do tr "\\0" " " < "$p/cmdline" 2>/dev/null | grep -q -- "$pat" '
        '&& echo "${p#/proc/}"; done; }; '
        'for p in $(hit); do kill -TERM "$p" 2>/dev/null; done; sleep 5; '
        'for p in $(hit); do kill -KILL "$p" 2>/dev/null; done; true'
    )
    try:
        await asyncio.wait_for(agent.exec_as_root(environment, command=script), timeout=60)
    except Exception as exc:  # noqa: BLE001 - the trial is already ending; never mask its error
        agent.logger.warning("could not stop the agent process after the timeout: %s", exc)


class ClaudeCode(HarborClaudeCode):
    """harbor's claude-code agent, stopped at the agent timeout like `Noeta`
    (so both get the same end of run); everything else is harbor's."""

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        try:
            await super().run(instruction, environment, context)
        except asyncio.CancelledError:
            await asyncio.shield(stop_agent_process(self, environment, "[c]laude --verbose --output-format"))
            raise


class Noeta(BaseInstalledAgent):
    """Noeta as a harbor installed agent."""

    capabilities = AgentCapabilities(reasoning_effort=True)

    def __init__(self, *args: Any, reasoning_effort: str | None = None, **kwargs: Any) -> None:
        self._effort = reasoning_effort or _EFFORT
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "noeta"

    def _credentials(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in ("NOETA_PROVIDER", "NOETA_BASE_URL", "NOETA_API_KEY"):
            value = self._get_env(key)
            if value is not None:
                env[key] = value
        if "NOETA_BASE_URL" not in env and self._get_env("LLM_BASE_URL"):
            env["NOETA_PROVIDER"] = "openai_responses"
            env["NOETA_BASE_URL"] = str(self._get_env("LLM_BASE_URL")).rstrip("/") + "/responses"
            env["NOETA_API_KEY"] = str(self._get_env("LLM_API_KEY") or "")
        return env

    @property
    def model_connection(self) -> ResolvedModelConnection:
        # Read by harbor's trial to allowlist the inference host in a sandbox
        # without internet; noeta itself takes the URL from NOETA_BASE_URL.
        base_url = self._credentials().get("NOETA_BASE_URL")
        if not base_url and (self._credentials().get("NOETA_PROVIDER") or "anthropic") == "anthropic":
            base_url = "https://api.anthropic.com"
        return ResolvedModelConnection(inferred_base_url=base_url)

    def get_version_command(self) -> str | None:
        return "noeta --help >/dev/null 2>&1 && echo noeta-installed"

    def parse_version(self, stdout: str) -> str:
        # The CLI has no --version; report a stable marker rather than a number.
        return stdout.strip().splitlines()[-1].strip() if stdout.strip() else "unknown"

    async def install(self, environment: BaseEnvironment) -> None:
        """Install the `noeta` CLI into the sandbox.

        This harbor build's `BaseInstalledAgent` has no `ensure_system_
        dependencies` helper; the documented path is `exec_as_root` for system
        packages and `exec_as_agent` for user-level installs. So we best-effort
        `apt-get` python3 + pip + ripgrep as root (tolerating an image that
        already has them or has no apt), then install the package.
        `--no-cache-dir` keeps the layer small and avoids a stale wheel across
        reruns. ripgrep is required because noeta shells Grep/Glob
        out to `rg`.

        Package source: a local wheel (NOETA_WHEEL) uploaded into the sandbox
        when set — the path that benchmarks unreleased code — else the
        NOETA_AGENT_SPEC index install.
        """
        proxy = _proxy_env()
        await self.exec_as_root(
            environment,
            command=(
                # python3 + pip for the install below, and ripgrep because
                # noeta shells Grep/Glob out to `rg` — without it
                # those tools fail loud and the agent loses file search. Task
                # images generally ship neither; apt-get best-effort covers the
                # common Debian/Ubuntu base, tolerating an image that has them
                # already or has no apt.
                "need=''; "
                "command -v python3 >/dev/null && python3 -m pip --version >/dev/null 2>&1 || need=\"$need python3 python3-pip\"; "
                "command -v rg >/dev/null || need=\"$need ripgrep\"; "
                "if [ -z \"$need\" ]; then echo 'python3+pip+rg present'; "
                "elif command -v apt-get >/dev/null; then "
                "apt-get update && apt-get install -y $need; "
                "else echo \"no apt-get; missing:$need (relying on preinstalled)\"; fi"
            ),
            env=proxy,
        )

        if _LOCAL_WHEEL:
            wheel_path = Path(_LOCAL_WHEEL).expanduser().resolve()
            if not wheel_path.is_file():
                raise RuntimeError(f"NOETA_WHEEL does not exist: {wheel_path}")
            # Keep the wheel's original filename in the sandbox — pip parses
            # name/version/tags out of it and rejects a renamed wheel.
            remote_wheel = f"{_REMOTE_WHEEL_DIR}/{wheel_path.name}"
            await self.exec_as_agent(
                environment,
                command=f"mkdir -p {shlex.quote(_REMOTE_WHEEL_DIR)}",
            )
            await environment.upload_file(wheel_path, remote_wheel)
            target = shlex.quote(remote_wheel)
        else:
            target = shlex.quote(_PACKAGE_SPEC)

        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                # noeta requires Python >=3.11 (the private env below uses 3.12). Task images vary:
                #  - TB2 images are mostly Python 3.13, so `python3` works.
                #  - SWE-bench images are conda envs on Python 3.9–3.11, where a
                #    bare `python3 -m pip install` fails ("requires a different
                #    Python: 3.11.x not in '>=3.12'"). There, provision a private
                #    3.12 with `uv` (pip-installed with the image's own python)
                #    and install into a uv venv. uv is used rather than
                #    `conda create -n … python=3.12` because conda's solver peaks
                #    well over the trial container's 4 GB cap and gets OOM-killed;
                #    uv downloads a prebuilt CPython and stays comfortably under.
                # Whichever path wins, write the resulting `noeta` entry point's
                # absolute path to a marker file for run() to read. The agent's
                # own shell tools keep using the container's native python (we
                # never touch PATH), so a SWE-bench verifier still tests against
                # the image's original interpreter.
                #
                # --break-system-packages: some Debian images mark the system
                # Python PEP 668 externally-managed; the sandbox is a throwaway
                # container so overriding is safe. Images without the marker
                # ignore it.
                'pyver=$(python3 -c "import sys;print(sys.version_info[:2]>=(3,11))" 2>/dev/null || echo False); '
                'if [ "$pyver" = "True" ]; then '
                f"  python3 -m pip install --no-cache-dir --break-system-packages {target} && "
                '  bin=$(command -v noeta); '
                "else "
                # Older image: pip-install uv with the image's own python, have
                # uv fetch a prebuilt 3.12 and build a venv, install into it.
                '  python3 -m pip install --no-cache-dir --break-system-packages uv >/dev/null 2>&1 || python3 -m pip install --no-cache-dir uv >/dev/null 2>&1; '
                '  UV=$(command -v uv || echo "$(python3 -c "import sys,os;print(os.path.dirname(sys.executable))")/uv"); '
                '  "$UV" python install 3.12 >/dev/null 2>&1; '
                '  "$UV" venv /opt/noeta312 --python 3.12 >/dev/null 2>&1 && '
                f'  "$UV" pip install --python /opt/noeta312/bin/python --no-cache-dir {target} && '
                '  bin=/opt/noeta312/bin/noeta; '
                "fi; "
                f'printf "%s" "$bin" > {shlex.quote(_REMOTE_BIN_MARKER)}; '
                '"$bin" --help >/dev/null'
            ),
            env=proxy,
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Invoke `noeta -p --output-format stream-json` once and tee its
        output to the logs directory; its `result` line carries the usage
        the post-run hook parses. Credentials come from harbor's environment (see the module
        docstring)."""
        model = self.model_name or _DEFAULT_MODEL
        # No proxy here: the run sees the container's own environment, the
        # same one harbor gives every agent; the proxy is for install only.
        env = self._credentials()
        env["NOETA_STORE"] = _REMOTE_STORE
        manifest = models_manifest(model, env.get("NOETA_PROVIDER"), self._effort)
        if manifest is not None:
            local = self.logs_dir / "noeta_models.json"
            local.write_text(json.dumps(manifest, indent=1), "utf-8")
            await environment.upload_file(local, _REMOTE_MODELS_CONFIG)
            env["NOETA_MODELS_FILE"] = _REMOTE_MODELS_CONFIG
            if not any(row["id"] == model for row in manifest["models"]):
                self.logger.warning("models manifest has no row for %s; noeta runs it at its fallbacks", model)
        else:
            self.logger.warning("no models manifest: %s runs at noeta's fallback limits", model)

        cwd_arg = shlex.quote(_WORKSPACE) if _WORKSPACE else '"$(pwd)"'
        effort_arg = f"--effort {shlex.quote(self._effort)} " if self._effort else ""
        command = (
            f'NOETA_BIN=$(cat {shlex.quote(_REMOTE_BIN_MARKER)} 2>/dev/null || echo noeta); '
            f'"$NOETA_BIN" -p {shlex.quote(instruction)} --output-format stream-json --verbose --permission-mode bypassPermissions '
            f"--cwd {cwd_arg} "
            f"--model {shlex.quote(model)} "
            f"{effort_arg}"
            f"2>&1 | tee /logs/agent/{_OUTPUT_FILENAME}"
        )
        try:
            await self.exec_as_agent(environment, command=command, env=env)
        except asyncio.CancelledError:
            await asyncio.shield(stop_agent_process(self, environment, "bin/[n]oeta -p"))
            raise

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Token usage from the CLI's JSON output (see `usage_totals`), in
        the same fields harbor's claude-code agent fills: `n_input_tokens`
        counts every input token, cached or not. The tee above may mix in log
        lines; those are skipped. No parseable usage leaves the counters
        unset."""
        output_file = self.logs_dir / _OUTPUT_FILENAME
        if not output_file.exists():
            return
        found = usage_totals(output_file.read_text(errors="replace").splitlines())
        if found is None:
            return
        totals, source = found
        context.n_input_tokens = totals["input"] + totals["cache_read"] + totals["cache_creation"]
        context.n_cache_tokens = totals["cache_read"]
        context.n_output_tokens = totals["output"]
        context.metadata = {**(context.metadata or {}), "usage_source": source,
                            "effort": self._effort}
