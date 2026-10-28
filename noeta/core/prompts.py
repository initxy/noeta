"""Named prompt texts and their overrides.

Every model-facing text has a name: ``system/<agent>`` (an agent's system
prompt), ``tool/<Tool>`` (a tool's description), and ``<plugin>/<key>`` for
a plugin's own texts (``compaction/summarize``, ``memory/policy``, …). The
owning code holds the default; an override replaces it, and
``<name>.append`` adds text after it.

Overrides come from, lowest first: the host's prompt directories
(``Runtime.prompt_dirs``, e.g. ``~/.noeta/prompts``), the task's workspace
(``<cwd>/.noeta/prompts``, when ``Runtime.workspace_trusted`` allows it),
and the task config's ``prompts`` mapping. A
file ``tool/Read.md`` overrides ``tool/Read``; ``tool/Read.append.md``
appends to it.

The files are read once, when a task first starts, and recorded in its log
(``core`` / ``prompts``), so a resumed task sends the same bytes; an edit
applies to tasks started after it.
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Iterable, Mapping
from pathlib import Path

from noeta.core.execenv import ExecEnv

__all__ = ["APPEND", "WORKSPACE_PROMPTS_DIR", "load_overrides", "resolve"]

#: The suffix of a name whose text is appended to the named text.
APPEND = ".append"
#: Where a workspace keeps its overrides, relative to the task's cwd.
WORKSPACE_PROMPTS_DIR = ".noeta/prompts"
#: Bound on one override file and on how many are read.
_MAX_FILE_BYTES = 256 * 1024
_MAX_FILES = 256


def resolve(overrides: Mapping[str, str], name: str, default: str) -> str:
    """The text of ``name``: its override or ``default``, then whatever
    ``<name>.append`` adds after a blank line."""
    text = overrides.get(name, default)
    extra = overrides.get(name + APPEND, "").strip("\n")
    if not extra:
        return text
    tail = "\n" if text.endswith("\n") else ""
    head = text.rstrip("\n")
    return f"{head}\n\n{extra}{tail}" if head else f"{extra}{tail}"


def _name(rel: str) -> str | None:
    if not rel.endswith(".md"):
        return None
    name = rel[: -len(".md")].strip("/")
    return name or None


def _read_host_dir(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.md"))[:_MAX_FILES]:
        name = _name(path.relative_to(root).as_posix())
        if name is None or not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            continue
        out[name] = path.read_text("utf-8", errors="replace")
    return out


async def _read_env_dir(env: ExecEnv, root: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        st = await env.stat(root)
        if st is None or not st.is_dir:
            return out
        found = await env.run(["find", "-L", root, "-type", "f", "-name", "*.md"], timeout=30,
                              output_cap=1 << 20)
    except Exception:  # noqa: BLE001 — no overrides is the safe reading
        return out
    paths = sorted(p for p in found.stdout.decode("utf-8", "replace").splitlines() if p)
    for path in paths[:_MAX_FILES]:
        name = _name(posixpath.relpath(path, root))
        if name is None:
            continue
        try:
            data = await env.read_bytes(path)
        except OSError:
            continue
        if len(data) <= _MAX_FILE_BYTES:
            out[name] = data.decode("utf-8", errors="replace")
    return out


async def load_overrides(host_dirs: Iterable[str | Path], env: ExecEnv | None,
                         cwd: str, *, workspace: bool = True) -> dict[str, str]:
    """The override files a task starting in ``cwd`` sees: the host's
    directories, then the workspace's (which wins) unless ``workspace`` is
    false (an untrusted workspace)."""
    out: dict[str, str] = {}
    for d in host_dirs:
        out.update(await asyncio.to_thread(_read_host_dir, Path(d).expanduser()))
    if env is not None and cwd and workspace:
        out.update(await _read_env_dir(env, posixpath.join(cwd, WORKSPACE_PROMPTS_DIR)))
    return out
