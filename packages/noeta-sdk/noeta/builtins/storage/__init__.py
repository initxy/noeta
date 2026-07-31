"""``storage`` — the durable storage backends (sqlite, Postgres).

Storage is not a contribution surface (ADR ``plugin-contribution-bundles``):
the ``(EventLogFull, ContentStore, Dispatcher)`` triple is the truth substrate
every other plugin guarantee stands on, injected all-or-none by the host
through ``HostConfig``, so activation / collision / ordering semantics would
add nothing. This built-in is therefore never activated and never enters agent
identity — the manifest exists only so the backend implementations have a home,
and hosts reach them through :mod:`noeta.sdk.storage`.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="storage", requires_noeta=">=0.4")
