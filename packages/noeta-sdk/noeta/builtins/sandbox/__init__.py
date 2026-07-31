"""``sandbox`` — the AIO container adapters on the ``sandbox_provider`` surface.

``sandbox_provider`` is host-plane: the host selects which loaded provider to
wire through ``HostConfig`` (``sandbox_provider`` / ``sandbox_backend_factory``
/ ``sandbox_browser_factory``, defaulting to these adapters), so these
declarations never merge onto an ``AgentSpec`` and never follow per-agent
activation. Secrets stay out of the manifest — the adapters take a live auth
strategy at wiring time, never a manifest, config, or ledger value.
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
            "noeta.builtins.sandbox.impl.exec_env:AioSandboxExecEnv",
        ),
        c(
            "sandbox_provider",
            "aio-browser",
            "noeta.builtins.sandbox.impl.browser:AioBrowserBackend",
        ),
    ),
)
