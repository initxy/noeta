"""L2 runtime wrappers: normal-mode LLM and Tool invocation.

The wrappers exist so that Engine call sites route every LLM / Tool call
through one layer (recording the canonical events into EventLog +
ContentStore).
"""

from __future__ import annotations

__all__: list[str] = []
