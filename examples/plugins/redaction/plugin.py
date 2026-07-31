"""A secret-scrubbing ``tool_result_transform`` stage.

Demonstrated SDK capability: the ``tool_result_transform`` surface. A transform
is a pure ``ToolResult -> ToolResult`` stage applied inside the ToolRuntime
boundary before the result is recorded, so the transformed value *is* what lands
in the EventLog and the ContentStore — a redaction stage means the secret is
never written. It is a pipeline stage, not a hook role: governance has two roles,
Guard and Observer, and this is neither.

Unlike ``guard`` / ``observer`` (process-wide governance), a
``tool_result_transform`` is a **per-agent activation** surface — only an agent
that lists this plugin in ``Options.plugins`` gets the stage. Stages run in
``(priority, plugin, name)`` order.

Purity is the contract: the same ``ToolResult`` must scrub to the same bytes, or
replay diverges and the stable-prefix cache stops holding. The credential
patterns are intentionally conservative — a real deployment tunes the set to the
secret formats its own tools emit.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from noeta.sdk import PluginBuilder, ToolResult


#: The replacement token every matched secret collapses to.
REDACTED = "***REDACTED***"


#: Conservative credential patterns. Each is a whole-token match; a real plugin
#: extends this set with the secret shapes its tools actually emit.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),          # OpenAI-style API keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),              # AWS access key ids
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}"),  # bearer tokens
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
)


def redact_text(text: str) -> str:
    """Replace every matched secret in ``text`` with :data:`REDACTED` (pure)."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def _redact_value(value: Any) -> Any:
    """Recursively scrub strings inside ``output`` (dict / list / str), pure.

    Non-string leaves pass through untouched; the structure is rebuilt so the
    original ``ToolResult.output`` object is never mutated in place.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact_value(v) for v in value]
        return type(value)(rebuilt)
    return value


def scrub_secrets(result: ToolResult) -> ToolResult:
    """The pure ``ToolResult -> ToolResult`` redaction stage.

    Scrubs ``summary`` and ``output``; leaves ``success`` / ``artifacts`` /
    every other field untouched. Returns a new ``ToolResult`` (``dataclasses.replace``)
    — the ToolRuntime records THIS value, so the secret never lands in the ledger.
    """
    return dataclasses.replace(
        result,
        summary=redact_text(result.summary),
        output=_redact_value(result.output),
    )


#: The builder *is* this plugin's manifest. The contributed function is cached
#: for single-file resolution; a distributed install exposes it at
#: ``redaction:scrub_secrets``. ``priority=50`` runs redaction early so a later
#: stage never sees the secret (ties broken by ``(plugin, name)``).
#: ``python -m noeta.sdk.plugin_check`` derives the TOML from this builder and
#: verifies the shipped ``noeta-plugin.toml`` matches.
plugin = PluginBuilder("redaction", requires_noeta=">=0.4")
plugin.tool_result_transform(scrub_secrets, name="scrub-secrets", priority=50)
