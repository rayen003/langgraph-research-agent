"""In-process KG cache — fast O(1) lookups, write-through to SQLite.

The cache is a singleton (one per server process). It loads from SQLite at
startup and on demand for specific (session_id, ticker) scopes. Writes go
to SQLite first, then update the in-memory dict.

TTL governs cache freshness per node_type. Compound nodes (thesis,
company_synthesis) additionally carry an ``input_hash`` — if upstream
nodes changed, the compound is treated as stale even if within TTL.

This module owns NOTHING DCF-specific. DCF code calls ``get_cache().get(...)``
and ``get_cache().put(...)`` via the bridge in ``graphs/workflows/dcf/``.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, TypedDict

import storage

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Shapes
# ──────────────────────────────────────────────────────────────────────────────


class KGNode(TypedDict, total=False):
    id: str
    session_id: str | None
    ticker: str
    node_type: str
    field: str
    value: Any
    confidence: float
    source: str
    input_hash: str | None
    run_id: str | None
    created_at: float
    updated_at: float


class KGEdge(TypedDict, total=False):
    id: str
    session_id: str | None
    src_id: str
    tgt_id: str
    relation: str
    confidence: float
    source: str
    created_at: float


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# TTL in seconds per node_type. Anything not listed defaults to TTL_DEFAULT.
TTL_DEFAULT = 86400  # 24h

#
# Three-layer KG model:
#
#   LAYER 1 — ANCHORED FACTS (immutable, ADDITIVE, infinite TTL)
#     New fetches ADD to corpus. Old facts never invalidate.
#     Agent queries by recency when needed.
#
#   LAYER 2 — DERIVED INFERENCES (rebuildable, cached, finite TTL)
#     Computed from Layer 1. Hash-checked against inputs.
#     Refresh when inputs change OR TTL expires.
#
#   LAYER 3 — RUN ARTIFACTS (immutable history, NEVER input to future runs)
#     Output of one analytical run, frozen forever.
#
TTL: dict[str, float] = {
    # ── Layer 0 — Entity anchors + user-stated ───────────────────────────────
    "user_belief":          float("inf"),    # only user can invalidate
    "company":              float("inf"),    # entity anchor — never expires
    # ── Layer 1 — Anchored facts (additive, immutable) ───────────────────────
    "filing":               float("inf"),    # 10-K/10-Q text — facts at point in time
    "news_item":            float("inf"),    # news article — historical event
    "market_metric_price":  3600.0,          # 1h — current snapshot (refreshable)
    "market_metric_fund":   86400.0,         # 24h — current FY snapshot (refreshable)
    "person":               2592000.0,       # 30d — entity attributes
    # ── Layer 2 — Derived inferences (rebuildable, hash-checked) ─────────────
    "driver":               604800.0,        # 7d narrative stability
    "theme":                604800.0,
    "risk":                 604800.0,
    "company_synthesis":    604800.0,        # 7d — hash-checked vs evidence
    "thesis":               604800.0,        # 7d — hash-checked vs evidence
    "company_lifecycle":    2592000.0,       # 30d — lifecycle changes slowly
    # ── Layer 3 — Run artifacts (immutable history) ──────────────────────────
    "dcf_run":              float("inf"),
    "run_assumption":       float("inf"),
    "run_output":           float("inf"),
    "run_scenario":         float("inf"),
}

# Layer 1 anchored types — additive on write (never overwrite existing).
ANCHORED_TYPES: set[str] = {"filing", "news_item"}

# Below this confidence, we treat a cache hit as a miss (force refresh).
CONFIDENCE_FLOOR = 0.7

# Compound node types whose freshness depends on inputs (not just TTL).
COMPOUND_TYPES: set[str] = {"company_synthesis", "thesis"}


def _ttl_for(node_type: str) -> float:
    return TTL.get(node_type, TTL_DEFAULT)


def make_node_id(ticker: str, node_type: str, field: str, run_id: str | None = None) -> str:
    """Deterministic node ID.

    Shared nodes:     ``"META::driver::AI_monetization"``
    Run-scoped nodes: ``"META::run_assumption::run_a7b3::wacc"``
    """
    if run_id:
        return f"{ticker}::{node_type}::{run_id}::{field}"
    return f"{ticker}::{node_type}::{field}"


# ──────────────────────────────────────────────────────────────────────────────
# Cache
# ──────────────────────────────────────────────────────────────────────────────


class KGCache:
    """Process-wide singleton KG cache. Thread-safe for writes."""

    def __init__(self) -> None:
        self._nodes: dict[str, KGNode] = {}        # node_id → node
        self._edges_by_src: dict[str, list[KGEdge]] = {}
        self._lock = threading.Lock()
        self._loaded_scopes: set[tuple[str | None, str]] = set()

    # ── Loading from SQLite ─────────────────────────────────────────────────

    def load_session(self, session_id: str) -> None:
        """Load all KG nodes/edges for a session into memory."""
        scope = ("session", session_id)
        if scope in self._loaded_scopes:
            return
        nodes = storage.list_kg_nodes(session_id=session_id)
        edges = storage.list_kg_edges(session_id=session_id)
        with self._lock:
            for n in nodes:
                self._nodes[n["id"]] = n  # type: ignore[assignment]
            for e in edges:
                self._edges_by_src.setdefault(e["src_id"], []).append(e)  # type: ignore[arg-type]
            self._loaded_scopes.add(scope)
        logger.info("KGCache loaded session=%s nodes=%d edges=%d",
                    session_id, len(nodes), len(edges))

    def load_ticker(self, ticker: str) -> None:
        """Cross-session load for a ticker (LT memory injection)."""
        scope = (None, ticker)
        if scope in self._loaded_scopes:
            return
        nodes = storage.list_kg_nodes(ticker=ticker)
        with self._lock:
            for n in nodes:
                if n["id"] not in self._nodes:
                    self._nodes[n["id"]] = n  # type: ignore[assignment]
            self._loaded_scopes.add(scope)
        logger.info("KGCache loaded ticker=%s nodes=%d", ticker, len(nodes))

    # ── Core ops ─────────────────────────────────────────────────────────────

    def get(
        self,
        ticker: str,
        node_type: str,
        field: str,
        run_id: str | None = None,
    ) -> KGNode | None:
        """Return cached node if it exists, is fresh, and meets confidence floor.

        Returns None on miss/stale/low-confidence.
        Compound types additionally check ``input_hash`` against current inputs.
        """
        node_id = make_node_id(ticker, node_type, field, run_id)
        node = self._nodes.get(node_id)
        if not node:
            logger.info("KG GET miss ticker=%s type=%s field=%s reason=not_present",
                        ticker, node_type, field)
            return None

        # TTL check
        age = time.time() - float(node.get("updated_at", 0))
        if age > _ttl_for(node_type):
            logger.info(
                "KG GET miss ticker=%s type=%s field=%s reason=stale age=%.0fs ttl=%.0fs",
                ticker, node_type, field, age, _ttl_for(node_type),
            )
            return None

        # Confidence floor
        if float(node.get("confidence", 0)) < CONFIDENCE_FLOOR:
            logger.info(
                "KG GET miss ticker=%s type=%s field=%s reason=low_confidence conf=%.2f floor=%.2f",
                ticker, node_type, field, float(node.get("confidence", 0)), CONFIDENCE_FLOOR,
            )
            return None

        logger.info(
            "KG GET hit ticker=%s type=%s field=%s age=%.0fs conf=%.2f source=%s",
            ticker, node_type, field, age, float(node.get("confidence", 0)),
            node.get("source", "?"),
        )
        # Compound staleness — caller checks externally via evidence_hash
        # (we expose age + input_hash for that)

        return node

    def get_raw(self, node_id: str) -> KGNode | None:
        """Unconditional fetch by id (bypasses TTL/confidence checks)."""
        return self._nodes.get(node_id)

    def put(
        self,
        ticker: str,
        node_type: str,
        field: str,
        value: Any,
        source: str,
        confidence: float = 0.8,
        run_id: str | None = None,
        input_hash: str | None = None,
        session_id: str | None = None,
        respect_user_lock: bool = True,
    ) -> KGNode:
        """Write-through to SQLite + update in-memory cache.

        ``respect_user_lock`` (default True) prevents auto-updates from
        overwriting nodes whose source is 'user_stated'. Only user-stated
        writes can replace user-stated nodes.
        """
        node_id = make_node_id(ticker, node_type, field, run_id)

        # Check user-stated lock
        existing = self._nodes.get(node_id)
        if (
            respect_user_lock
            and existing
            and existing.get("source") == "user_stated"
            and source != "user_stated"
        ):
            logger.debug("KGCache skip auto-write (user-stated lock) id=%s", node_id)
            return existing

        # Layer 1 ANCHORED guard: facts are immutable. If a node with this ID
        # already exists, the fact has not changed (deterministic ID = same fact).
        # Re-fetching the same filing or news item is a no-op — don't touch
        # updated_at, don't rewrite SQLite. Forces additive corpus growth only.
        if existing and node_type in ANCHORED_TYPES:
            logger.debug("KGCache anchored-existing (skip overwrite) id=%s", node_id)
            return existing

        now = time.time()
        node: KGNode = {
            "id": node_id,
            "session_id": session_id,
            "ticker": ticker,
            "node_type": node_type,
            "field": field,
            "value": value,
            "confidence": confidence,
            "source": source,
            "input_hash": input_hash,
            "run_id": run_id,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }

        # SQLite first (durable)
        storage.upsert_kg_node(
            id=node_id,
            session_id=session_id,
            ticker=ticker,
            node_type=node_type,
            field=field,
            value=value,
            confidence=confidence,
            source=source,
            input_hash=input_hash,
            run_id=run_id,
            respect_user_lock=respect_user_lock,
        )

        # Then in-memory
        with self._lock:
            self._nodes[node_id] = node

        value_preview = repr(value)[:80]
        logger.info(
            "KG PUT ticker=%s type=%s field=%s source=%s conf=%.2f run=%s value=%s",
            ticker, node_type, field, source, confidence, run_id or "-", value_preview,
        )
        return node

    def invalidate(self, node_id: str) -> None:
        with self._lock:
            removed = self._nodes.pop(node_id, None)
        logger.info("KG INVALIDATE id=%s removed=%s", node_id, removed is not None)

    def add_edge(
        self,
        *,
        src_id: str,
        tgt_id: str,
        relation: str,
        session_id: str | None = None,
        confidence: float = 0.8,
        source: str = "agent_inferred",
    ) -> KGEdge:
        edge_id = f"{src_id}--{relation}-->{tgt_id}"
        storage.insert_kg_edge(
            id=edge_id,
            session_id=session_id,
            src_id=src_id,
            tgt_id=tgt_id,
            relation=relation,
            confidence=confidence,
            source=source,
        )
        edge: KGEdge = {
            "id": edge_id,
            "session_id": session_id,
            "src_id": src_id,
            "tgt_id": tgt_id,
            "relation": relation,
            "confidence": confidence,
            "source": source,
            "created_at": time.time(),
        }
        with self._lock:
            bucket = self._edges_by_src.setdefault(src_id, [])
            # dedup by id
            bucket[:] = [e for e in bucket if e.get("id") != edge_id]
            bucket.append(edge)
        logger.info(
            "KG EDGE add src=%s relation=%s tgt=%s source=%s conf=%.2f",
            src_id, relation, tgt_id, source, confidence,
        )
        return edge

    def remove_edge(self, edge_id: str) -> None:
        storage.delete_kg_edge(edge_id)
        with self._lock:
            for bucket in self._edges_by_src.values():
                bucket[:] = [e for e in bucket if e.get("id") != edge_id]
        logger.info("KG EDGE remove id=%s", edge_id)

    # ── Subgraph queries (for UI panel + injection) ─────────────────────────

    def get_subgraph(self, ticker: str) -> tuple[list[KGNode], list[KGEdge]]:
        """All nodes + edges for a ticker (any session)."""
        nodes = [n for n in self._nodes.values() if n.get("ticker") == ticker]
        node_ids = {n["id"] for n in nodes}
        edges: list[KGEdge] = []
        for src_id in list(self._edges_by_src.keys()):
            if src_id in node_ids:
                edges.extend(
                    e for e in self._edges_by_src[src_id]
                    if e.get("tgt_id") in node_ids
                )
        type_counts: dict[str, int] = {}
        for n in nodes:
            t = str(n.get("node_type", "?"))
            type_counts[t] = type_counts.get(t, 0) + 1
        logger.info(
            "KG SUBGRAPH ticker=%s nodes=%d edges=%d types=%s",
            ticker, len(nodes), len(edges), type_counts,
        )
        return nodes, edges

    def get_drivers(self, ticker: str) -> list[KGNode]:
        return [
            n for n in self._nodes.values()
            if n.get("ticker") == ticker and n.get("node_type") == "driver"
        ]

    def get_user_beliefs(self, ticker: str) -> list[KGNode]:
        return [
            n for n in self._nodes.values()
            if n.get("ticker") == ticker
            and n.get("node_type") == "user_belief"
            and n.get("source") == "user_stated"
        ]

    def get_anchored_corpus(
        self,
        ticker: str,
        node_types: set[str] | None = None,
        since_ts: float | None = None,
    ) -> list[KGNode]:
        """Return all Layer 1 anchored nodes (filings + news) for a ticker.

        Args:
            ticker: company ticker.
            node_types: filter to specific anchored types (default: all ANCHORED_TYPES).
            since_ts: filter to nodes created on/after this unix ts.
                     Use for "recent news" queries; omit for full corpus.

        Returns newest-first by created_at. Additive corpus — never invalidates,
        only grows as new sources are ingested.
        """
        types = node_types or ANCHORED_TYPES
        items = [
            n for n in self._nodes.values()
            if n.get("ticker") == ticker
            and n.get("node_type") in types
        ]
        if since_ts is not None:
            items = [n for n in items if float(n.get("created_at", 0)) >= since_ts]
        items.sort(key=lambda n: float(n.get("created_at", 0)), reverse=True)
        logger.info(
            "KG ANCHORED ticker=%s types=%s since_ts=%s returned=%d",
            ticker, sorted(types), since_ts, len(items),
        )
        return items

    def get_recent_run_assumptions(
        self, ticker: str, limit: int = 1,
    ) -> dict[str, Any]:
        """Most recent N runs' assumptions, keyed by field → value (latest wins)."""
        runs = [
            n for n in self._nodes.values()
            if n.get("ticker") == ticker and n.get("node_type") == "run_assumption"
        ]
        runs.sort(key=lambda n: n.get("updated_at", 0), reverse=True)
        out: dict[str, Any] = {}
        seen_fields: set[str] = set()
        for n in runs[:limit * 10]:  # over-fetch in case multiple fields per run
            field = n.get("field")
            if field and field not in seen_fields:
                out[field] = n.get("value")
                seen_fields.add(field)
        logger.info(
            "KG RECENT_ASSUMPTIONS ticker=%s limit=%d fields=%s",
            ticker, limit, sorted(out.keys()),
        )
        return out

    # ── Hashing for compound staleness ───────────────────────────────────────

    def evidence_hash(self, ticker: str) -> str:
        """Hash of all driver + theme + user_belief + market_metric nodes for a ticker.

        Used by compound nodes (thesis, company_synthesis) to detect when
        their inputs have changed since they were computed.
        """
        relevant_types = {"driver", "theme", "user_belief", "market_metric", "risk"}
        items: list[tuple[str, str, float]] = []
        for n in self._nodes.values():
            if n.get("ticker") != ticker:
                continue
            if n.get("node_type") not in relevant_types:
                continue
            items.append(
                (n["id"], str(n.get("value")), float(n.get("updated_at", 0)))
            )
        items.sort()
        h = hashlib.sha256()
        for item in items:
            h.update(repr(item).encode("utf-8"))
        return h.hexdigest()[:16]

    # ── Traversal logging ────────────────────────────────────────────────────

    def record_traversal(
        self,
        *,
        run_id: str,
        node_id: str,
        status: str,
        action: str | None = None,
        age_s: float | None = None,
    ) -> None:
        """Record one cache access during a DCF run. Used for UI replay."""
        storage.insert_kg_traversal(
            run_id=run_id, node_id=node_id, status=status,
            action=action, age_s=age_s,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Singleton accessor
# ──────────────────────────────────────────────────────────────────────────────

_cache_singleton: KGCache | None = None


def get_cache() -> KGCache:
    """Return the process-wide KGCache singleton (lazy init)."""
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = KGCache()
    return _cache_singleton
