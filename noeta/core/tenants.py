"""What a host allows each tenant (``Runtime.tenants``): how many of its
tasks run at once (the claim skips the rest, which wait their turn), a
monthly token and cost budget (a model call past it fails its turn with
``budget_spent``), and its own model provider (its keys)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from noeta.core import models
from noeta.core.provider import Provider
from noeta.core.types import Usage


@dataclass(frozen=True)
class Tenant:
    #: Most tasks of the tenant running at once (sub-agents included).
    max_running: int | None = None
    #: Tokens (input and output) its model calls may use per calendar month (UTC).
    budget_tokens: int | None = None
    #: Dollars its model calls may cost per calendar month (UTC, catalog prices).
    budget_usd: float | None = None
    #: ``provider_for(model)`` for its tasks instead of the host's.
    provider_for: Callable[[str], Provider] | None = None

    @property
    def budgeted(self) -> bool:
        return self.budget_tokens is not None or self.budget_usd is not None


def period(now: float | None = None) -> str:
    """The budget period ``now`` falls in: its UTC month, ``YYYY-MM``."""
    return time.strftime("%Y-%m", time.gmtime(time.time() if now is None else now))


def charged(model: str, usage: Usage) -> tuple[int, float]:
    """What a model call costs a budget: its tokens and catalog price."""
    return usage.input + usage.output, models.price(model, usage) if model else 0.0


def over_budget(name: str, tenant: Tenant, spent: tuple[int, float], when: str) -> str | None:
    """Why ``tenant`` may make no more model calls this period, or ``None``."""
    tokens, cost = spent
    if tenant.budget_tokens is not None and tokens >= tenant.budget_tokens:
        return (f"tenant {name!r} has used its token budget for {when}: {tokens:,} of "
                f"{tenant.budget_tokens:,} tokens")
    if tenant.budget_usd is not None and cost >= tenant.budget_usd:
        return (f"tenant {name!r} has used its cost budget for {when}: ${cost:.2f} of "
                f"${tenant.budget_usd:.2f}")
    return None
