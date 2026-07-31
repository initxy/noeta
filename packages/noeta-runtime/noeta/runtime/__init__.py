"""Kernel runtime services — the side-effecting layer beneath the Engine.

Every LLM call, tool call, subprocess spawn and background job routes
through one wrapper here so the canonical events land in the EventLog and
ContentStore exactly once. The in-process registries (cancellation,
background shells, file checkpoints) are accelerators only: the event log
stays authoritative, so a resume that folds the log needs none of them.
"""

from __future__ import annotations

__all__: list[str] = []
