"""Small helpers the workspace-context extensions share (instructions,
skills, memory, compaction): the task's ExecEnv, the per-project directory
under noeta's home, the CJK-aware token estimate their budgets are counted
in, and the 1 % resident budget."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from noeta.core.execenv import ExecEnv
from noeta.core.types import Message, ToolResult

if TYPE_CHECKING:
    from noeta.core.extension import TaskView
    from noeta.core.loop.state import TaskState

#: The 1 % of a model's window a resident listing (skills, memory index) may
#: take, and its ceiling: at a 1M-token window the fraction alone would admit
#: ~10K tokens of roster on every request.
RESIDENT_BUDGET_FRACTION = 0.01
RESIDENT_BUDGET_CEILING_TOKENS = 4096
#: The budget when no model is known: 1 % of a 200k-token window.
DEFAULT_RESIDENT_BUDGET_TOKENS = 2000

_CHARS_PER_TOKEN = 4
#: One character = one token for these scripts; ``chars/4`` would undercount
#: a Chinese summary three- to four-fold.
_CJK_CHAR = re.compile(
    "["
    "ᄀ-ᇿ"  # Hangul Jamo
    "　-㏿"  # CJK symbols, kana, bopomofo, compatibility jamo, ...
    "㐀-䶿"  # CJK unified ideographs extension A
    "一-鿿"  # CJK unified ideographs
    "가-힯"  # Hangul syllables
    "豈-﫿"  # CJK compatibility ideographs
    "＀-￯"  # halfwidth and fullwidth forms
    "\U00020000-\U0002ffff"  # CJK unified ideographs extensions B-F
    "]"
)


def estimate_tokens(text: str) -> int:
    """Deterministic, CJK-aware token estimate: CJK characters count one
    each, the rest ``ceil(n / 4)``. A budgeting unit, not a billed count."""
    cjk = len(_CJK_CHAR.findall(text))
    other = len(text) - cjk
    return cjk + (other + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def resident_budget(model: str) -> int:
    """1 % of ``model``'s catalog window, capped at 4,096 tokens."""
    if not model:
        return DEFAULT_RESIDENT_BUDGET_TOKENS
    from noeta.core.models import model_limits

    window, _ = model_limits(model)
    return min(max(1, int(window * RESIDENT_BUDGET_FRACTION)), RESIDENT_BUDGET_CEILING_TOKENS)


def task_env(env: ExecEnv | None, config: Mapping[str, Any]) -> ExecEnv:
    """The task's ExecEnv, or a local one on the task's ``cwd`` when the
    runner has none."""
    if env is not None:
        return env
    from noeta.core import registry

    return registry.execenv("local", str(config.get("cwd") or os.getcwd()))


#: Longest project slug kept whole; a longer one is cut and given a hash.
PROJECT_SLUG_MAX = 200


def noeta_home() -> Path:
    """noeta's config directory: ``$NOETA_HOME``, else ``~/.noeta``."""
    return Path(os.environ.get("NOETA_HOME") or Path.home() / ".noeta")


def plans_dir() -> Path:
    """Where plan mode keeps plan files (Claude Code's ``plans`` directory
    in its config directory): file tools may write there."""
    return noeta_home() / "plans"


