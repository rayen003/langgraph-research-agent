"""Adapter registry — DeckSource.type → SourceAdapter instance.

The graph's normalize stage dispatches via this dict.  Unregistered source
types raise ``KeyError`` at validate time so failures are loud, not silent.

Adding a new adapter:
  1. Implement ``SourceAdapter`` in ``deck/adapters/<type>.py``.
  2. Import + register here.
  3. Extend ``DeckSource`` union in ``deck/state.py``.
"""

from __future__ import annotations

from .base import SourceAdapter, make_block_id
from .chart_artifact import ChartArtifactAdapter
from .dcf_output import DcfOutputAdapter
from .document import DocumentAdapter
from .kg_subgraph import KgSubgraphAdapter
from .manual_text import ManualTextAdapter
from .web import WebAdapter

ADAPTERS: dict[str, SourceAdapter] = {
    "dcf_output":     DcfOutputAdapter(),
    "document":       DocumentAdapter(),
    "manual_text":    ManualTextAdapter(),
    "web":            WebAdapter(),
    "kg_subgraph":    KgSubgraphAdapter(),
    "chart_artifact": ChartArtifactAdapter(),
}


def get_adapter(source_type: str) -> SourceAdapter:
    """Lookup an adapter by source type.  Raises KeyError if not registered."""
    if source_type not in ADAPTERS:
        raise KeyError(
            f"No adapter registered for source type '{source_type}'. "
            f"Known: {sorted(ADAPTERS.keys())}"
        )
    return ADAPTERS[source_type]


__all__ = [
    "ADAPTERS", "get_adapter", "SourceAdapter", "make_block_id",
    "DcfOutputAdapter", "ManualTextAdapter", "DocumentAdapter",
    "WebAdapter", "KgSubgraphAdapter", "ChartArtifactAdapter",
]
