"""harbor installed agents for the noeta-vs-Claude-Code benchmark.

`Noeta` runs Noeta 1.0's `noeta -p` in the task container. Modelled on
harbor's own `pi` agent: install the CLI into the sandbox, invoke it once with
the task instruction, tee its JSON lines to `/logs/agent/`, read token usage
back. `ClaudeCode` is harbor's `claude-code` agent with the same stop at the
agent timeout.

Credentials reach the sandbox as `NOETA_PROVIDER` / `NOETA_BASE_URL` /
`NOETA_API_KEY` (read from harbor's environment). A gateway configured the 0.6
way (`LLM_BASE_URL` / `LLM_API_KEY`, an OpenAI-Responses gateway) is mapped to
`openai_responses` at `<LLM_BASE_URL>/responses`. The base URL's host is
declared to harbor, so a task whose sandbox has no internet still lets the
model calls through (harbor's `claude-code` agent does the same with
`ANTHROPIC_BASE_URL`).

    harbor run -p <dataset dir> -a bench.harbor_adapter:Noeta -m <model> --effort high
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

# Default model (NOETA_MODEL, else DeepSeek V4.1 Flash); harbor's -m overrides it.
_DEFAULT_MODEL = os.environ.get("NOETA_MODEL", "deepseek-v4.1-flash")

# The package to install: NOETA_WHEEL (a local wheel, uploaded — how unreleased
# code is benchmarked) or else NOETA_SPEC from the index (e.g. "noeta==1.0.0").
_LOCAL_WHEEL = os.environ.get("NOETA_WHEEL")
_PACKAGE_SPEC = os.environ.get("NOETA_SPEC", "noeta")

# Static (musl) uv and ripgrep binaries uploaded into every sandbox, so the
# install needs nothing from the task image: no apt, no pip, no python of its.
_UV_BIN = os.environ.get("NOETA_UV_BIN")
_RG_BIN = os.environ.get("NOETA_RG_BIN")
# python-build-standalone CPython 3.12 archives, one per libc; the sandbox gets
# the one matching its image, so the install downloads no interpreter.
_PYTHON = {"gnu": os.environ.get("NOETA_PYTHON_GNU"), "musl": os.environ.get("NOETA_PYTHON_MUSL")}

# Everything noeta needs lives under one directory the task never uses: uv, a
# uv-managed CPython, the venv noeta is installed into, `rg`. The task's own
# interpreter and site-packages are never touched, so a verifier that runs in
# the same container (SWE-rebench, TB2.1) tests the environment the task built.
# Only `<home>/bin` (holding `rg` alone) goes on noeta's PATH.
_HOME = "/opt/noeta-bench"
_VENV = f"{_HOME}/venv"
_NOETA = f"{_VENV}/bin/noeta"

# The working directory noeta's tools act on: the container's own WORKDIR
# (`pwd` at run time) unless NOETA_WORKSPACE names one, as harbor's `pi` does.
_WORKSPACE = os.environ.get("NOETA_WORKSPACE")

_OUTPUT_FILENAME = "noeta.json"
_USAGE_FILENAME = "noeta-usage.json"

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
_REMOTE_MODELS_CONFIG = f"{_HOME}/models.json"
_STORE_PATH = "/tmp/noeta-bench/tasks.db"
_REMOTE_STORE = f"sqlite:///{_STORE_PATH}"
_WIRE_FAMILY = {"anthropic": "anthropic", "openai_compat": "openai",
                "openai_responses": "openai"}

# Reasoning effort passed to `noeta -p --effort`: harbor's `--effort` (the
# same flag the claude-code agent takes), else NOETA_EFFORT. Unset, noeta's
# Anthropic wire sends `medium`, which some gateway models refuse (DeepSeek
# V4.1 takes low / high / xhigh / max) — pin it for a scored run.
_EFFORT = os.environ.get("NOETA_EFFORT")

# Run with the venv's python inside the sandbox once the agent has ended: every
# model call of the whole task tree from noeta's own store — the main agent's,
# its sub-agents' (ModelReplied) and the compaction summaries' (Compacted).
_STORE_USAGE_SCRIPT = """
import asyncio, json, sys
from noeta.core import registry
from noeta.core.events import Compacted, ModelReplied

async def main():
    store = registry.store(sys.argv[1])
    out = {"calls": 0, "tasks": 0, "input": 0, "cache_read": 0, "cache_creation": 0, "output": 0}
    try:
        for task in await store.tasks(limit=1_000_000):
            out["tasks"] += 1
            for rec in await store.read(task.task_id):
                ev = rec.event
                if isinstance(ev, ModelReplied):
                    u = ev.response.usage
                elif isinstance(ev, Compacted):
                    u = ev.usage
                else:
                    continue
                out["calls"] += 1
                out["input"] += u.uncached
                out["cache_read"] += u.cache_read
                out["cache_creation"] += u.cache_write
                out["output"] += u.output
    finally:
        await store.close()
    with open(sys.argv[2], "w") as f:
        json.dump(out, f)

