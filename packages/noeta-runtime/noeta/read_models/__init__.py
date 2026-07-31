"""Fold-based read projections a host renders.

The shared seam that turns the EventLog into summary shapes, so no host surface
owns a read model privately and reaches into storage-adapter internals to build
it. Import-linter pins the boundary: this package may import only
``noeta.protocols`` and the one whitelisted ``noeta.core.fold`` edge, so it can
only talk to storage through Protocols.
"""

from noeta.read_models.tasks import list_task_summaries


__all__ = ["list_task_summaries"]
