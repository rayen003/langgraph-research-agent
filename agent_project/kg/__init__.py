"""Knowledge Graph module — structured memory for entities, beliefs, runs.

Architecture:
  SQLite (durable)  → primary store; user edits write here directly.
  KGCache (process) → in-memory dict cache; loaded from SQLite at startup.
                      Provides O(1) lookups for the hot DCF cache-check path.

Node ID schemes:
  Shared:     "{ticker}::{node_type}::{field}"
  Run-scoped: "{ticker}::{node_type}::{run_id}::{field}"

Public entry points:
  - ``cache`` — singleton ``KGCache`` instance (load_on_startup before use)
  - ``KGNode`` / ``KGEdge`` — TypedDict shapes
"""

from .cache import KGCache, KGNode, KGEdge, get_cache

__all__ = ["KGCache", "KGNode", "KGEdge", "get_cache"]
