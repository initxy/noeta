"""noeta-agent new backend (T4).

The product's Python backend, rebuilt to eat its own dogfood: it drives agents
**only** through ``noeta.sdk`` (no runtime internals) and owns the HTTP/SSE
bridge to the browser. T8 deleted both the legacy ``noeta.server`` HTTP stack (A)
and the ``noeta.agent.host.session`` runner + its ``noeta.agent.api`` facade (B);
the engine-behaviour suite now assembles through ``noeta.sdk.Client`` (see
``tests/_sdk_session``), the same path this backend uses.

Modules:

* :mod:`noeta.agent.backend.engine_room` — the in-process noeta.sdk engine
  (conversation verbs + canonical EventEnvelope stream). Imports only
  ``noeta.sdk``.
* :mod:`noeta.agent.backend.app` — the HTTP/SSE application + routing root.
* :mod:`noeta.agent.backend.stream` — the SSE multiplexed envelope stream (T5).
* :mod:`noeta.agent.backend.task_protocol` — the eight command endpoints (T5).
* :mod:`noeta.agent.backend.resource_services` — content / files / file (T6).
* :mod:`noeta.agent.backend.mcp_service` — MCP connector management (T6).
* :mod:`noeta.agent.backend.read_views` — capabilities + session-list index.
* :mod:`noeta.agent.backend.lifecycle` — process boot / serve / drain.
"""

from __future__ import annotations

from noeta.agent.backend.engine_room import EngineRoom
from noeta.agent.backend.lifecycle import BackendConfig, serve_backend


__all__ = ["BackendConfig", "EngineRoom", "serve_backend"]
