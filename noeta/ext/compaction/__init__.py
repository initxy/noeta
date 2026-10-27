"""Compaction: keep a long task inside its model's context window.

See :class:`~noeta.ext.compaction.extension.Compaction`.
"""

from noeta.ext.compaction.extension import Compaction, CompactionState

__all__ = ["Compaction", "CompactionState"]
