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
from .schemas import (
    validate_kg_value,
    is_scalar_node,
    SCALAR_NODE_TYPES,
    RUN_SCOPED_NODE_TYPES,
)
from .ingest import (
    DocumentFact,
    IngestResult,
    IngestStatus,
    Layer,
    ingest_fact,
    ingest_facts,
    kg_write,
    SOURCE_TIER_PRECEDENCE,
    MIN_CONFIDENCE_FOR_KG_WRITE,
    NODE_TYPE_LAYER_MAP,
)
from .audit import (
    AuditFinding,
    AuditSeverity,
    run_audit,
    cross_source_consistency,
    staleness_detection,
    orphan_detection,
    entity_coherence,
    hallucination_spot_check,
    get_audit_findings,
    persist_findings,
)

__all__ = [
    "KGCache", "KGNode", "KGEdge", "get_cache",
    "DocumentFact", "IngestResult", "IngestStatus", "Layer",
    "ingest_fact", "ingest_facts", "kg_write",
    "SOURCE_TIER_PRECEDENCE", "MIN_CONFIDENCE_FOR_KG_WRITE", "NODE_TYPE_LAYER_MAP",
    "validate_kg_value", "is_scalar_node",
    "SCALAR_NODE_TYPES", "RUN_SCOPED_NODE_TYPES",
]
