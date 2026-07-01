"""ContextComposer Protocol.

The main path of a Composer must be deterministic and must not invoke the
LLM, so the same Task always assembles the same View — the prompt prefix
stays byte-stable (stable-prefix prompt cache) and a resume re-derives the
identical View.
"""

from __future__ import annotations

from typing import Protocol

from noeta.protocols.task import Task
from noeta.protocols.view import View


class ContextComposer(Protocol):
    """Assembles a ``View`` for the Policy from a ``Task``."""

    def compose(self, task: Task) -> View: ...
