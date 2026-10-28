"""``run_skill_script``: run a script an active skill bundles.

Opt-in (``Skills(scripts=True)``) and always asked: the extension's
``before_tool`` denies a call for a skill that is not active or a script it
did not discover, and asks for every other one; there is no allow path. The
argv is built here (interpreter + resolved script path + validated args),
never from a command string. The process is not sandboxed beyond the task's
ExecEnv, so this is for trusted workspaces.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import time
from importlib import resources
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.toolkit.context import task_env
from noeta.toolkit.shell import SHELL_META_CHARS
from noeta.toolkit.tools import path_within, truncate_bytes

__all__ = [
    "SKILL_SCRIPT_TOOL_NAME",
    "RunSkillScriptTool",
    "is_skill_script_resource",
    "script_error",
]

SKILL_SCRIPT_TOOL_NAME = "run_skill_script"

_SCRIPT_MAX_BYTES = 64 * 1024
_INTERPRETER_FOR_SUFFIX: dict[str, str] = {
    ".sh": "bash",
    ".py": "python3",
    ".mjs": "node",
    ".js": "node",
}
_MAX_ARGS = 16
_MAX_ARG_LEN = 4096
_MAX_LISTED_SCRIPTS = 20
_TIMEOUT_S = 120
_OUTPUT_CAP = 256 * 1024
_STDOUT_TAIL_BYTES = 2048
_STDERR_TAIL_BYTES = 1024
_INLINE_OUTPUT_MAX_BYTES = 32 * 1024
_SUMMARY_EMBED_MAX_BYTES = 100


def is_skill_script_resource(relpath: str) -> bool:
    """Whether ``relpath``'s suffix has an allowlisted interpreter."""
    return posixpath.splitext(relpath)[1].lower() in _INTERPRETER_FOR_SUFFIX


def script_error(message: str) -> str:
    return f"{SKILL_SCRIPT_TOOL_NAME}: {message}"


def _err(message: str) -> ToolOutput:
    return text_output(script_error(message), is_error=True)


def _tail(buf: bytes, n: int) -> str:
    return (buf[-n:] if len(buf) > n else buf).decode("utf-8", errors="replace")