def _string_hash(text: str) -> int:
    """The 32-bit string hash Claude Code names long project slugs with
    (over UTF-16 code units)."""
    h = 0
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h = (h * 31 + int.from_bytes(data[i:i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def project_slug(path: str) -> str:
    """``path`` as one directory name, as Claude Code names a project under
    ``projects/``: every character that is not an ASCII letter or digit
    becomes ``-``; past 200 characters the name is cut and a hash added."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", path)
    if len(slug) <= PROJECT_SLUG_MAX:
        return slug
    return f"{slug[:PROJECT_SLUG_MAX]}-{_base36(abs(_string_hash(path)))}"


def project_dir(path: str, home: Path | None = None) -> Path:
    """The per-project directory for the workspace at ``path``:
    ``<home>/projects/<slug>`` (transcripts, auto memory)."""
    return (home or noeta_home()) / "projects" / project_slug(path)


def new_user_messages(state: TaskState, seq: int) -> list[Message]:
    """The person's messages that entered the conversation with event
    ``seq``: taken straight from the inbox, or released then (held behind a
    tool batch, or queued behind the turn). Notices and recalled material
    (``origin`` system / memory) and tool results are not counted."""
    out: list[Message] = []
    for message, at in zip(state.messages, state.message_seqs, strict=False):
        if (at == seq and message.role == "user" and message.origin in (None, "human")
                and message.text and not any(isinstance(b, ToolResult) for b in message.content)):
            out.append(message)
    return out


async def read_text(env: ExecEnv, path: str) -> str | None:
    """``path`` as UTF-8 text, or ``None`` on any read fault (missing, a
    directory, not UTF-8)."""
    try:
        return (await env.read_bytes(path)).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def simulated_state(view: TaskView,
                    reducers: Mapping[str, Callable[[Any, str, Any], Any]] | None = None
                    ) -> TaskState:
    """The task state with every record pending on ``view`` folded in, on a
    copy: what the next fold will see (a compaction made earlier in the same
    ``before_model`` round included). ``reducers`` folds those extensions'
    own records too."""
    import dataclasses

    from noeta.core.events import Recorded
    from noeta.core.loop.state import apply

    state = view.state
    if not view.pending:
        return state
    # Every container is copied (and each call, which ``apply`` updates in
    # place), so folding on the copy never reaches the live state.
    copies: dict[str, Any] = {}
    for f in dataclasses.fields(state):
        value = getattr(state, f.name)
        if f.name == "calls":
            copies[f.name] = [dataclasses.replace(c) for c in value]
        elif f.name == "timers":
            copies[f.name] = {k: dict(v) for k, v in value.items()}
        elif isinstance(value, (list, dict, set)):
            copies[f.name] = type(value)(value)
    sim = dataclasses.replace(state, **copies)
    for i, event in enumerate(view.pending, 1):
        apply(sim, Recorded(seq=state.last_seq + i, at=0.0, event=event), dict(reducers or {}))
    return sim


#: The auto memory index a memory directory keeps, and how much of it is
#: loaded (Claude Code's limits). The memory plugin points the model at the
#: directory; the instructions plugin shows the index with the other
#: instruction files.
MEMORY_INDEX_FILE = "MEMORY.md"
MEMORY_INDEX_MAX_LINES = 200
MEMORY_INDEX_MAX_BYTES = 25_000
_INDEX_CUT = ("> Note: {name} is {size}; only part of it was loaded ({what}). Keep each index "
              "entry to one short line and move details into the memory files.")


def trim_memory_index(text: str) -> str:
    """``MEMORY.md`` within its line and byte limits, with a note when cut."""
    trimmed = text.strip()
    lines = trimmed.split("\n")
    size = len(trimmed.encode("utf-8"))
    over_lines = len(lines) > MEMORY_INDEX_MAX_LINES
    over_bytes = size > MEMORY_INDEX_MAX_BYTES
    if not over_lines and not over_bytes:
        return trimmed
    kept = "\n".join(lines[:MEMORY_INDEX_MAX_LINES])
    if len(kept.encode("utf-8")) > MEMORY_INDEX_MAX_BYTES:
        cut = kept.encode("utf-8")[:MEMORY_INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
        newline = cut.rfind("\n")
        kept = cut[:newline] if newline > 0 else cut
    shown = kept.count("\n") + 1
    if over_lines and not over_bytes:
        size_text = f"{len(lines)} lines (limit {MEMORY_INDEX_MAX_LINES})"
    elif over_bytes and not over_lines:
        size_text = f"{size} bytes (limit {MEMORY_INDEX_MAX_BYTES})"
    else:
        size_text = f"{len(lines)} lines and {size} bytes"
    note = _INDEX_CUT.format(name=MEMORY_INDEX_FILE, size=size_text,
                             what=f"the first {shown} of {len(lines)} lines")
    return f"{kept}\n{note}"


def read_memory_index(directory: str) -> tuple[str, str] | None:
    """``(path, trimmed text)`` of the memory directory's index, or ``None``
    when it is missing, unreadable or blank. Read on this machine: the
    memory directory lives under noeta's home."""
    path = Path(directory) / MEMORY_INDEX_FILE
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    return str(path), trim_memory_index(text)
