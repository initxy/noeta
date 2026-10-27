"""Small helpers the workspace-context extensions share (instructions,
skills, memory): the task's ExecEnv, the CJK-aware token estimate their
budgets are counted in, and the 1 % resident budget."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from noeta.execenv.base import ExecEnv
from noeta.types import Message, ToolResult

if TYPE_CHECKING:
    from noeta.loop.state import TaskState

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
    from noeta.models import model_limits

    window, _ = model_limits(model)
    return min(max(1, int(window * RESIDENT_BUDGET_FRACTION)), RESIDENT_BUDGET_CEILING_TOKENS)


def task_env(env: ExecEnv | None, config: Mapping[str, Any]) -> ExecEnv:
    """The task's ExecEnv, or a local one on the task's ``cwd`` when the
    runner has none."""
    if env is not None:
        return env
    from noeta.execenv.local import LocalExecEnv

    local: ExecEnv = LocalExecEnv(str(config.get("cwd") or os.getcwd()))
    return local


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
