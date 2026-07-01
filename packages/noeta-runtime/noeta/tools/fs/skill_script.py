"""``run_skill_script`` — execute a skill's bundled script under
governance (Phase 4.5 Issue E).

A **narrow, opt-in, always-approval** exec tool: it runs a script that a
skill bundled (e.g. ``analyze-sessions.mjs`` / ``scripts/check.sh``), but
only for a script L3 resolved as discovered + in-root, only via an
allowlisted interpreter, only after the ``PermissionGuard`` E precheck +
human approval (Issue A). It is **separate** from ``shell_run`` — its
argv is constructed by Noeta (interpreter + resolved script realpath +
validated args), never from a free-form command string.

Layering: this lives in L2 ``noeta.tools.fs`` and reuses the shell
module's restricted-subprocess primitive. It takes the resolved script
map as **plain ``(skill, relpath, root_path)`` tuples** — it never
imports ``noeta.context.skills``. Honest boundary (same as ``shell_run``):
Noeta does **not** sandbox the spawned process; the hash/ref are the bytes
read just before exec — this does not defend against a concurrent
malicious rewrite. Trusted-workspace only.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools._invocation import require_str
from noeta.tools._limits import (
    INLINE_OUTPUT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools._refs import ref_json
from noeta.tools.fs._subprocess import run_argv, tail_bytes
from noeta.tools.fs.shell import (
    DEFAULT_SHELL_OUTPUT_CAP,
    DEFAULT_SHELL_TIMEOUT_S,
    _SHELL_META_CHARS,
    _STDERR_TAIL_BYTES,
    _STDOUT_TAIL_BYTES,
)
from noeta.tools.fs._workspace import WorkspaceRoot


__all__ = [
    "RunSkillScriptTool",
    "SKILL_SCRIPT_TOOL_NAME",
    "is_skill_script_resource",
]


SKILL_SCRIPT_TOOL_NAME = "run_skill_script"

#: Per-script byte cap (mirrors the write / Issue D resource cap).
_SCRIPT_MAX_BYTES = 64 * 1024
#: Closed suffix → interpreter map. An unknown suffix is refused.
_INTERPRETER_FOR_SUFFIX: dict[str, str] = {
    ".sh": "bash",
    ".py": "python3",
    ".mjs": "node",
    ".js": "node",
}
#: argv hygiene caps — keep the arg list a bounded surface.
_MAX_ARGS = 16
_MAX_ARG_LEN = 4096


def is_skill_script_resource(relpath: str) -> bool:
    """True if ``relpath``'s suffix has an allowlisted interpreter — i.e.
    it is an executable skill script (public seam so L3 need not import a
    private suffix map)."""
    return Path(relpath).suffix.lower() in _INTERPRETER_FOR_SUFFIX


def _has_meta(s: str) -> bool:
    return any(c in _SHELL_META_CHARS for c in s)


def _err(message: str) -> ToolResult:
    return ToolResult(success=False, summary=f"{SKILL_SCRIPT_TOOL_NAME}: {message}")


@dataclass
class RunSkillScriptTool:
    """Execute a discovered skill script via an allowlisted interpreter.

    Constructed with the L3-resolved ``scripts`` map (``(skill, relpath,
    root_path)`` tuples; ``root_path`` is the skill root's absolute
    realpath). The ``PermissionGuard`` already enforced the always-
    approval invariant + active-skill + discovered checks before this
    tool is ever invoked; the tool re-validates defensively and
    re-resolves the realpath at invoke (TOCTOU-aware).
    """

    workspace: WorkspaceRoot
    scripts: tuple[tuple[str, str, Path], ...] = ()
    timeout_s: int = DEFAULT_SHELL_TIMEOUT_S
    output_cap: int = DEFAULT_SHELL_OUTPUT_CAP
    runner: Optional[Callable[..., subprocess.CompletedProcess[bytes]]] = None
    name: str = SKILL_SCRIPT_TOOL_NAME
    description: str = (
        "Run an active skill's bundled script via an allowlisted interpreter "
        "(bash, python3, or node). The script must already be discovered under "
        "an active skill in the workspace; argv is built by Noeta (interpreter + "
        "real path + validated args), never from a free-form command string, "
        "and args may not contain shell metacharacters. cwd is the workspace "
        "root; bounded timeout and output cap apply; not sandboxed (trusted "
        "workspace only)."
    )
    risk_level: str = "high"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "skill": {"type": "string"},
                "relpath": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["skill", "relpath"],
            "additionalProperties": False,
        }
    )

    def _root_for(self, skill: str, relpath: str) -> Optional[Path]:
        for s, rel, root in self.scripts:
            if s == skill and rel == relpath:
                return root
        return None

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        skill = require_str(arguments, "skill", _err, message="requires a non-empty 'skill'")
        if isinstance(skill, ToolResult):
            return skill
        relpath = require_str(
            arguments, "relpath", _err, message="requires a non-empty 'relpath'"
        )
        if isinstance(relpath, ToolResult):
            return relpath
        raw_args = arguments.get("args", [])
        if not isinstance(raw_args, list) or len(raw_args) > _MAX_ARGS:
            return _err(f"'args' must be a list of <= {_MAX_ARGS} strings")
        for a in raw_args:
            if not isinstance(a, str) or a == "" or len(a) > _MAX_ARG_LEN:
                return _err("each arg must be a non-empty string within length cap")
            if _has_meta(a):
                return _err("args must not contain shell metacharacters")

        root = self._root_for(skill, relpath)
        if root is None:
            return _err(f"{skill!r}/{relpath!r} is not a discovered skill script")
        interpreter = _INTERPRETER_FOR_SUFFIX.get(Path(relpath).suffix.lower())
        if interpreter is None:
            return _err(f"no allowlisted interpreter for {relpath!r}")

        # Re-resolve + re-confirm containment at invoke (TOCTOU-aware);
        # use the SAME realpath for hashing + argv.
        candidate = root / relpath
        try:
            real = candidate.resolve()
            if not real.is_relative_to(root):
                return _err(f"{relpath!r} resolves outside its skill root")
        except OSError:
            return _err(f"could not resolve {relpath!r}")
        try:
            size = real.stat().st_size
        except OSError:
            return _err(f"could not stat {relpath!r}")
        if size > _SCRIPT_MAX_BYTES:
            return _err(f"{relpath!r} exceeds the {_SCRIPT_MAX_BYTES}-byte cap")
        try:
            raw = real.read_bytes()
        except OSError:
            return _err(f"could not read {relpath!r}")
        script_hash = hashlib.sha256(raw).hexdigest()

        argv = [interpreter, str(real), *raw_args]
        try:
            outcome = run_argv(
                argv,
                cwd=self.workspace.root,
                timeout_s=self.timeout_s,
                output_cap=self.output_cap,
                runner=self.runner,
            )
        except OSError as exc:
            # interpreter missing / spawn failure → typed failure result,
            # NOT a half-enveloped raise (watchpoint #3).
            return _err(f"could not execute {interpreter!r}: {exc}")

        return self._build_result(
            skill=skill, relpath=relpath, interpreter=interpreter,
            argv=argv, script_hash=script_hash, outcome=outcome, ctx=ctx,
        )

    def _build_result(
        self, *, skill: str, relpath: str, interpreter: str,
        argv: list[str], script_hash: str, outcome: Any, ctx: ToolContext,
    ) -> ToolResult:
        stdout_tail, _ = tail_bytes(outcome.stdout, _STDOUT_TAIL_BYTES)
        stderr_tail, _ = tail_bytes(outcome.stderr, _STDERR_TAIL_BYTES)
        stdout_ref = (
            ctx.artifact_store.put(outcome.stdout, media_type="text/plain")
            if outcome.stdout else None
        )
        stderr_ref = (
            ctx.artifact_store.put(outcome.stderr, media_type="text/plain")
            if outcome.stderr else None
        )
        output: dict[str, Any] = {
            "skill": skill,
            "relpath": relpath,
            "interpreter": interpreter,
            "argv": [truncate_bytes(a, 256) for a in argv],
            "cwd": str(self.workspace.root),
            "exit_code": outcome.returncode,
            "duration_ms": outcome.duration_ms,
            "resource_hash": script_hash,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "stdout_truncated": outcome.stdout_truncated,
            "stderr_truncated": outcome.stderr_truncated,
            "timed_out": outcome.timed_out,
        }
        if stdout_ref is not None:
            output["stdout_ref"] = ref_json(stdout_ref)
        if stderr_ref is not None:
            output["stderr_ref"] = ref_json(stderr_ref)
        output = fit_output_fields(
            output,
            shrink_order=["stderr_tail", "stdout_tail", "argv"],
            max_bytes=INLINE_OUTPUT_MAX_BYTES,
        )
        status = "OK" if outcome.returncode == 0 else f"exit={outcome.returncode}"
        if outcome.timed_out:
            status = "timeout"
        artifacts = [r for r in (stdout_ref, stderr_ref) if r is not None]
        return ToolResult(
            success=True,
            output=output,
            artifacts=artifacts,
            summary=(
                f"{self.name} {truncate_bytes(f'{skill}/{relpath}', SUMMARY_EMBED_MAX_BYTES)}"
                f" → {status} ({outcome.duration_ms}ms)"
            ),
        )
