"""``noeta.read_models`` — fold-based read projections for UI surfaces (CW5a).

A read-only layer that turns the EventLog into the summary shapes the Web
surfaces (and the ``GET /tasks`` HTTP endpoint) display — WITHOUT any of them
reaching into storage-adapter internals or each other.

Boundary (import-linter, CW5a): depends ONLY on ``noeta.protocols`` (the
``EventLogTaskIndex`` / ``EventLogReader`` / ``ContentStore`` Protocols) and
``noeta.core.fold``. It must NOT import ``noeta.agent``, nor the
concrete ``noeta.storage`` adapters — it talks to storage through Protocols only.
This is the shared seam so no surface owns the read model privately (pre-CW5a the
list lived inside one surface's private helper and poked adapter privates).
"""

from noeta.read_models.sessions import list_session_summaries


__all__ = ["list_session_summaries"]