def _encoded_len(value: dict[str, Any]) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _fit(output: dict[str, Any], shrink_order: list[str], max_bytes: int) -> dict[str, Any]:
    """Halve one string field per pass, in order, until the output fits."""
    out = dict(output)
    while _encoded_len(out) > max_bytes:
        for key in shrink_order:
            value = out.get(key)
            if isinstance(value, str) and value:
                out[key] = truncate_bytes(value, len(value.encode("utf-8")) // 2)
                break
        else:
            break
    return out


def _description() -> str:
    path = resources.files("noeta.plugins.skills").joinpath("descriptions", "run_skill_script.md")
    return path.read_text(encoding="utf-8").rstrip("\n")


class RunSkillScriptTool:
    """Scripts come from the activation records in the task's state
    (``ctx.state["skills"]["active"][name]["scripts"]``), so only what an
    active skill bundled at activation time can run."""

    name = SKILL_SCRIPT_TOOL_NAME
    read_only = False
    clearable = False

    def __init__(self, extension: str = "skills") -> None:
        self.extension = extension
        self.description = _description()
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Name of the active skill that bundles the script.",
                },
                "relpath": {
                    "type": "string",
                    "description": (
                        "Path to the script relative to that skill's own "
                        "directory (the 'Base directory for this skill' path), "
                        "e.g. 'scripts/check.sh'."
                    ),
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Arguments for the script, one list element each. No "
                        "shell metacharacters; there is no shell."
                    ),
                },
            },
            "required": ["skill", "relpath"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        skill = input.get("skill")
        if not isinstance(skill, str) or not skill:
            return _err("requires a non-empty 'skill'")
        relpath = input.get("relpath")
        if not isinstance(relpath, str) or not relpath:
            return _err("requires a non-empty 'relpath'")
        raw_args = input.get("args", [])
        if not isinstance(raw_args, list) or len(raw_args) > _MAX_ARGS:
            return _err(f"'args' must be a list of <= {_MAX_ARGS} strings")
        for a in raw_args:
            if not isinstance(a, str) or a == "" or len(a) > _MAX_ARG_LEN:
                return _err("each arg must be a non-empty string within length cap")
            if any(c in SHELL_META_CHARS for c in a):
                return _err("args must not contain shell metacharacters")

        state = ctx.state.get(self.extension) or {}
        active = (state.get("active") or {}).get(skill) or {}
        root = active.get("dir")
        scripts = list(active.get("scripts") or [])
        if not root or relpath not in scripts:
            if scripts:
                shown = sorted(scripts)[:_MAX_LISTED_SCRIPTS]
                hint = "discovered scripts for it: " + ", ".join(shown)
                if len(scripts) > _MAX_LISTED_SCRIPTS:
                    hint += f", … ({len(scripts)} total)"
            else:
                hint = f"skill {skill!r} bundles no scripts"
            return _err(f"{relpath!r} is not a discovered script of skill {skill!r}. "
                        "'relpath' must be relative to the skill's own directory; " + hint)
        interpreter = _INTERPRETER_FOR_SUFFIX.get(posixpath.splitext(relpath)[1].lower())
        if interpreter is None:
            return _err(f"no allowlisted interpreter for {relpath!r}")

        env = task_env(ctx.env, {})
        try:
            real_root = await env.realpath(str(root))
            real = await env.realpath(posixpath.join(str(root), relpath))
        except OSError:
            return _err(f"could not resolve {relpath!r}")
        if not path_within(real, real_root):
            return _err(f"{relpath!r} resolves outside its skill root")
        st = await env.stat(real)
        if st is None:
            return _err(f"could not stat {relpath!r}")
        if st.size > _SCRIPT_MAX_BYTES:
            return _err(f"{relpath!r} exceeds the {_SCRIPT_MAX_BYTES}-byte cap")
        try:
            raw = await env.read_bytes(real)
        except OSError:
            return _err(f"could not read {relpath!r}")
        script_hash = hashlib.sha256(raw).hexdigest()

        argv = [interpreter, real, *raw_args]
        started = time.monotonic()
        try:
            outcome = await env.run(argv, cwd=env.cwd, timeout=_TIMEOUT_S,
                                    output_cap=_OUTPUT_CAP)
        except OSError as exc:
            return _err(f"could not execute {interpreter!r}: {exc}")
        duration_ms = int((time.monotonic() - started) * 1000)
        output: dict[str, Any] = {
            "skill": skill,
            "relpath": relpath,
            "interpreter": interpreter,
            "argv": [truncate_bytes(a, 256) for a in argv],
            "cwd": env.cwd,
            "exit_code": outcome.exit_code,
            "duration_ms": duration_ms,
            "resource_hash": script_hash,
            "stdout_tail": _tail(outcome.stdout, _STDOUT_TAIL_BYTES),
            "stderr_tail": _tail(outcome.stderr, _STDERR_TAIL_BYTES),
            "stdout_truncated": outcome.stdout_truncated,
            "stderr_truncated": outcome.stderr_truncated,
            "timed_out": outcome.timed_out,
        }
        output = _fit(output, ["stderr_tail", "stdout_tail", "argv"], _INLINE_OUTPUT_MAX_BYTES)
        body = json.dumps(output, ensure_ascii=False)
        ok = outcome.exit_code == 0 and not outcome.timed_out
        if ok:
            return text_output(body, exit_code=outcome.exit_code)
        status = "timeout" if outcome.timed_out else f"exit={outcome.exit_code}"
        label = truncate_bytes(f"{skill}/{relpath}", _SUMMARY_EMBED_MAX_BYTES)
        summary = f"{self.name} {label} → {status} ({duration_ms}ms)"
        return text_output(f"{summary}\n{body}", is_error=True, exit_code=outcome.exit_code)
