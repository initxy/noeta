"""The workspace environment block (Claude Code's environment section, line
for line), recorded once at the start of a task as a system-origin message so
it never changes under the prompt cache. Later runs keep the first capture;
the block says so, and the model runs git itself for live state."""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from noeta.ext.base import Extension, TaskView
from noeta.types import user

#: Git status beyond this is cut at a line boundary and marked.
GIT_STATUS_MAX_BYTES = 2048


def _git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=5, check=False)
    except Exception:  # noqa: BLE001 — capture is best-effort
        return ""
    return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else ""


def _git_status(cwd: str) -> str:
    status = _git(cwd, "status", "--short").rstrip("\n")
    encoded = status.encode("utf-8")
    if len(encoded) <= GIT_STATUS_MAX_BYTES:
        return status
    kept = encoded[:GIT_STATUS_MAX_BYTES].decode("utf-8", errors="ignore")
    newline = kept.rfind("\n")
    kept = kept[:newline] if newline >= 0 else ""
    marker = (
        f"({len(kept.splitlines())} of {len(status.splitlines())} changed "
        "paths shown; the status is truncated — run `git status` for the rest.)"
    )
    return f"{kept}\n{marker}" if kept else marker


def _shell() -> str:
    shell = os.environ.get("SHELL") or "unknown"
    return "zsh" if "zsh" in shell else "bash" if "bash" in shell else shell


def render_environment(cwd: str, model: str, *, local: bool = True) -> str:
    is_git = (Path(cwd) / ".git").exists()
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip() if is_git else ""
    status = _git_status(cwd) if is_git else ""
    lines = [
        "<workspace-environment>",
        "# Environment",
        "You have been invoked in the following environment: ",
        f" - Primary working directory: {cwd}",
        f" - Is a git repository: {'true' if is_git else 'false'}",
        f" - Platform: {sys.platform}",
    ]
    if local:
        lines.append(f" - Shell: {_shell()}")
        lines.append(f" - OS Version: {platform.system()} {platform.release()}".rstrip())
    if model:
        lines.append(f"You are powered by the model {model}.")
    lines.append(f"Today's date is {datetime.now().astimezone().date().isoformat()}.")
    lines.append(
        "File paths for read, edit, glob, and grep resolve relative to the "
        "primary working directory."
    )
    if branch:
        lines.append(f"Git branch: {branch}")
    if status:
        lines.append(f"Git status:\n{status}")
    if branch or status:
        lines.append(
            "The git branch/status above are a snapshot from when this "
            "task started; they do not update as the task runs. Run git "
            "commands for the current state."
        )
    lines.append("</workspace-environment>")
    return "\n".join(lines)


class Environment(Extension):
    name = "workspace"

    def __init__(self, *, model_name: bool = True) -> None:
        self.model_name = model_name

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return True if kind == "environment" else state

    async def on_start(self, view: TaskView) -> None:
        if view.ext_state(self.name):
            return
        config = view.state.config
        cwd = str(config.get("cwd") or os.getcwd())
        model = str(config.get("model") or "") if self.model_name else ""
        text = await asyncio.to_thread(render_environment, cwd, model)
        view.record(self.name, "environment", message=user(text, origin="system"))
