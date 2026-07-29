"""First-party example manifest plugin — ``redaction``: a secret-scrubbing
``tool_result_transform`` stage.

Demonstrated SDK capability
---------------------------
The new ``tool_result_transform`` surface (the SDK-extensibility redesign,
``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``, D9): a
**pure** ``ToolResult -> ToolResult`` stage applied **inside the ToolRuntime
boundary, before recording**. The transformed result *is* the recorded output,
so a redaction stage means the secret never reaches the EventLog or the
ContentStore (acceptance 10). It is a ToolRuntime pipeline stage, **not** a
third hook role — Guard/Observer stays exactly two roles.

Unlike ``guard`` / ``observer`` (governance, process-wide), a
``tool_result_transform`` is a **per-agent activation** surface (spec D6): only
an agent that activates this plugin (``Options.plugins = [... "redaction"]``)
gets the stage. Stages run in ``(priority, plugin, name)`` order. The reference
host activates this plugin, so its recorded tool results are scrubbed
(``examples/reference-host/host.py``).

What it scrubs
--------------
Common credential shapes — provider API keys (``sk-...`` / ``AKIA...``), bearer
tokens, and ``key=value`` secrets — are replaced with :data:`REDACTED` in the
result's ``summary`` and anywhere a string appears in its structured
``output``. The transform is **pure and deterministic**: the same
``ToolResult`` always scrubs to the same bytes (the contract every transform
owes, so replay and the stable-prefix cache are undisturbed). It is intentionally
conservative pattern matching — a real deployment tunes the pattern set to its
own secret formats.
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


#: The single-file manifest (decorator sugar *is* the manifest, spec D1). The
#: contributed function is cached for single-file resolution; a distributed
#: install exposes it at ``redaction:scrub_secrets``. Priority orders this stage
#: among sibling transforms (ties broken by ``(plugin, name)``); redaction runs
#: early so later stages never see the secret. ``python -m noeta.sdk.plugin_check``
#: derives the TOML from this builder and verifies the shipped ``noeta-plugin.toml``.
plugin = PluginBuilder("redaction", requires_noeta=">=0.4")
plugin.tool_result_transform(scrub_secrets, name="scrub-secrets", priority=50)
