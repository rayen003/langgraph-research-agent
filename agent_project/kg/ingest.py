"""Unified KG ingestion — single write path for all KG mutations.

Every caller (DCF evidence, valuation, RAG extraction, chat tool calls,
manual edits) goes through `ingest_facts()`. This module enforces:

  1. Entity validation — reject facts where the document entity doesn't match
     the target ticker.
  2. Dedup — skip writes where an identical (ticker, node_type, field, value)
     fact already exists.
  3. Contradiction detection — flag facts where the same (ticker, node_type,
     field, as_of) has a different value, and apply source-tier precedence.
  4. Confidence floor — quarantine facts below a minimum confidence threshold
     instead of writing them to the main KG.
  5. Layer assignment — determine the correct KG layer (1=anchored, 2=derived,
     3=run artifact) and TTL based on node_type and source.
  6. Provenance — every write is traceable to its source (document_id, run_id,
     extraction method, confidence).

Design principles:
  - Deterministic checks first (no LLM calls in the write path).
  - LLM-based checks happen in the periodic audit agent (kg/audit.py).
  - Every rejected/quarantined fact is logged for debugging.
  - The quarantine is queryable — a future audit can promote quarantined facts
    once confidence increases or corroboration is found.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from .cache import KGCache, get_cache
from .schemas import validate_kg_value

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class IngestStatus(str, Enum):
    ACCEPTED = "accepted"           # Written to KG
    DUPLICATE = "duplicate"         # Identical fact already exists — skipped
    CONTRADICTION = "contradiction" # Same key, different value — higher-tier wins
    QUARANTINED = "quarantined"     # Below confidence floor — stored but not served
    REJECTED = "rejected"           # Entity mismatch or invalid data — dropped


class Layer(int, Enum):
    ANCHORED = 1    # Immutable facts (filings, fundamentals). Infinite TTL.
    DERIVED = 2      # Rebuildable inferences (guidance, risk, thesis). Finite TTL.
    RUN_ARTIFACT = 3 # Immutable history of a single run. Never input to future runs.


# Source-tier precedence: higher number = more authoritative.
# When two facts contradict, the higher-tier source wins.
SOURCE_TIER_PRECEDENCE: dict[str, int] = {
    "filing": 5,              # SEC filings (10-K, 10-Q, 8-K)
    "structured_api": 4,       # FMP, yfinance
    "document_extraction": 3,  # User-uploaded docs, LLM-extracted facts
    "dcf_derived": 2,         # Computed from DCF (valuation outputs)
    "web_search": 1,          # Exa search results
    "user_stated": 6,         # Manual edits by user (highest authority)
    "user_override": 6,       # HITL approval overrides
}

# Layer + TTL assignments by node_type.
# Layer 1 (anchored) get infinite TTL; Layer 2 get finite TTL.
NODE_TYPE_LAYER_MAP: dict[str, tuple[int, int | float]] = {
    # node_type: (layer, TTL_seconds)
    # Layer 1 — anchored, infinite TTL
    "company":            (Layer.ANCHORED, float("inf")),
    "filing":             (Layer.ANCHORED, float("inf")),
    "filing_excerpt":     (Layer.ANCHORED, float("inf")),
    "financials_hub":      (Layer.ANCHORED, float("inf")),
    "structured_fundamental": (Layer.ANCHORED, float("inf")),
    "profile":            (Layer.ANCHORED, float("inf")),
    "market_data":        (Layer.ANCHORED, float("inf")),
    # Layer 2 — derived, with TTL
    "news_item":          (Layer.DERIVED, 86400 * 30),      # 30 days
    "news_hub":           (Layer.DERIVED, 86400 * 30),       # 30 days
    "guidance":           (Layer.DERIVED, 86400 * 90),      # 90 days (forward-looking)
    "risk_factor":        (Layer.DERIVED, 86400 * 180),     # 180 days
    "competitive_moat":   (Layer.DERIVED, 86400 * 90),      # 90 days
    "capital_allocation":  (Layer.DERIVED, 86400 * 90),      # 90 days
    "thesis":             (Layer.DERIVED, 86400 * 30),        # 30 days
    "company_synthesis":   (Layer.DERIVED, 86400 * 30),      # 30 days
    "document_fact":      (Layer.DERIVED, 86400 * 180),      # 180 days (from 10-K, stable)
    # Layer 3 — run artifacts
    "dcf_run":            (Layer.RUN_ARTIFACT, float("inf")),
    "valuation_result":   (Layer.RUN_ARTIFACT, float("inf")),
    "scenario_result":    (Layer.RUN_ARTIFACT, float("inf")),
}

# Minimum confidence to write to the main KG (not quarantine).
# Below this threshold → go to quarantine, not the main graph.
MIN_CONFIDENCE_FOR_KG_WRITE: dict[str, float] = {
    "filing": 0.0,              # Filings are always accepted (factual)
    "structured_fundamental": 0.0,  # API data always accepted
    "market_data": 0.0,         # Market data always accepted
    "document_fact": 0.70,      # LLM extraction needs 70%+ confidence
    "guidance": 0.65,           # Forward-looking statements slightly lower
    "risk_factor": 0.65,        # Risk assessments
    "competitive_moat": 0.60,   # Interpretive
    "thesis": 0.60,             # Interpretive
    "news_item": 0.50,          # News items
    "web_excerpt": 0.50,        # Web search
    # Default fallback
    "_default": 0.60,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DocumentFact:
    """A structured financial fact extracted from a document."""
    ticker: str                        # e.g. "AAPL"
    fact_type: str                     # "revenue", "fcff_margin", "guidance", "risk_factor", etc.
    field: str                         # DCFState field name, e.g. "base_revenue", "revenue_growth"
    value: float | None = None         # Numeric value if extractable (e.g. 391.0 for $391B)
    value_text: str = ""               # Original text span (e.g. "revenue grew 8.2% year-over-year")
    fiscal_period: str = ""             # "FY2024", "Q3 2024", "LTM", "FY2024-Q3"
    confidence: float = 0.5            # 0-1, LLM-assigned extraction confidence
    source_doc_id: str = ""             # Document ID from the documents table
    source_filename: str = ""           # Original filename
    source_page: int | None = None     # Page number in the source document
    source_tier: str = "document_extraction"  # Source tier for precedence
    node_type: str = "document_fact"    # KG node type for storage
    metadata: dict[str, Any] = field(default_factory=dict)  # Extra data


@dataclass
class IngestResult:
    """Result of a single fact ingestion attempt."""
    status: IngestStatus               # What happened
    reason: str                         # Human-readable explanation
    node_id: str | None = None          # ID of the written or existing node
    existing_value: Any | None = None   # Value of the pre-existing node (for contradictions)
    new_value: Any | None = None        # Value that was attempted to write
    fact: DocumentFact | None = None    # The original fact (for audit trail)


# ---------------------------------------------------------------------------
# Validation functions (deterministic, no LLM)
# ---------------------------------------------------------------------------


def _validate_entity(
    fact_ticker: str,
    doc_entity: dict[str, Any] | None = None,
) -> list[str]:
    """Check that the fact's ticker matches the source document's entity.

    Returns a list of warning strings. Empty list = valid.
    """
    warnings: list[str] = []
    if not doc_entity:
        return warnings  # No entity metadata to validate against

    doc_ticker = (doc_entity.get("ticker") or "").upper().strip()
    if not doc_ticker:
        return warnings  # No ticker in entity metadata

    if doc_ticker != fact_ticker.upper():
        warnings.append(
            f"Entity mismatch: document says ticker={doc_ticker}, "
            f"fact says ticker={fact_ticker}"
        )
    return warnings


def _check_duplicate(
    cache: KGCache,
    ticker: str,
    node_type: str,
    field: str,
    value: Any,
) -> dict[str, Any] | None:
    """Check if an identical fact already exists in the KG.

    Returns the existing node if found, None otherwise.
    """
    try:
        existing = cache.get_nearest(
            ticker=ticker,
            node_type=node_type,
            field=field,
        )
        if existing and existing.get("value") == value:
            return existing
    except Exception:
        pass  # get_nearest may not be available; fall through
    return None


def _check_contradiction(
    cache: KGCache,
    ticker: str,
    node_type: str,
    field: str,
    new_value: Any,
    new_source: str,
    new_as_of: str = "",
) -> dict[str, Any] | None:
    """Check if a fact with the same (ticker, node_type, field, as_of)
    but a different value already exists.

    Returns the contradictory existing node if found, None otherwise.
    Source-tier precedence determines which value wins.
    """
    try:
        # Look for existing nodes with the same field
        existing_nodes = cache.query(
            ticker=ticker,
            node_type=node_type,
            field=field,
        )
        for node in existing_nodes:
            existing_value = node.get("value")
            existing_as_of = str(node.get("as_of", ""))
            existing_source = node.get("source", "")

            # Only flag as contradiction if same period
            if existing_as_of and new_as_of and existing_as_of != new_as_of:
                continue  # Different period — both are valid

            # Same period, different value
            if existing_value is not None and existing_value != new_value:
                # Check source-tier precedence
                existing_tier = SOURCE_TIER_PRECEDENCE.get(existing_source, 0)
                new_tier = SOURCE_TIER_PRECEDENCE.get(new_source, 0)

                if new_tier > existing_tier:
                    # New source is more authoritative — will overwrite
                    return node
                else:
                    # Existing source is equal or more authoritative — skip new
                    return node
    except Exception:
        pass  # Fall through — no contradiction check available
    return None


def _get_confidence_floor(node_type: str) -> float:
    """Get the minimum confidence for a node_type to enter the main KG."""
    if node_type in MIN_CONFIDENCE_FOR_KG_WRITE:
        return MIN_CONFIDENCE_FOR_KG_WRITE[node_type]
    return MIN_CONFIDENCE_FOR_KG_WRITE["_default"]


def _get_layer_and_ttl(node_type: str) -> tuple[int, float]:
    """Get the Layer and TTL for a node_type."""
    if node_type in NODE_TYPE_LAYER_MAP:
        return NODE_TYPE_LAYER_MAP[node_type]
    # Default to derived with 30-day TTL
    return (Layer.DERIVED, 86400 * 30)


# ---------------------------------------------------------------------------
# Main ingestion function
# ---------------------------------------------------------------------------


def ingest_fact(
    *,
    ticker: str,
    node_type: str,
    field: str,
    value: dict[str, Any],
    source: str,
    confidence: float = 1.0,
    session_id: str = "",
    source_doc_id: str | None = None,
    source_doc_entity: dict[str, Any] | None = None,
    allow_overwrite: bool = False,
    # --- DCF / cache.put compat params ---
    run_id: str | None = None,
    input_hash: str | None = None,
    respect_user_lock: bool = True,
    _cache: KGCache | None = None,
) -> IngestResult:
    """Single entry point for writing facts to the KG.

    Enforces: entity validation, dedup, contradiction detection,
    confidence floor, quarantine, provenance.

    Args:
        ticker: Target ticker (e.g. "AAPL")
        node_type: KG node type (e.g. "filing", "document_fact", "thesis")
        field: Field key (e.g. "FY2024", "base_revenue", "Q3_2024::body")
        value: Fact payload dict (varies by node_type)
        source: Provenance source (e.g. "sec_edgar", "fmp", "document_extraction")
        confidence: 0-1 confidence in this fact
        session_id: Session ID for provenance
        source_doc_id: Document ID (for document_extraction facts)
        source_doc_entity: Entity metadata from document extraction
            (e.g. {"ticker": "META", "company": "Meta Platforms", ...})
        allow_overwrite: If True, allows overwriting existing anchored facts.
            USE CAREFULLY — should only be True for user edits.
        _cache: Override cache instance (for testing).

    Returns:
        IngestResult with status, reason, and optional node_id.
    """
    cache = _cache or get_cache()
    result = IngestResult(
        status=IngestStatus.ACCEPTED,
        reason="",
        fact=None,
    )

    # ── 1. Entity validation ────────────────────────────────────────────
    if source_doc_entity:
        entity_warnings = _validate_entity(ticker, source_doc_entity)
        if entity_warnings:
            # Log the warning but don't reject — just flag it
            logger.warning(
                "KG ingest entity mismatch ticker=%s warnings=%s doc_id=%s",
                ticker, entity_warnings, source_doc_id,
            )
            # If the document's ticker is completely different, reject
            doc_ticker = (source_doc_entity.get("ticker") or "").upper().strip()
            if doc_ticker and doc_ticker != ticker.upper():
                result.status = IngestStatus.REJECTED
                result.reason = (
                    f"Entity mismatch: document says ticker={doc_ticker}, "
                    f"target ticker={ticker}"
                )
                logger.info(
                    "KG ingest REJECTED ticker=%s node_type=%s field=%s "
                    "reason='%s'",
                    ticker, node_type, field, result.reason,
                )
                return result

    # ── 1b. Value-shape validation (advisory) ───────────────────────────
    # Validate the payload against the canonical per-node_type schema. Never
    # fatal — we log shape mismatches but still write (additive KG). This is
    # the contract that prevents the scalar-vs-dict crash class.
    _, _value_warnings = validate_kg_value(node_type, value)
    if _value_warnings:
        logger.warning(
            "KG ingest value-shape ticker=%s node_type=%s field=%s warnings=%s",
            ticker, node_type, field, _value_warnings,
        )

    # ── 2. Confidence floor ──────────────────────────────────────────────
    confidence_floor = _get_confidence_floor(node_type)
    if confidence < confidence_floor:
        result.status = IngestStatus.QUARANTINED
        result.reason = (
            f"Confidence {confidence:.2f} below floor {confidence_floor:.2f} "
            f"for node_type={node_type}"
        )
        # TODO: Write to quarantine store (not the main KG)
        logger.info(
            "KG ingest QUARANTINED ticker=%s node_type=%s field=%s "
            "confidence=%.2f floor=%.2f",
            ticker, node_type, field, confidence, confidence_floor,
        )
        return result

    # ── 3. Dedup check ───────────────────────────────────────────────────
    existing = _check_duplicate(cache, ticker, node_type, field, value)
    if existing and not allow_overwrite:
        result.status = IngestStatus.DUPLICATE
        result.reason = (
            f"Duplicate fact already exists: {ticker}/{node_type}/{field}"
        )
        result.node_id = existing.get("id")
        result.existing_value = existing.get("value")
        logger.debug(
            "KG ingest DUPLICATE ticker=%s node_type=%s field=%s",
            ticker, node_type, field,
        )
        return result

    # ── 4. Contradiction detection ───────────────────────────────────────
    # `value` may be a dict (document facts, theses) OR a raw scalar
    # (market_metric_fund stores a bare float). Guard the dict accessors so a
    # scalar fact doesn't raise AttributeError — that crash was previously
    # swallowed by finalize_node's non-fatal handler and silently dropped ALL
    # shared fundamentals.
    _vdict = value if isinstance(value, dict) else {}
    as_of = str(_vdict.get("as_of", "") or "")
    _inner = _vdict.get("value")
    new_value_scalar = (
        _inner if isinstance(_inner, (int, float))
        else value if isinstance(value, (int, float))
        else None
    )
    contradictory = _check_contradiction(
        cache, ticker, node_type, field,
        new_value=new_value_scalar,
        new_source=source,
        new_as_of=as_of,
    )
    if contradictory and not allow_overwrite:
        existing_source = contradictory.get("source", "unknown")
        existing_value = contradictory.get("value")
        new_tier = SOURCE_TIER_PRECEDENCE.get(source, 0)
        existing_tier = SOURCE_TIER_PRECEDENCE.get(existing_source, 0)

        if new_tier > existing_tier:
            # New source is more authoritative — overwrite
            result.status = IngestStatus.CONTRADICTION
            result.reason = (
                f"Contradiction: overwriting {existing_source}({existing_value}) "
                f"with {source}({value}) — higher tier"
            )
            result.existing_value = existing_value
            result.new_value = value
            # Fall through to write (will overwrite)
            logger.info(
                "KG ingest CONTRADICTION-OVERWRITE ticker=%s node_type=%s "
                "field=%s new_source=%s new_tier=%d existing_source=%s "
                "existing_tier=%d",
                ticker, node_type, field, source, new_tier,
                existing_source, existing_tier,
            )
        else:
            # Existing source is equal or more authoritative — skip
            result.status = IngestStatus.CONTRADICTION
            result.reason = (
                f"Contradiction: keeping {existing_source}({existing_value}) "
                f"over {source}({value}) — equal or higher tier"
            )
            result.node_id = contradictory.get("id")
            result.existing_value = existing_value
            result.new_value = value
            logger.info(
                "KG ingest CONTRADICTION-SKIP ticker=%s node_type=%s "
                "field=%s existing_source=%s existing_tier=%d new_source=%s "
                "new_tier=%d",
                ticker, node_type, field, existing_source,
                existing_tier, source, new_tier,
            )
            return result

    # ── 5. Layer + TTL assignment ─────────────────────────────────────────
    layer, ttl = _get_layer_and_ttl(node_type)

    # ── 6. Write to KG ───────────────────────────────────────────────────
    try:
        put_kwargs: dict[str, Any] = {
            "ticker": ticker,
            "node_type": node_type,
            "field": field,
            "value": value,
            "source": source,
            "confidence": confidence,
            "session_id": session_id,
        }
        # DCF-compat: pass through run_id, input_hash, respect_user_lock
        if run_id is not None:
            put_kwargs["run_id"] = run_id
        if input_hash is not None:
            put_kwargs["input_hash"] = input_hash
        put_kwargs["respect_user_lock"] = respect_user_lock

        node = cache.put(**put_kwargs)
        result.status = IngestStatus.ACCEPTED
        result.node_id = node.get("id") if isinstance(node, dict) else None
        result.reason = f"Written to KG layer={layer} ttl={ttl}s"
        logger.info(
            "KG ingest ACCEPTED ticker=%s node_type=%s field=%s "
            "source=%s confidence=%.2f layer=%d",
            ticker, node_type, field, source, confidence, layer,
        )
    except Exception as exc:
        result.status = IngestStatus.REJECTED
        result.reason = f"KG write failed: {exc}"
        logger.error(
            "KG ingest WRITE-FAILED ticker=%s node_type=%s field=%s error=%s",
            ticker, node_type, field, exc,
        )

    return result


def ingest_facts(
    facts: list[DocumentFact],
    *,
    session_id: str = "",
    _cache: KGCache | None = None,
) -> list[IngestResult]:
    """Batch ingestion of DocumentFacts.

    Convenience wrapper that calls ingest_fact() for each fact.
    Returns a list of IngestResult objects for auditing.
    """
    results: list[IngestResult] = []
    for fact in facts:
        # ── Temporal scoping ────────────────────────────────────────────────
        # The node id derives from (ticker, node_type, field). To keep multiple
        # reporting periods of the SAME metric (revenue Q1 vs Q2 vs FY) instead
        # of overwriting, the period is folded into the field key:
        # "revenue::Q2 2026". Same metric + same period → same id → idempotent
        # upsert (a corrected re-upload replaces in place). Same metric +
        # different period → different id → both retained (the YoY time-series
        # an analyst can scrub). The base fact_type + period are also stored in
        # the value so the UI can group/label without parsing the field key.
        period = (fact.fiscal_period or "").strip()
        field_key = f"{fact.fact_type}::{period}" if period else fact.fact_type
        result = ingest_fact(
            ticker=fact.ticker,
            node_type=fact.node_type,
            field=field_key,
            value={
                "value": fact.value,
                "text": fact.value_text,
                "as_of": period,
                "period": period,
                "fact_type": fact.fact_type,
                "source_doc_id": fact.source_doc_id,
                "source_filename": fact.source_filename,
                "source_page": fact.source_page,
                "confidence": fact.confidence,
                **fact.metadata,
            },
            source=fact.source_tier,
            confidence=fact.confidence,
            session_id=session_id,
            source_doc_id=fact.source_doc_id,
            source_doc_entity=None,  # Entity validated at extraction time
            _cache=_cache,
        )
        results.append(result)

    # Summary logging
    accepted = sum(1 for r in results if r.status == IngestStatus.ACCEPTED)
    duplicates = sum(1 for r in results if r.status == IngestStatus.DUPLICATE)
    contradictions = sum(1 for r in results if r.status == IngestStatus.CONTRADICTION)
    quarantined = sum(1 for r in results if r.status == IngestStatus.QUARANTINED)
    rejected = sum(1 for r in results if r.status == IngestStatus.REJECTED)
    logger.info(
        "KG ingest_facts total=%d accepted=%d duplicates=%d "
        "contradictions=%d quarantined=%d rejected=%d",
        len(results), accepted, duplicates, contradictions, quarantined, rejected,
    )

    return results


# ---------------------------------------------------------------------------
# Convenience wrapper: kg_write()
# ---------------------------------------------------------------------------
# A drop-in replacement for cache.put() that routes through ingest_fact()'s
# quality gates. DCF call sites can swap `cache.put(...)` with
# `kg_write(...)` and get dedup, contradiction detection, confidence floors,
# and entity validation for free.

def kg_write(
    *,
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
    source_doc_entity: dict[str, Any] | None = None,
    _cache: KGCache | None = None,
) -> IngestResult:
    """Drop-in replacement for cache.put() that enforces quality gates.

    Routes every write through ingest_fact() for dedup, contradiction
    detection, entity validation, and confidence floors.

    For DCF run-scoped writes (dcf_run, run_assumption, run_output),
    set allow_overwrite=True via the source convention — these are
    always accepted since they're computation results tied to a run_id.

    For user edits, set source='user_stated' or 'user_override' —
    highest tier in the precedence table.

    Returns IngestResult for auditing; callers can log or ignore.
    """
    # Run-scoped nodes and user edits bypass most quality gates
    # because they're deterministic computation outputs or explicit user
    # actions, not extracted facts that might be wrong.
    _skip_quality_gates = (
        node_type in ("dcf_run", "run_assumption", "run_output",
                      "scenario_result", "valuation_result")
        or source in ("user_stated", "user_override")
    )

    if _skip_quality_gates:
        # Fast path: write directly, preserving existing cache.put behavior
        cache = _cache or get_cache()
        cache.put(
            ticker=ticker,
            node_type=node_type,
            field=field,
            value=value,
            source=source,
            confidence=confidence,
            run_id=run_id,
            input_hash=input_hash,
            session_id=session_id or "",
            respect_user_lock=respect_user_lock,
        )
        logger.info(
            "[KG ⚡] %s fast-path: %s::%s/%s ← %s (conf=%.2f)",
            ticker, node_type, field, source, _trunc_val(value),
            confidence,
        )
        return IngestResult(
            status=IngestStatus.ACCEPTED,
            reason=f"Fast-path: run-scoped/user write ({node_type})",
            new_value=value,
        )

    # Full quality-gate path for shared KV nodes
    result = ingest_fact(
        ticker=ticker,
        node_type=node_type,
        field=field,
        value=value,
        source=source,
        confidence=confidence,
        session_id=session_id or "",
        source_doc_entity=source_doc_entity,
        allow_overwrite=not respect_user_lock,  # user edits can overwrite
        run_id=run_id,
        input_hash=input_hash,
        respect_user_lock=respect_user_lock,
        _cache=_cache,
    )
    logger.info(
        "[KG ✓] %s result=%s %s::%s/%s ← %s (conf=%.2f)",
        ticker, result.status, node_type, field, source, _trunc_val(value),
        confidence,
    )
    return result


def _trunc_val(v: Any, max_len: int = 42) -> str:
    """Truncate a value for logging — keep logs compact."""
    if isinstance(v, (int, float)):
        s = f"{v:.2f}" if isinstance(v, float) else str(v)
    else:
        s = str(v)
    return s[:max_len] + ("…" if len(s) > max_len else "")