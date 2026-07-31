"""``storage`` — the durable storage backends (sqlite, Postgres).

Storage is NOT a contribution surface (ADR ``plugin-contribution-bundles``,
Alternative 5): the ``(EventLogFull, ContentStore, Dispatcher)`` triple is
the truth substrate every plugin guarantee stands on — a single all-or-none
host injection through ``HostConfig`` with no merge semantics, so the plugin
mechanism (activation, collision, ordering) adds nothing. This built-in is
never activated and never enters agent identity; it exists to house the
durable backend impls. This is a declaration-only reference manifest; the
backend factory refs are documented for host discovery:

* ``noeta.builtins.storage.impl.sqlite.stack:build_stack``
* ``noeta.builtins.storage.impl.postgres.stack:build_stack``

The only doorway is :mod:`noeta.sdk.storage` (lazy re-exports + stack
builders); the host wires the built triple via ``HostConfig``.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="storage", requires_noeta=">=0.4")