asyncio.run(main())
"""


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
    """Token totals from `noeta -p --output-format stream-json` output — the
    fallback when the store export is missing.

    The final `result` line's `modelUsage` covers the task and its sub-agents
    (not compaction). A run harbor killed at its timeout has no `result` line;
    then the usage of each main-agent `assistant` message is summed, once per
    message (sub-agents' and compaction calls are not in the stream). Returns
    `({input, cache_read, cache_creation, output}, source)` or None."""
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
    """HTTP(S) proxy vars for the install commands (downloads of CPython and
    noeta's dependencies). A loopback proxy is useless inside the container;
    NOETA_PROXY names a container-reachable one and overrides; NO_PROXY is
    carried so the gateway is still reached directly."""
    override = os.environ.get("NOETA_PROXY")
    env: dict[str, str] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        raw = os.environ.get(key)
        if override:
            env[key] = override
        elif raw and "127.0.0.1" not in raw and "localhost" not in raw:
            env[key] = raw
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def stop_tree_script(pattern: str) -> str:
    """Shell that stops every process whose command line matches `pattern`
    (a grep regex written so it does not match this script itself) and all
    their descendants: freeze the tree (STOP) so nothing forks away, TERM it,
    let it run, KILL what is left after 5 s (the first round's processes and
    anything new in the tree)."""
    return (
        f"pat={shlex.quote(pattern)}; "
        'hit() { for p in /proc/[0-9]*; do tr "\\0" " " < "$p/cmdline" 2>/dev/null | grep -q -- "$pat" '
        '&& echo "${p#/proc/}"; done; }; '
        'ppid() { sed "s/^.*) //" "/proc/$1/stat" 2>/dev/null | cut -d" " -f2; }; '
        'tree() { all=$(hit); front="$all"; while [ -n "$front" ]; do next=""; '
        'for p in /proc/[0-9]*; do q=${p#/proc/}; pp=$(ppid "$q"); '
        'for f in $front; do [ "$pp" = "$f" ] && next="$next $q"; done; done; '
        'all="$all $next"; front="$next"; done; echo $all; }; '
        'pids=$(tree); [ -n "$pids" ] || exit 0; '
        'kill -STOP $pids 2>/dev/null; kill -TERM $pids 2>/dev/null; kill -CONT $pids 2>/dev/null; '
        # the first round's processes too: a child whose parent died is now
        # PID 1's and no longer in the tree
        'sleep 5; kill -KILL $pids $(tree) 2>/dev/null; true'
    )


async def stop_agent_process(agent: BaseInstalledAgent, environment: BaseEnvironment,
                             pattern: str) -> None:
    """End the agent's CLI process tree inside the sandbox.

    harbor's agent timeout cancels the `docker exec` client, not the process it
    started: the agent (and whatever it launched) keeps running — and editing
    the task — while a same-container verifier runs (SWE-rebench, TB2.1).
    Best-effort."""
    try:
        await asyncio.wait_for(agent.exec_as_root(environment, command=stop_tree_script(pattern)),
                               timeout=60)
    except Exception as exc:  # noqa: BLE001 - the trial is already ending; never mask its error
        agent.logger.warning("could not stop the agent process after the timeout: %s", exc)


class ClaudeCode(HarborClaudeCode):
    """harbor's claude-code agent, stopped at the agent timeout like `Noeta`
    (so both get the same end of run); everything else is harbor's. The
    parent's `run` already renders the prompt template."""

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
        return f"{_NOETA} --help >/dev/null 2>&1 && echo noeta-installed"

    def parse_version(self, stdout: str) -> str:
        # The CLI has no --version; report a stable marker rather than a number.
        return stdout.strip().splitlines()[-1].strip() if stdout.strip() else "unknown"

    async def install(self, environment: BaseEnvironment) -> None:
        """Install noeta into its own venv under `_HOME` with an uploaded
        static uv and a uv-managed CPython 3.12 — never into the task's
        interpreter, never with the image's package manager."""
        tools = {"NOETA_UV_BIN": _UV_BIN, "NOETA_RG_BIN": _RG_BIN,
                 "NOETA_PYTHON_GNU": _PYTHON["gnu"], "NOETA_PYTHON_MUSL": _PYTHON["musl"]}
        for var, local in tools.items():
            if not local or not Path(local).is_file():
                raise RuntimeError(f"{var} unset or missing: the tools uploaded into the sandbox "
                                   "(`bench/compare.py tools <dir>` fetches them)")
        await self.exec_as_root(environment, command=f"mkdir -p {_HOME}/bin {_HOME}/wheel")
        await environment.upload_file(Path(str(_UV_BIN)), f"{_HOME}/uv")
        await environment.upload_file(Path(str(_RG_BIN)), f"{_HOME}/bin/rg")
        libc = await self.exec_as_root(
            environment, command="if ls /lib/ld-musl-* >/dev/null 2>&1; then echo musl; else echo gnu; fi")
        variant = "musl" if "musl" in str(getattr(libc, "stdout", "") or "") else "gnu"
        await environment.upload_file(Path(str(_PYTHON[variant])), f"{_HOME}/python.tar.gz")
        if _LOCAL_WHEEL:
            wheel_path = Path(_LOCAL_WHEEL).expanduser().resolve()
            if not wheel_path.is_file():
                raise RuntimeError(f"NOETA_WHEEL does not exist: {wheel_path}")
            # pip parses name / version / tags out of the filename: keep it.
            target = f"{_HOME}/wheel/{wheel_path.name}"
            await environment.upload_file(wheel_path, target)
            target = shlex.quote(target)
        else:
            target = shlex.quote(_PACKAGE_SPEC)
        uv_env = {
            **_proxy_env(),
            "UV_CACHE_DIR": f"{_HOME}/cache",
            "UV_NO_CONFIG": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
        await self.exec_as_root(
            environment,
            command=(
                "set -eu; "
                f"chmod 755 {_HOME}/uv {_HOME}/bin/rg; "
                f"tar -xzf {_HOME}/python.tar.gz -C {_HOME}; rm -f {_HOME}/python.tar.gz; "
                f"{_HOME}/uv venv {_VENV} --python {_HOME}/python/bin/python3; "
                f"{_HOME}/uv pip install --python {_VENV}/bin/python {target}; "
                f"rm -rf {_HOME}/cache; "
                f"chmod -R a+rX {_HOME}; "
                f"{_NOETA} --help >/dev/null"
            ),
            env=uv_env,
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Invoke `noeta -p --output-format stream-json` once and tee its
        output to the logs directory; afterwards (also after a timeout) export
        the task tree's usage from noeta's store. Credentials come from
        harbor's environment (see the module docstring)."""
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
            f'PATH="{_HOME}/bin:$PATH" {_NOETA} -p {shlex.quote(instruction)} '
            "--output-format stream-json --verbose --permission-mode bypassPermissions "
            f"--cwd {cwd_arg} "
            f"--model {shlex.quote(model)} "
            f"{effort_arg}"
            f"2>&1 | tee /logs/agent/{_OUTPUT_FILENAME}"
        )
        try:
            await self.exec_as_agent(environment, command=command, env=env)
        except asyncio.CancelledError:
            await asyncio.shield(self._finish(environment, timed_out=True))
            raise
        await self._finish(environment, timed_out=False)

    async def _finish(self, environment: BaseEnvironment, *, timed_out: bool) -> None:
        if timed_out:
            await stop_agent_process(self, environment, "bin/[n]oeta -p")
        try:
            await asyncio.wait_for(self.exec_as_root(
                environment,
                command=(f"{_VENV}/bin/python -c {shlex.quote(_STORE_USAGE_SCRIPT)} "
                         f"{shlex.quote(_REMOTE_STORE)} /logs/agent/{_USAGE_FILENAME}")),
                timeout=120)
        except Exception as exc:  # noqa: BLE001 - usage is best-effort; never mask the run's outcome
            self.logger.warning("could not export noeta's usage from its store: %s", exc)

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Token usage in the fields harbor's claude-code agent fills
        (`n_input_tokens` counts every input token, cached or not): from the
        store export when present (`usage_source=store`, the whole tree with
        compaction), else from the stream (see `usage_totals`). No parseable
        usage leaves the counters unset."""
        found: tuple[dict[str, int], str] | None = None
        exported = self.logs_dir / _USAGE_FILENAME
        if exported.is_file():
            try:
                data = json.loads(exported.read_text("utf-8"))
                found = ({k: int(data[k]) for k in ("input", "cache_read", "cache_creation", "output")},
                         "store")
            except (ValueError, KeyError, TypeError):
                found = None
        output_file = self.logs_dir / _OUTPUT_FILENAME
        if found is None and output_file.exists():
            found = usage_totals(output_file.read_text(errors="replace").splitlines())
        if found is None:
            return
        totals, source = found
        context.n_input_tokens = totals["input"] + totals["cache_read"] + totals["cache_creation"]
        context.n_cache_tokens = totals["cache_read"]
        context.n_output_tokens = totals["output"]
        context.metadata = {**(context.metadata or {}), "usage_source": source,
                            "effort": self._effort}
