"""``sandbox`` — the AIO execution adapters as the first ``sandbox_provider``.

The retirement-slated AIO execution adapters, migrated as the first built-in
``sandbox_provider`` plugin (D10). The surface is host-plane and host-wired:
the host SELECTS which loaded provider to wire through ``HostConfig``
(``sandbox_provider`` / ``sandbox_backend_factory`` /
``sandbox_browser_factory``, defaulting to these AIO adapters) — selection is
host wiring, NEVER agent identity, so these declarations are NOT merged onto
any ``AgentSpec`` and never follow per-agent activation. Secrets never appear
here: the adapters take a live ``SandboxAuth`` strategy at wiring time (never a
manifest / config / ledger value). This manifest is the listing / reference
surface — the refs point at the AIO backend + browser adapter classes.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="sandbox",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "sandbox_provider",
            "aio-exec-env",
            "noeta.runtime.exec_env:AioSandboxExecEnv",
        ),
        c(
            "sandbox_provider",
            "aio-browser",
            "noeta.tools.browser:AioBrowserBackend",
        ),
    ),
)
