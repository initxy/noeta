"""Compaction: keep a long task inside its model's context window.

See :class:`~noeta.plugins.compaction.extension.Compaction`.
"""

from noeta.plugins.compaction.extension import Compaction, CompactionState

__all__ = ["Compaction", "CompactionState"]
